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
from xts_agent.execution import cancel
from xts_agent.execution.ats2_client import ATS2Client
from xts_agent.execution.run_state import RunState
from xts_agent.execution.shard_manager import ShardManager
from xts_agent.execution.test_plan_executor import PlanResult, SuiteResult, TestPlanExecutor
from xts_agent.rca.diagnostic_collector import DiagnosticCollector
from xts_agent.rca.failure_classifier import FailureClassifier
from xts_agent.rca.pattern_matcher import PatternMatcher
from xts_agent.rca.rca_engine import RCAEngine
from xts_agent.reporting.metrics import MetricsPublisher, publisher_for
from xts_agent.reporting.report_generator import ReportGenerator
from xts_agent.reporting.slack_notifier import SlackNotifier
from xts_agent.results.result_parser import overall_status
from xts_agent.results.result_store import ResultStore
from xts_agent.storage.db import Database
from xts_agent.retry.isolation import IsolationHandler
from xts_agent.retry.retry_manager import RetryManager
from xts_agent.suites.suite_registry import SuiteRegistry
from xts_agent.utils.env_validator import EnvironmentValidator
from xts_agent.utils.logger import run_id, setup_logging

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
        self.last_triage = None
        self._last_report_files: List[Path] = []
        self._last_triage_path: Optional[Path] = None
        self._results_dir = Path("results")
        self._metrics: Optional[MetricsPublisher] = None

    def _initialize(self) -> TestPlanConfig:
        self.plan = self.config_loader.load_plan()
        self._results_dir = Path(self.plan.agent.results_dir or "results")
        self._results_dir.mkdir(parents=True, exist_ok=True)
        device_defaults = self.plan.raw_defaults.get("device") or {}
        self.device_manager.adb_timeout = int(device_defaults.get("adb_timeout_secs", 30))
        self.device_manager.reboot_timeout = int(device_defaults.get("reboot_timeout_secs", 120))
        if device_defaults.get("lease_dir") and not os.environ.get("XTS_LEASE_DIR"):
            self.device_manager.lease_dir = Path(device_defaults["lease_dir"])
        self.device_manager.quarantine_threshold = int(
            device_defaults.get("quarantine_after_failures", 3)
        )
        self.device_manager.quarantine_hours = float(device_defaults.get("quarantine_hours", 24))
        (self._results_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self._results_dir / "reports").mkdir(parents=True, exist_ok=True)

        log_level = getattr(logging, str(self.plan.agent.log_level).upper(), logging.INFO)
        setup_logging(
            level=log_level,
            log_file=str(Path(self.plan.agent.log_dir) / "xts_agent.log"),
        )
        logger.info("Loaded plan: %s (run id %s)", self.plan.name, run_id())
        self._metrics = publisher_for(self.plan)

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

        self._apply_tool_paths()
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

    def _publish_metrics(self, result: PlanResult) -> None:
        metrics = getattr(self, "_metrics", None)
        if metrics is None:
            return
        try:
            quarantined = len(self.device_manager.ledger.quarantined())
        except Exception:
            quarantined = 0
        metrics.run_finished(result, getattr(self, "last_triage", None), quarantined)

    def _apply_tool_paths(self) -> None:
        """Honour paths.java_home / android_sdk / adb_path for this process and TradeFed."""
        paths = self.plan.paths
        prepend = []
        if paths.java_home:
            os.environ["JAVA_HOME"] = paths.java_home
            prepend.append(str(Path(paths.java_home) / "bin"))
        if paths.android_sdk:
            os.environ["ANDROID_HOME"] = paths.android_sdk
            os.environ["ANDROID_SDK_ROOT"] = paths.android_sdk
        if paths.adb_path:
            adb = Path(paths.adb_path)
            prepend.append(str(adb.parent if adb.name == "adb" else adb))
        missing = [p for p in prepend if not Path(p).exists()]
        if missing:
            logger.warning("Configured tool paths do not exist: %s", missing)
        if prepend:
            os.environ["PATH"] = os.pathsep.join(prepend + [os.environ.get("PATH", "")])
            logger.info("Tool paths from config: %s", prepend)

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
            metrics=self._metrics,
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

        if results.cancelled:
            # Be quick: CI gives a short grace period after SIGTERM. Skip RCA,
            # triage, uploads and history (a partial run would skew trends).
            self.generate_reports(results)
            self._publish_metrics(results)
            notifier.notify_plan_complete(results)
            return results

        rca_report = None
        if plan.post_execution.rca.enabled and not dry_run:
            rca_report = self._run_rca(results)
            self.last_rca_report = rca_report
        if not dry_run:
            self._run_triage(results, rca_report)

        self.generate_reports(results, rca_report=rca_report)
        if not dry_run:
            # Upload once per execution; regenerating reports must not re-upload
            self._upload_ats2(results)
            self._upload_artifacts(results)
        if not dry_run:
            self._persist_results(results)
            self.write_dashboard()
            self._publish_metrics(results)
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
            if suite_res.status in ("PASSED", "DRY_RUN") or suite_res.session_id is None:
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
        was_cancelled = cancel.cancelled()
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
            cancelled=was_cancelled,
        )
        self.last_plan_result = result
        if was_cancelled:
            self.generate_reports(result)
            self._publish_metrics(result)
            return result
        rca = self._run_rca(result) if plan.post_execution.rca.enabled else None
        self._run_triage(result, rca)
        self.generate_reports(result, rca_report=rca)
        # Retry produced new (cumulative) sessions, so upload those results
        self._upload_ats2(result)
        self._upload_artifacts(result)
        self._persist_results(result)
        self.write_dashboard()
        self._publish_metrics(result)
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
                session_id=raw.get("session_id"),
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
        self._run_triage(self.last_plan_result, report)
        return report

    def triage_engine(self):
        from xts_agent.triage.engine import TriageEngine
        from xts_agent.triage.history import FailureHistory
        from xts_agent.triage.known_issues import KnownIssueDB
        from xts_agent.triage.ownership import OwnershipMap

        cfg = self.plan.triage
        return TriageEngine(
            history=FailureHistory(
                Database(cfg.history_db) if cfg.history_db else self.database(),
                window=cfg.history_window,
            ),
            known_issues=KnownIssueDB.load(cfg.known_issues_file),
            ownership=OwnershipMap.load(cfg.ownership_file),
        )

    def code_indexer(self):
        from xts_agent.rca.code_indexer import OEMCodeIndexer

        cfg = self.plan.ai_rca
        return OEMCodeIndexer(cfg.index_db_path, cfg.source_code_paths, cfg.embedding_model)

    def _run_group_ai(self, report) -> None:
        cfg = self.plan.ai_rca
        if not cfg.enabled:
            return
        from xts_agent.rca.llm_provider import get_llm_provider
        from xts_agent.triage.ai_rca import GroupAIAnalyzer

        try:
            provider = get_llm_provider(cfg)  # refuses external providers without opt-in
            indexer = self.code_indexer() if cfg.source_code_paths else None
            GroupAIAnalyzer(
                provider,
                indexer=indexer,
                cache_db=self.database(),
                max_groups=cfg.max_groups,
            ).analyze(report)
        except Exception as exc:
            logger.error("AI RCA skipped: %s", exc)

    def _file_jira(self, report, stamp: str) -> None:
        cfg = self.plan.jira
        if cfg is None or not cfg.enabled:
            return
        from xts_agent.triage.jira_filer import JiraFiler

        try:
            JiraFiler.from_config(cfg).file(
                report, preview_path=self._results_dir / "triage" / f"jira_preview_{stamp}.json"
            )
        except Exception as exc:
            logger.error("Jira filing skipped: %s", exc)

    def plan_result_from_results_dirs(self, suite: str, results_dirs: List[str]) -> PlanResult:
        """Build a PlanResult from raw TradeFed result dirs (no agent report needed)."""
        from xts_agent.execution.tradefed_runner import ExecutionResult

        suites = {}
        for i, rdir in enumerate(results_dirs):
            exec_res = ExecutionResult(True, None, 0, 0.0, str(rdir), "")
            name = suite if len(results_dirs) == 1 else f"{suite}#{i + 1}"
            res = TestPlanExecutor._to_suite_result(name, exec_res, [], 0)
            suites[name] = res
        return PlanResult(
            plan_name=self.plan.name,
            suites_results=suites,
            total_pass=sum(s.pass_count for s in suites.values()),
            total_fail=sum(s.fail_count for s in suites.values()),
            total_skip=sum(s.skip_count for s in suites.values()),
            duration=0.0,
            overall_status=overall_status(s.status for s in suites.values()),
            profile=self.plan.profile,
        )

    def _run_triage(self, results: PlanResult, rca_report=None):
        if not self.plan.triage.enabled:
            return None
        try:
            report = self.triage_engine().triage(results, rca_report)
        except Exception as exc:
            # Triage must never take down the run that produced the results
            logger.error("Triage failed: %s", exc, exc_info=True)
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._run_group_ai(report)
        self._file_jira(report, stamp)
        path = report.save(self._results_dir / "triage" / f"triage_{stamp}.json")
        self._last_triage_path = path
        logger.info("Triage report: %s", path)
        self.last_triage = report
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
            triage=self.last_triage,
            junit_detail=self.plan.post_execution.reporting.junit_detail,
        )
        for kind, path in written.items():
            logger.info("Generated %s report: %s", kind, Path(path).absolute())
        self._last_report_files = [Path(p) for p in written.values()]

    def _upload_artifacts(self, results: PlanResult) -> None:
        from xts_agent.storage.artifacts import ArtifactStore, record_artifacts

        store = ArtifactStore(self.plan.artifacts) if self.plan.artifacts else None
        if store is None or not store.enabled:
            return
        try:
            urls = store.upload_run(
                results.plan_name,
                results.suites_results,
                self._last_report_files,
                self._last_triage_path,
            )
            record_artifacts(self.database(), results.plan_name, urls)
            for name, url in urls.items():
                logger.info("Artifact %s -> %s", name, url)
        except Exception as exc:
            # Never fail a finished run because the archive is unreachable
            logger.error("Artifact upload failed: %s", exc)

    def _upload_ats2(self, results: PlanResult) -> None:
        ats_cfg = self.plan.ats2
        client = ATS2Client(
            base_url=ats_cfg.base_url,
            api_key=ats_cfg.api_key,
            timeout_secs=ats_cfg.timeout_secs,
            enabled=ats_cfg.enabled,
            upload_path=ats_cfg.upload_path,
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
        # AI analysis runs once per failure group in triage, not per test here
        engine = RCAEngine(
            config=rca_cfg,
            diagnostic_collector=diagnostics,
            failure_classifier=classifier,
            pattern_matcher=pattern_matcher,
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
            store = ResultStore(self.database())
            for suite_res in results.suites_results.values():
                # Recorded even without details: infra failures belong in trends too
                store.save_suite_run(results.plan_name, results.profile, suite_res)
        except Exception as exc:
            logger.warning("ResultStore persistence skipped: %s", exc)

    def database(self) -> Database:
        """Shared store: agent.database_url (e.g. PostgreSQL) or SQLite at database_path."""
        if getattr(self, "_database", None) is None:
            agent = self.plan.agent
            self._database = Database(agent.database_url or agent.database_path)
            logger.info("Using %r", self._database)
        return self._database

    def write_dashboard(self) -> Optional[Path]:
        """Regenerate results/reports/dashboard.html from the database."""
        from xts_agent.reporting.dashboard import collect, write_dashboard
        from xts_agent.triage.known_issues import KnownIssueDB

        try:
            data = collect(
                ResultStore(self.database()),
                history=self.triage_engine().history,
                known_issues=KnownIssueDB.load(self.plan.triage.known_issues_file),
                ledger=self.device_manager.ledger,
            )
            path = write_dashboard(self._results_dir / "reports" / "dashboard.html", data)
            logger.info("Dashboard: %s", path.absolute())
            return path
        except Exception as exc:
            logger.error("Dashboard generation failed: %s", exc)
            return None

    def _history_estimate(self, suite_name: str) -> Optional[float]:
        """Measured device-hours for a suite from past runs, if any."""
        try:
            return ResultStore(self.database()).estimate_device_hours(suite_name)
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
