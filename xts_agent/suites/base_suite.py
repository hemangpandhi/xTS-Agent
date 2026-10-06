"""Base suite abstraction — metadata + TradefedRunner delegation.

Execution of plans goes through ``TestPlanExecutor`` + ``TradefedRunner``.
Suite plugins remain available for discovery defaults and optional direct use.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from xts_agent.execution.test_plan_executor import SuiteResult
from xts_agent.execution.tradefed_runner import TradefedRunner
from xts_agent.results.result_parser import ResultParser, TestResults, derive_suite_status

logger = logging.getLogger(__name__)


class BaseSuite(ABC):
    def __init__(self, package_path: Path):
        self._package_path = Path(package_path)

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def command(self) -> str:
        pass

    @property
    def package_path(self) -> Path:
        return self._package_path

    @property
    @abstractmethod
    def plan(self) -> str:
        pass

    @property
    def suite_dir(self) -> Path:
        nested = self._package_path / f"android-{self.name.lower()}"
        if nested.exists():
            return nested
        return self._package_path

    @property
    def tradefed_command(self) -> str:
        return f"{self.command}-tradefed" if not self.command.endswith("-tradefed") else self.command

    def get_runner(self) -> TradefedRunner:
        return TradefedRunner(self.suite_dir, self.tradefed_command)

    def execute(self, devices: List[str], config: Dict[str, Any]) -> SuiteResult:
        shard_count = int(config.get("shard_count", max(len(devices), 1)))
        runner = self.get_runner()
        cmd = runner.build_run_command(
            plan=config.get("plan", self.plan),
            shard_count=shard_count,
            retry_config=config.get("retry") or {},
            exclude_filters=config.get("exclude_filters") or [],
            include_filters=config.get("include_filters") or [],
            extra_args=config.get("extra_args") or [],
            device_serials=devices,
            modules=config.get("modules") or [],
        )
        exec_res = runner.execute(
            cmd,
            timeout_hours=float(config.get("timeout_hours", 24)),
            log_dir=config.get("log_dir", "./results/logs"),
        )
        return self._execution_to_suite_result(exec_res, devices)

    def retry(self, session_id: str, devices: List[str], config: Dict[str, Any]) -> SuiteResult:
        runner = self.get_runner()
        cmd = runner.build_retry_command(
            session_id=int(session_id),
            retry_type=config.get("retry_type", "FAILED"),
            device_serials=devices,
        )
        exec_res = runner.execute(
            cmd,
            timeout_hours=float(config.get("timeout_hours", 24)),
            log_dir=config.get("log_dir", "./results/logs"),
        )
        return self._execution_to_suite_result(exec_res, devices)

    def parse_results(self, result_dir: Path) -> Optional[TestResults]:
        xml_path = Path(result_dir) / "test_result.xml"
        if not xml_path.exists():
            xml_files = list(Path(result_dir).glob("**/test_result.xml"))
            if not xml_files:
                return None
            xml_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            xml_path = xml_files[0]
        try:
            return ResultParser().parse_xml(xml_path)
        except Exception as exc:
            logger.error("Failed to parse results from %s: %s", xml_path, exc)
            return None

    def _execution_to_suite_result(self, exec_res, devices: List[str]) -> SuiteResult:
        pass_c = fail_c = skip_c = 0
        details = None
        results_dir = exec_res.results_dir or ""
        if results_dir:
            parsed = self.parse_results(Path(results_dir))
            if parsed:
                details = parsed
                pass_c = parsed.summary.get("pass", 0)
                fail_c = parsed.summary.get("fail", 0)
                skip_c = parsed.summary.get("skip", 0)
        status, reason = derive_suite_status(details, exec_res.success)
        return SuiteResult(
            name=self.name,
            status=status,
            pass_count=pass_c,
            fail_count=fail_c,
            skip_count=skip_c,
            duration=exec_res.duration,
            session_id=exec_res.session_id or 0,
            results_dir=results_dir,
            retry_count=0,
            details=details,
            log_path=exec_res.log_path,
            device_serials=list(devices),
            error_message=reason,
        )
