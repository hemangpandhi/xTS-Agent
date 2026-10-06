"""Main orchestration engine for xTS Agent."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from xts_agent.config_loader import ConfigLoader, TestPlanConfig
from xts_agent.device.adb_wrapper import AdbWrapper
from xts_agent.device.device_manager import DeviceManager
from xts_agent.device.device_prep import DevicePreparer
from xts_agent.execution.ats2_client import ATS2Client
from xts_agent.execution.run_state import RunState
from xts_agent.execution.shard_manager import ShardManager
from xts_agent.execution.test_plan_executor import PlanResult, SuiteResult, TestPlanExecutor
from xts_agent.rca.diagnostic_collector import DiagnosticCollector
from xts_agent.rca.failure_classifier import FailureClassifier
from xts_agent.rca.pattern_matcher import PatternMatcher
from xts_agent.rca.rca_engine import RCAEngine
from xts_agent.reporting.report_generator import ReportGenerator
from xts_agent.reporting.slack_notifier import SlackNotifier
from xts_agent.results.result_parser import overall_status
from xts_agent.results.result_store import ResultStore
from xts_agent.retry.isolation import IsolationHandler
from xts_agent.retry.retry_manager import RetryManager
from xts_agent.suites.suite_registry import SuiteRegistry
from xts_agent.utils.env_validator import EnvironmentValidator
from xts_agent.utils.logger import setup_logger

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        plan_path: str | Path,
        defaults_path: str | Path | None = None,
    ):
        self.config_loader = ConfigLoader(plan_path, defaults_path=defaults_path)
        self.plan: Optional[TestPlanConfig] = None
        self.device_manager = DeviceManager()
        self.shard_manager = ShardManager(self.device_manager)
        self.suite_registry = SuiteRegistry()
        self.retry_manager: Optional[RetryManager] = None
        self.last_plan_result: Optional[PlanResult] = None
        self.last_rca_report = None
        self._results_dir = Path("results")

    def _initialize(self) -> TestPlanConfig:
        self.plan = self.config_loader.load_plan()
        self._results_dir = Path(self.plan.agent.results_dir or "results")
        self._results_dir.mkdir(parents=True, exist_ok=True)
        device_defaults = self.plan.raw_defaults.get("device") or {}
        if device_defaults.get("lease_dir") and not os.environ.get("XTS_LEASE_DIR"):
            self.device_manager.lease_dir = Path(device_defaults["lease_dir"])
        self.device_manager.quarantine_threshold = int(
            device_defaults.get("quarantine_after_failures", 3)
        )
        self.device_manager.quarantine_hours = float(device_defaults.get("quarantine_hours", 24))
        (self._results_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self._results_dir / "reports").mkdir(parents=True, exist_ok=True)

        log_level = getattr(logging, str(self.plan.agent.log_level).upper(), logging.INFO)
        setup_logger(
            "xts_agent",
            log_file=str(Path(self.plan.agent.log_dir) / "xts_agent.log"),
            level=log_level,
        )
        logger.info("Loaded plan: %s", self.plan.name)

        packages_dir = Path(self.plan.paths.xts_packages_dir)
        installed = self.suite_registry.discover_installed_suites(packages_dir)
        logger.info("Installed suites under %s: %s", packages_dir, installed)

        isolation = IsolationHandler(
            self.device_manager,
            virtual_reset_command=(self.plan.raw_defaults.get("device") or {}).get(
                "virtual_reset_command", ""
            ),
            preparer=DevicePreparer(self.plan.device_prep) if self.plan.devices.prepare else None,
        )
        self.retry_manager = RetryManager(self.plan, isolation_handler=isolation)

        EnvironmentValidator.ensure_aapt2_on_path()
        for suite in self.plan.suites:
            if not suite.enabled:
                continue
            script = Path(suite.package_path) / "tools" / suite.command
            if not script.exists():
                script = Path(suite.package_path) / suite.command
            if script.exists():
                EnvironmentValidator.check_tradefed_script(script)

        return self.plan

    def run_plan(
        self, auto_retry: bool = False, dry_run: bool = False, resume: bool = False
    ) -> PlanResult:
        plan = self._initialize()
        notifier = self._slack()
        notifier.notify_start(plan.name)

        logger.info(
            "Running plan %s (dry_run=%s, auto_retry=%s)",
            plan.name,
            dry_run,
            auto_retry,
        )

        # CLI --auto-retry always enables suite retry. Otherwise honor post_execution config.
        effective_retry = bool(auto_retry) or bool(
            plan.post_execution and plan.post_execution.suite_retry.enabled
        )

        executor = TestPlanExecutor(
            config=plan,
            device_manager=self.device_manager,
            shard_manager=self.shard_manager,
            retry_manager=self.retry_manager,
            suite_registry=self.suite_registry,
            results_dir=self._results_dir,
            run_state=RunState.for_plan(self._results_dir, plan.name),
            history_estimate=self._history_estimate,
        )

        results = executor.execute_plan(
            plan, dry_run=dry_run, auto_retry=effective_retry, resume=resume
        )
        self.last_plan_result = results
        logger.info("Plan Execution Finished: %s", results.overall_status)
        logger.info(
            "Duration: %.2fs | Pass: %s, Fail: %s, Skip: %s",
            results.duration,
            results.total_pass,
            results.total_fail,
            results.total_skip,
        )

        rca_report = None
        if plan.post_execution.rca.enabled and not dry_run:
            rca_report = self._run_rca(results)
            self.last_rca_report = rca_report

        self.generate_reports(results, rca_report=rca_report)
        if not dry_run:
            # Upload once per execution; regenerating reports must not re-upload
            self._upload_ats2(results)
        self._persist_results(results)
        notifier.notify_plan_complete(results)
        return results

    def retry_plan(self, max_retries: int = 1) -> Optional[PlanResult]:
        plan = self._initialize()
        logger.info("Retrying plan %s with max_retries=%s", plan.name, max_retries)

        if not self.last_plan_result:
            self.last_plan_result = self._load_latest_plan_result()
        if not self.last_plan_result:
            logger.error(
                "No prior plan result found (memory or results/reports/*.json). "
                "Prefer `run --auto-retry` during execute."
            )
            return None

        updated = dict(self.last_plan_result.suites_results)
        from xts_agent.execution.tradefed_runner import TradefedRunner

        for name, suite_res in list(updated.items()):
            if suite_res.status in ("PASSED", "DRY_RUN") or not suite_res.session_id:
                continue
            suite_cfg = next((s for s in plan.suites if s.name == name), None)
            if not suite_cfg:
                continue
            suite_cfg.retry.max_retries = max_retries
            runner = TradefedRunner(suite_cfg.package_path, suite_cfg.command)
            updated[name] = self.retry_manager.retry_suite_until_done(
                runner=runner,
                suite_result=suite_res,
                suite_config=suite_cfg,
                device_serials=suite_res.device_serials or self.last_plan_result.device_serials,
                log_dir=self._results_dir / "logs",
            )

        total_pass = sum(s.pass_count for s in updated.values())
        total_fail = sum(s.fail_count for s in updated.values())
        total_skip = sum(s.skip_count for s in updated.values())
        overall = overall_status(s.status for s in updated.values())
        result = PlanResult(
            plan_name=plan.name,
            suites_results=updated,
            total_pass=total_pass,
            total_fail=total_fail,
            total_skip=total_skip,
            duration=self.last_plan_result.duration,
            overall_status=overall,
            device_serials=self.last_plan_result.device_serials,
            profile=self.last_plan_result.profile,
        )
        self.last_plan_result = result
        rca = self._run_rca(result) if plan.post_execution.rca.enabled else None
        self.generate_reports(result, rca_report=rca)
        # Retry produced new (cumulative) sessions, so upload those results
        self._upload_ats2(result)
        return result

    def _load_latest_plan_result(self) -> Optional[PlanResult]:
        """Hydrate PlanResult from the newest JSON report under results/reports."""
        import json

        from xts_agent.results.result_parser import ResultParser

        reports_dir = self._results_dir / "reports"
        if not reports_dir.exists():
            return None
        # Newest first by mtime: filenames start with the plan name, so sorting
        # them would pick another plan's report.
        reports = sorted(
            reports_dir.glob("xts_report_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        data = None
        for report in reports:
            try:
                candidate = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("Skipping unreadable report %s: %s", report, exc)
                continue
            if self.plan and candidate.get("plan_name") != self.plan.name:
                continue
            if candidate.get("status") == "DRY_RUN":
                continue
            logger.info("Loaded prior results from %s", report)
            data = candidate
            break
        if data is None:
            return None
        suites = {}
        for name, raw in (data.get("suites") or {}).items():
            sr = SuiteResult(
                name=name,
                status=raw.get("status", "FAILED"),
                pass_count=raw.get("pass_count", 0),
                fail_count=raw.get("fail_count", 0),
                skip_count=raw.get("skip_count", 0),
                duration=raw.get("duration", 0.0),
                session_id=raw.get("session_id", 0),
                results_dir=raw.get("results_dir", ""),
                retry_count=raw.get("retry_count", 0),
                device_serials=raw.get("device_serials") or [],
                error_message=raw.get("error_message", ""),
            )
            if sr.results_dir:
                xml = Path(sr.results_dir) / "test_result.xml"
                if xml.exists():
                    try:
                        sr.details = ResultParser().parse_xml(xml)
                    except Exception as exc:
                        logger.warning("Could not parse %s: %s", xml, exc)
            suites[name] = sr
        return PlanResult(
            plan_name=data.get("plan_name", "unknown"),
            suites_results=suites,
            total_pass=data.get("total_pass", 0),
            total_fail=data.get("total_fail", 0),
            total_skip=data.get("total_skip", 0),
            duration=data.get("duration", 0.0),
            overall_status=data.get("status", "UNKNOWN"),
            device_serials=data.get("device_serials") or [],
            profile=data.get("profile", "development"),
        )

    def analyze(self, enable_rca: bool = True, classify_failures: bool = True):
        plan = self._initialize()
        if not self.last_plan_result:
            self.last_plan_result = self._load_latest_plan_result()
        if not self.last_plan_result:
            logger.error("No plan results available to analyze. Run a plan first.")
            return None
        if not enable_rca:
            logger.info("RCA disabled by flag")
            return None
        plan.post_execution.rca.failure_classification = classify_failures
        report = self._run_rca(self.last_plan_result)
        self.last_rca_report = report
        out = self._results_dir / "rca"
        out.mkdir(parents=True, exist_ok=True)
        from xts_agent.reporting.json_report import JSONReportGenerator

        JSONReportGenerator().generate(
            self.last_plan_result, report, None, out / "rca_report.json"
        )
        logger.info("RCA wrote %s failures to %s", len(report.failures), out)
        return report

    def generate_reports(self, results: PlanResult, rca_report=None, formats: Optional[List[str]] = None):
        if self.plan is None:
            raise RuntimeError("Orchestrator not initialized")

        formats = formats or list(self.plan.post_execution.reporting.formats)
        output_dir = self._results_dir / "reports"
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        device_str = "device"
        if results.device_serials:
            device_str = results.device_serials[0].replace(":", "_").replace(".", "_")
        elif results.suites_results:
            first_suite = next(iter(results.suites_results.values()))
            if first_suite.details and getattr(first_suite.details, "device_info", None):
                di = first_suite.details.device_info
                device_str = str(di.get("device_serial", "multiple_devices"))
                device_str = device_str.replace(":", "_").replace(".", "_")

        basename = f"xts_report_{self.plan.name.replace(' ', '_')}_{device_str}_{timestamp}"
        written = ReportGenerator().generate_all(
            plan_result=results,
            rca_report=rca_report or self.last_rca_report,
            comparison=None,
            output_dir=output_dir,
            formats=formats,
            basename=basename,
        )
        for kind, path in written.items():
            logger.info("Generated %s report: %s", kind, Path(path).absolute())

    def _upload_ats2(self, results: PlanResult) -> None:
        ats_cfg = self.plan.ats2
        client = ATS2Client(
            base_url=ats_cfg.base_url,
            api_key=ats_cfg.api_key,
            timeout_secs=ats_cfg.timeout_secs,
            enabled=ats_cfg.enabled,
        )
        if not client.enabled:
            logger.info("ATS 2.0 upload disabled")
            return

        for suite_name, suite_res in results.suites_results.items():
            if not suite_res.results_dir or not Path(suite_res.results_dir).is_dir():
                logger.warning(
                    "Results dir not found for suite %s, skipping ATS 2.0 upload.",
                    suite_name,
                )
                continue
            results_path = Path(suite_res.results_dir)
            # Archive next to results dir: <results_dir>.zip
            archive_base = results_path.parent / f"{results_path.name}_ats2"
            zip_file = Path(shutil.make_archive(str(archive_base), "zip", results_path))
            logger.info("Uploading compliance zip %s to ATS 2.0...", zip_file)
            client.upload_results(suite_name, self.plan.name, str(zip_file))

    def _run_rca(self, results: PlanResult):
        rca_cfg = self.plan.post_execution.rca
        pattern_matcher = PatternMatcher(rca_cfg.patterns_file)
        classifier = FailureClassifier() if rca_cfg.failure_classification else None
        diagnostics = DiagnosticCollector(
            AdbWrapper, self._results_dir / "diagnostics"
        )
        ai_analyzer = None
        if self.plan.ai_rca.enabled:
            from xts_agent.rca.ai_analyzer import AIAnalyzer

            ai_analyzer = AIAnalyzer(self.plan.ai_rca)

        engine = RCAEngine(
            config=rca_cfg,
            diagnostic_collector=diagnostics,
            failure_classifier=classifier,
            pattern_matcher=pattern_matcher,
            ai_analyzer=ai_analyzer,
        )
        report = engine.analyze_plan_results(results)
        logger.info(
            "RCA complete: %s failures across classes %s",
            len(report.failures),
            dict(report.summary),
        )
        return report

    def _persist_results(self, results: PlanResult) -> None:
        try:
            store = ResultStore(self.plan.agent.database_path)
            for suite_name, suite_res in results.suites_results.items():
                if suite_res.details is None:
                    continue
                store.save_run(
                    plan_name=results.plan_name,
                    suite_name=suite_name,
                    results=suite_res.details,
                    metadata={
                        "status": suite_res.status,
                        "session_id": suite_res.session_id,
                        "devices": suite_res.device_serials,
                        "retry_count": suite_res.retry_count,
                        "duration": suite_res.duration,
                    },
                )
        except Exception as exc:
            logger.warning("ResultStore persistence skipped: %s", exc)

    def _history_estimate(self, suite_name: str) -> Optional[float]:
        """Measured device-hours for a suite from past runs, if any."""
        try:
            return ResultStore(self.plan.agent.database_path).estimate_device_hours(suite_name)
        except Exception as exc:
            logger.debug("No duration history for %s: %s", suite_name, exc)
            return None

    def _slack(self) -> SlackNotifier:
        webhook = ""
        if self.plan and self.plan.post_execution.reporting.notifications:
            webhook = self.plan.post_execution.reporting.notifications.get(
                "slack_webhook", ""
            ) or ""
        # Also allow default_config reporting.notifications
        if not webhook and self.plan and self.plan.raw_defaults:
            webhook = (
                self.plan.raw_defaults.get("reporting", {})
                .get("notifications", {})
                .get("slack_webhook", "")
            )
        return SlackNotifier(webhook)

    def device_check(self, min_devices: int = 1) -> bool:
        devices = self.device_manager.get_available_devices()
        logger.info("Found %s available device(s)", len(devices))
        for d in devices:
            logger.info("  %s [%s] battery=%s type=%s", d.serial, d.state, d.battery_level, d.device_type)
        return len(devices) >= min_devices

    def health_check(self, reboot_unhealthy: bool = False) -> dict:
        return self.device_manager.health_check_all(reboot_unhealthy=reboot_unhealthy)

    def cleanup(self, kill_tradefed: bool = False) -> None:
        if kill_tradefed:
            from xts_agent.execution.tradefed_runner import kill_recorded_tradefed

            killed = kill_recorded_tradefed(self._results_dir / "logs")
            logger.info("Killed %s TradeFed process group(s) started by this agent", len(killed))
        # Release any allocated serials in this process
        if self.device_manager._allocated:
            serials = list(self.device_manager._allocated)
            self.device_manager.release_devices(serials)
            logger.info("Released allocated devices: %s", serials)

    def download_packages(self) -> None:
        from xts_agent.suites.suite_downloader import SuiteDownloader

        logger.info("Delegating package download to SuiteDownloader / scripts")
        SuiteDownloader()  # ensure importable; actual URLs come from ops scripts
        logger.info(
            "Use scripts/download_xts_packages.sh for full package fetch into %s",
            self.plan.paths.xts_packages_dir if self.plan else "/opt/xts",
        )
