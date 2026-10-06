"""Automatic retry orchestration."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from xts_agent.config_loader import SuiteConfig
from xts_agent.results.result_parser import ResultParser, derive_suite_status

logger = logging.getLogger(__name__)


@dataclass
class RetryResult:
    tests_retried: int
    tests_passed_on_retry: int
    tests_still_failing: int
    total_retry_attempts: int


class RetryManager:
    """Suite-level and agent-level retry orchestration around TradefedRunner."""

    def __init__(self, config: Any, tradefed_runner: Any = None, isolation_handler: Any = None):
        self.config = config
        self.tradefed_runner = tradefed_runner
        self.isolation_handler = isolation_handler
        self.retry_history: List[Dict[str, Any]] = []

    def should_retry(self, suite_result: Any, attempt: int, max_retries: int) -> bool:
        if suite_result.status in ("PASSED", "DRY_RUN"):
            return False
        if not suite_result.session_id:
            logger.warning("Cannot suite-retry without session_id")
            return False
        if attempt >= max_retries:
            return False
        return True

    def retry_suite_until_done(
        self,
        runner: Any,
        suite_result: Any,
        suite_config: SuiteConfig,
        device_serials: Sequence[str],
        log_dir: str | Path,
    ) -> Any:
        """Run TradeFed `run retry` up to suite max_retries.

        Each retry session's test_result.xml is cumulative (TradeFed carries the
        previous session's results forward), so the latest session is the source
        of truth and the next retry always targets it.
        """
        # Suite max_retries == 0 is an explicit opt-out (e.g. smoke plans)
        max_retries = max(int(suite_config.retry.max_retries), 0)
        post = getattr(self.config, "post_execution", None)
        retry_type = suite_config.retry.retry_type
        cooldown = 60
        if post and post.suite_retry:
            if not post.suite_retry.enabled:
                return suite_result
            retry_type = post.suite_retry.retry_type or retry_type
            cooldown = int(post.suite_retry.cooldown_secs)
            # If suite did not set a positive limit, fall back to post-exec cap
            if max_retries <= 0:
                max_retries = int(post.suite_retry.max_suite_retries)
        if max_retries <= 0:
            logger.info("Suite retry skipped for %s (max_retries=0)", suite_result.name)
            return suite_result

        attempt = 0
        current = suite_result

        while self.should_retry(current, attempt, max_retries):
            attempt += 1
            logger.info(
                "Suite retry %s/%s for %s (session=%s)",
                attempt,
                max_retries,
                current.name,
                current.session_id,
            )

            if self.isolation_handler and device_serials:
                grade = suite_config.retry.isolation_grade
                for serial in device_serials:
                    self.isolation_handler.apply_isolation(serial, grade)

            if cooldown > 0:
                time.sleep(min(cooldown, 300))

            # Incomplete runs must also re-run NOT_EXECUTED modules
            attempt_retry_type = retry_type
            if current.status == "INCOMPLETE" and str(retry_type).upper() == "FAILED":
                attempt_retry_type = None
            cmd = runner.build_retry_command(
                session_id=current.session_id,
                retry_type=attempt_retry_type,
                device_serials=list(device_serials),
            )
            exec_res = runner.execute(
                cmd,
                timeout_hours=suite_config.timeout_hours,
                log_dir=log_dir,
            )

            from xts_agent.execution.test_plan_executor import SuiteResult

            details = None
            if exec_res.results_dir:
                xml_path = Path(exec_res.results_dir) / "test_result.xml"
                try:
                    details = ResultParser().parse_xml(xml_path)
                except Exception as exc:
                    logger.error("Retry result parse failed: %s", exc)

            if details is None or not exec_res.session_id:
                logger.error(
                    "Retry %s/%s for %s produced no usable session; stopping retries",
                    attempt,
                    max_retries,
                    current.name,
                )
                self.retry_history.append(
                    {
                        "suite": current.name,
                        "attempt": attempt,
                        "session_id": None,
                        "status": "NO_RESULTS",
                        "fail_count": current.fail_count,
                    }
                )
                break

            status, reason = derive_suite_status(details, exec_res.success)
            current = SuiteResult(
                name=current.name,
                status=status,
                pass_count=details.summary.get("pass", 0),
                fail_count=details.summary.get("fail", 0),
                skip_count=details.summary.get("skip", 0),
                duration=current.duration + exec_res.duration,
                session_id=exec_res.session_id,
                results_dir=exec_res.results_dir,
                retry_count=attempt,
                details=details,
                log_path=exec_res.log_path,
                device_serials=list(device_serials),
                error_message=reason,
            )
            self.retry_history.append(
                {
                    "suite": current.name,
                    "attempt": attempt,
                    "session_id": current.session_id,
                    "status": current.status,
                    "fail_count": current.fail_count,
                }
            )

        return current

    def execute_suite_retry(
        self,
        session_id: int,
        retry_type: str = "FAILED",
        device_serials: Optional[Sequence[str]] = None,
        timeout_hours: float = 24.0,
        log_dir: str | Path = "./results/logs",
    ) -> Any:
        if not self.tradefed_runner:
            raise RuntimeError("RetryManager has no TradefedRunner bound")
        cmd = self.tradefed_runner.build_retry_command(
            session_id, retry_type, device_serials=device_serials
        )
        return self.tradefed_runner.execute(cmd, timeout_hours=timeout_hours, log_dir=log_dir)

    def execute_agent_retry(
        self,
        failed_tests: list,
        device_manager: Any,
        classifications: Optional[Dict[str, str]] = None,
    ) -> RetryResult:
        """Filter retryable failures based on RCA classifications when provided."""
        classifications = classifications or {}
        skip_classes = {"PRODUCT_BUG", "TEST_BUG"}
        retried = [
            t
            for t in failed_tests
            if classifications.get(getattr(t, "test_name", ""), "") not in skip_classes
        ]
        logger.info(
            "Agent retry candidates: %s (of %s failed)",
            len(retried),
            len(failed_tests),
        )
        return RetryResult(
            tests_retried=len(retried),
            tests_passed_on_retry=0,
            tests_still_failing=len(failed_tests),
            total_retry_attempts=0,
        )

    def get_retry_summary(self) -> Dict[str, Any]:
        return {
            "total_retries": len(self.retry_history),
            "history": self.retry_history,
        }
