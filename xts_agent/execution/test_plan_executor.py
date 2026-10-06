"""Plan-based execution sequencer."""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from xts_agent.config_loader import SuiteConfig, TestPlanConfig
from xts_agent.results.result_parser import (
    ResultParser,
    TestResults,
    derive_suite_status,
    has_unexecuted_modules,
    overall_status,
)

from .run_state import RunState
from .tradefed_runner import ExecutionResult, TradefedRunner

logger = logging.getLogger(__name__)


@dataclass
class SuiteResult:
    name: str
    status: str
    pass_count: int
    fail_count: int
    skip_count: int
    duration: float
    session_id: int
    results_dir: str
    retry_count: int
    details: Any = None
    log_path: str = ""
    device_serials: List[str] = field(default_factory=list)
    error_message: str = ""


@dataclass
class PlanResult:
    plan_name: str
    suites_results: Dict[str, SuiteResult]
    total_pass: int
    total_fail: int
    total_skip: int
    duration: float
    overall_status: str
    device_serials: List[str] = field(default_factory=list)
    profile: str = "development"


class TestPlanExecutor:
    """Executes suites from a loaded TestPlanConfig via TradefedRunner."""

    def __init__(
        self,
        config: TestPlanConfig,
        device_manager: Any,
        shard_manager: Any,
        retry_manager: Any,
        suite_registry: Any,
        results_dir: str | Path = "./results",
        run_state: Optional[RunState] = None,
    ):
        self.config = config
        self.device_manager = device_manager
        self.shard_manager = shard_manager
        self.retry_manager = retry_manager
        self.suite_registry = suite_registry
        self.results_dir = Path(results_dir)
        self.logs_dir = self.results_dir / "logs"
        self._last_runners: Dict[str, TradefedRunner] = {}
        self.run_state = run_state
        self._resuming = False

    def execute_plan(
        self,
        plan_config: Optional[TestPlanConfig] = None,
        dry_run: bool = False,
        auto_retry: bool = False,
        resume: bool = False,
    ) -> PlanResult:
        plan = plan_config or self.config
        if self.run_state is not None and not dry_run:
            self._resuming = resume and self.run_state.load()
            if resume and not self._resuming:
                logger.warning("No unfinished run state for %s; starting a fresh run", plan.name)
            if self._resuming:
                logger.info("Resuming %s from %s", plan.name, self.run_state.path)
            else:
                self.run_state.start(plan.name, getattr(plan, "profile", "development"))
        suites = list(plan.suites)
        # Lower priority number runs first (cert plan: CTS=1 before CATBOX=6)
        suites.sort(key=lambda s: getattr(s, "priority", 100))

        suites_results: Dict[str, SuiteResult] = {}
        total_pass = total_fail = total_skip = 0
        all_devices: List[str] = []
        start_time = time.time()

        for suite_config in suites:
            if not suite_config.enabled:
                logger.info("Skipping disabled suite: %s", suite_config.name)
                continue

            suite_res = self.execute_suite(suite_config, dry_run=dry_run, auto_retry=auto_retry)
            suites_results[suite_res.name] = suite_res
            total_pass += suite_res.pass_count
            total_fail += suite_res.fail_count
            total_skip += suite_res.skip_count
            all_devices.extend(suite_res.device_serials)

            if plan.devices.reboot_between_suites and not dry_run:
                logger.info("Rebooting %s between suites", suite_res.device_serials)
                self.device_manager.reboot_and_wait_all(list(suite_res.device_serials))

        duration = time.time() - start_time
        final_status = overall_status(s.status for s in suites_results.values())
        if self.run_state is not None and not dry_run:
            self.run_state.mark_complete(final_status)
        # Preserve order while uniquifying
        seen = set()
        unique_devices = []
        for serial in all_devices:
            if serial not in seen:
                seen.add(serial)
                unique_devices.append(serial)

        return PlanResult(
            plan_name=plan.name,
            suites_results=suites_results,
            total_pass=total_pass,
            total_fail=total_fail,
            total_skip=total_skip,
            duration=duration,
            overall_status=final_status,
            device_serials=unique_devices,
            profile=getattr(plan, "profile", "development"),
        )

    def execute_suite(
        self,
        suite_config: SuiteConfig,
        dry_run: bool = False,
        auto_retry: bool = False,
    ) -> SuiteResult:
        name = suite_config.name
        plan_name = suite_config.plan
        logger.info("Preparing to execute suite: %s (plan: %s)", name, plan_name)

        devices = []
        serials: List[str] = []
        try:
            package_path = self._resolve_package_path(suite_config)
            command_name = suite_config.command or f"{name.lower()}-tradefed"
            runner = TradefedRunner(package_path, command_name)
            self._last_runners[name] = runner

            if dry_run:
                # Resolve shard intent without requiring live ADB
                configured = suite_config.sharding.shard_count
                if isinstance(configured, str) and configured.lower() == "auto":
                    shard_count = max(int(self.config.devices.min_devices), 1)
                else:
                    try:
                        shard_count = max(int(configured), 1)
                    except (TypeError, ValueError):
                        shard_count = max(int(self.config.devices.min_devices), 1)
                serials = [f"dry-run-device-{i}" for i in range(shard_count)]
                retry_dict = dataclasses.asdict(suite_config.retry)
                cmd = runner.build_run_command(
                    plan=plan_name,
                    shard_count=len(serials),
                    retry_config=retry_dict,
                    exclude_filters=suite_config.exclude_filters,
                    include_filters=suite_config.include_filters,
                    extra_args=suite_config.extra_args,
                    device_serials=serials,
                    modules=suite_config.modules,
                )
                logger.info("DRY RUN command: %s", " ".join(cmd))
                return SuiteResult(
                    name=name,
                    status="DRY_RUN",
                    pass_count=0,
                    fail_count=0,
                    skip_count=0,
                    duration=0.0,
                    session_id=0,
                    results_dir="",
                    retry_count=0,
                    device_serials=serials,
                )

            entry = self.run_state.suite(name) if (self._resuming and self.run_state) else None
            if entry and entry.get("status") == "PASSED":
                logger.info("Resume: %s already PASSED in session %s; skipping", name, entry.get("session_id"))
                return self._suite_result_from_entry(name, entry)

            available = self.device_manager.get_available_devices(
                min_battery=self.config.devices.min_battery_level
            )
            device_type = self.config.devices.device_type or "any"
            required_props = dict(self.config.devices.properties or {})
            if entry and entry.get("fingerprint"):
                # A resumed session may only continue on the build it started on
                required_props["ro.build.fingerprint"] = entry["fingerprint"]
            pool = self.device_manager.select_shard_pool(available, device_type, required_props)
            if len(pool) < self.config.devices.min_devices:
                raise ValueError(
                    f"Need at least {self.config.devices.min_devices} {device_type} device(s) "
                    f"on one build, found {len(pool)} (of {len(available)} available)"
                )

            shard_count = self.shard_manager.calculate_shard_count(len(pool), suite_config)
            if shard_count < 1:
                raise ValueError("No devices available for sharding!")

            devices = self.device_manager.allocate_devices(
                shard_count, device_type, candidates=pool
            )
            serials = [d.serial for d in devices]
            logger.info(
                "Allocated %s device(s): %s (shards=%s, build=%s)",
                len(devices),
                serials,
                shard_count,
                devices[0].build_fingerprint if devices else "",
            )
            if getattr(self.config, "profile", "") == "certification" and devices:
                _warn_if_not_user_build(devices[0].build_fingerprint)

            if not runner.tradefed_script.exists() and not (
                runner.tools_dir / command_name
            ).exists():
                raise FileNotFoundError(
                    f"TradeFed script not found under {runner.tools_dir}: {command_name}"
                )

            fingerprint = devices[0].build_fingerprint if devices else ""
            suite_res = self._resume_suite(name, entry, runner, suite_config, serials) if entry else None
            if suite_res is None:
                if self.run_state is not None:
                    self.run_state.suite_started(
                        name, runner.snapshot_result_dirs(), fingerprint, serials
                    )
                retry_dict = dataclasses.asdict(suite_config.retry)
                cmd = runner.build_run_command(
                    plan=plan_name,
                    shard_count=len(serials),
                    retry_config=retry_dict,
                    exclude_filters=suite_config.exclude_filters,
                    include_filters=suite_config.include_filters,
                    extra_args=suite_config.extra_args,
                    device_serials=serials,
                    modules=suite_config.modules,
                )
                logger.info("Executing: %s", " ".join(cmd))

                exec_res = runner.execute(
                    cmd,
                    timeout_hours=suite_config.timeout_hours,
                    log_dir=self.logs_dir,
                )
                suite_res = self._to_suite_result(name, exec_res, serials, retry_count=0)
            self._checkpoint(suite_res)

            if auto_retry and self.retry_manager and suite_res.status != "PASSED":
                suite_res = self.retry_manager.retry_suite_until_done(
                    runner=runner,
                    suite_result=suite_res,
                    suite_config=suite_config,
                    device_serials=serials,
                    log_dir=self.logs_dir,
                    on_attempt=self._checkpoint,
                )

            return suite_res

        except Exception as exc:
            logger.error("Suite %s failed before/during execution: %s", name, exc)
            return SuiteResult(
                name=name,
                status="FAILED",
                pass_count=0,
                fail_count=0,
                skip_count=0,
                duration=0.0,
                session_id=0,
                results_dir="",
                retry_count=0,
                device_serials=serials,
                error_message=str(exc),
            )
        finally:
            if serials:
                self.device_manager.release_devices(serials)
                logger.info("Released devices: %s", serials)

    def _checkpoint(self, suite_res: SuiteResult) -> None:
        if self.run_state is not None:
            self.run_state.suite_updated(suite_res)

    def _suite_result_from_entry(self, name: str, entry: dict) -> SuiteResult:
        exec_res = ExecutionResult(
            success=True,
            session_id=entry.get("session_id"),
            return_code=0,
            duration=0.0,
            results_dir=entry.get("results_dir") or None,
            log_path="",
        )
        res = self._to_suite_result(
            name, exec_res, entry.get("device_serials") or [], entry.get("retry_count", 0)
        )
        return res

    def _resume_suite(
        self,
        name: str,
        entry: dict,
        runner: TradefedRunner,
        suite_config: SuiteConfig,
        serials: List[str],
    ) -> Optional[SuiteResult]:
        """Continue an interrupted suite from its last TradeFed session.

        Returns None when there is nothing to continue from (fresh run needed).
        """
        session_id = entry.get("session_id")
        results_dir = entry.get("results_dir") or ""
        if session_id is None:
            # Interrupted during the first invocation: find the dir it created
            results_dir = runner.find_results_dir("", before=set(entry.get("before") or [])) or ""
            session_id = runner.resolve_session_id(results_dir) if results_dir else None
        if session_id is None:
            logger.info("Resume: no TradeFed session recorded for %s; running it fresh", name)
            return None

        current = self._suite_result_from_entry(
            name, {**entry, "session_id": session_id, "results_dir": results_dir}
        )
        current.device_serials = list(serials)
        if not has_unexecuted_modules(current):
            # Complete-but-failed suites continue through normal auto-retry
            return current

        logger.info(
            "Resume: continuing %s from session %s (%s)", name, session_id, current.error_message
        )
        cmd = runner.build_retry_command(session_id, retry_type=None, device_serials=serials)
        exec_res = runner.execute(cmd, timeout_hours=suite_config.timeout_hours, log_dir=self.logs_dir)
        if exec_res.results_dir is None:
            logger.error("Resume retry for %s produced no results; keeping session %s", name, session_id)
            return current
        return self._to_suite_result(name, exec_res, serials, retry_count=current.retry_count + 1)

    def _resolve_package_path(self, suite_config: SuiteConfig) -> Path:
        if suite_config.package_path:
            path = Path(suite_config.package_path)
            if path.exists():
                return path
            logger.warning("Configured package_path missing: %s", path)

        # Prefer registry discovery under xts packages dir
        if self.suite_registry:
            registered = self.suite_registry.get_suite(suite_config.name)
            if registered is not None:
                candidate = Path(registered.package_path) / f"android-{suite_config.name.lower()}"
                if candidate.exists():
                    return candidate
                if Path(registered.package_path).exists():
                    # Registry stores base_path; suite dir may be base itself
                    base = Path(registered.package_path)
                    nested = base / f"android-{suite_config.name.lower()}"
                    return nested if nested.exists() else base

        fallback = Path(self.config.paths.xts_packages_dir) / f"android-{suite_config.name.lower()}"
        return fallback

    def _to_suite_result(
        self,
        name: str,
        exec_res: ExecutionResult,
        serials: List[str],
        retry_count: int,
    ) -> SuiteResult:
        pass_c = fail_c = skip_c = 0
        parsed: Optional[TestResults] = None

        results_dir = exec_res.results_dir or ""
        if results_dir:
            res_file = Path(results_dir) / "test_result.xml"
            if not res_file.exists():
                # Sometimes xml is one level deeper
                matches = list(Path(results_dir).glob("**/test_result.xml"))
                if matches:
                    res_file = max(matches, key=lambda p: p.stat().st_mtime)
                    results_dir = str(res_file.parent)
            if res_file.exists():
                try:
                    parsed = ResultParser().parse_xml(res_file)
                    pass_c = parsed.summary.get("pass", 0)
                    fail_c = parsed.summary.get("fail", 0)
                    skip_c = parsed.summary.get("skip", 0)
                except Exception as exc:
                    logger.error("Failed to parse results: %s", exc)

        status, reason = derive_suite_status(parsed, exec_res.success)
        if status != "PASSED":
            logger.warning("Suite %s %s: %s", name, status, reason)

        return SuiteResult(
            name=name,
            status=status,
            pass_count=pass_c,
            fail_count=fail_c,
            skip_count=skip_c,
            duration=exec_res.duration,
            session_id=exec_res.session_id or 0,
            results_dir=results_dir,
            retry_count=retry_count,
            details=parsed,
            log_path=exec_res.log_path,
            device_serials=list(serials),
            error_message="" if status == "PASSED" else _error_message(reason, exec_res),
        )


def _error_message(reason: str, exec_res: ExecutionResult) -> str:
    tail = exec_res.output_excerpt[-500:] if exec_res.output_excerpt else ""
    return f"{reason}\n{tail}".strip()


def _warn_if_not_user_build(fingerprint: str) -> None:
    """Fingerprint ends with ``:<build_type>/<tags>``; submissions need ``user``."""
    build_type = fingerprint.rsplit(":", 1)[-1].split("/", 1)[0] if ":" in fingerprint else ""
    if build_type != "user":
        logger.warning(
            "Certification profile on a %r build (%s); official submissions require a user build",
            build_type or "unknown",
            fingerprint,
        )
