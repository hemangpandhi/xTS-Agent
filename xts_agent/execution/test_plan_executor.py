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
    overall_status,
)

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
    ):
        self.config = config
        self.device_manager = device_manager
        self.shard_manager = shard_manager
        self.retry_manager = retry_manager
        self.suite_registry = suite_registry
        self.results_dir = Path(results_dir)
        self.logs_dir = self.results_dir / "logs"
        self._last_runners: Dict[str, TradefedRunner] = {}

    def execute_plan(
        self,
        plan_config: Optional[TestPlanConfig] = None,
        dry_run: bool = False,
        auto_retry: bool = False,
    ) -> PlanResult:
        plan = plan_config or self.config
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
                for serial in suite_res.device_serials:
                    logger.info("Rebooting %s between suites", serial)
                    self.device_manager.reboot_device(serial)
                    self.device_manager.wait_for_device(serial)

        duration = time.time() - start_time
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
            overall_status=overall_status(s.status for s in suites_results.values()),
            device_serials=unique_devices,
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

            available = self.device_manager.get_available_devices(
                min_battery=self.config.devices.min_battery_level
            )
            available_count = len(available)
            if available_count < self.config.devices.min_devices:
                raise ValueError(
                    f"Need at least {self.config.devices.min_devices} devices, "
                    f"found {available_count}"
                )

            shard_count = self.shard_manager.calculate_shard_count(
                available_count, suite_config
            )
            if shard_count < 1:
                raise ValueError("No devices available for sharding!")

            device_type = self.config.devices.device_type or "any"
            devices = self.device_manager.allocate_devices(shard_count, device_type)
            serials = [d.serial for d in devices]
            logger.info("Allocated %s device(s): %s (shards=%s)", len(devices), serials, shard_count)

            if not runner.tradefed_script.exists() and not (
                runner.tools_dir / command_name
            ).exists():
                raise FileNotFoundError(
                    f"TradeFed script not found under {runner.tools_dir}: {command_name}"
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

            if auto_retry and self.retry_manager and suite_res.status != "PASSED":
                suite_res = self.retry_manager.retry_suite_until_done(
                    runner=runner,
                    suite_result=suite_res,
                    suite_config=suite_config,
                    device_serials=serials,
                    log_dir=self.logs_dir,
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
