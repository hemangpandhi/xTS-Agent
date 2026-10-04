from __future__ import annotations
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict, Any
import xml.etree.ElementTree as ET
import logging

logger = logging.getLogger(__name__)

@dataclass
class TestResults:
    passed: int
    failed: int
    ignored: int
    modules_done: int
    modules_total: int
    session_id: str
    report_path: Path

@dataclass
class SuiteResult:
    success: bool
    results: TestResults
    log_path: Path

class BaseSuite(ABC):
    def __init__(self, package_path: Path):
        self._package_path = package_path

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

    @abstractmethod
    def execute(self, devices: List[str], config: Dict[str, Any]) -> SuiteResult:
        pass

    @abstractmethod
    def retry(self, session_id: str, devices: List[str], config: Dict[str, Any]) -> SuiteResult:
        pass

    def get_results_dir(self) -> Path:
        # Results are usually at <package_path>/android-<suite>/results
        # Can be overridden by subclasses if different
        suite_lower = self.name.lower()
        return self._package_path / "results"

    def parse_results(self, result_dir: Path) -> Optional[TestResults]:
        xml_path = result_dir / "test_result.xml"
        if not xml_path.exists():
            # Sometimes it's nested in a timestamp dir
            xml_files = list(result_dir.glob("*/test_result.xml"))
            if not xml_files:
                return None
            # Sort to get the latest
            xml_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            xml_path = xml_files[0]

        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            
            passed = int(root.get("pass", "0"))
            failed = int(root.get("failed", "0"))
            modules_done = int(root.get("modules_done", "0"))
            modules_total = int(root.get("modules_total", "0"))
            
            return TestResults(
                passed=passed,
                failed=failed,
                ignored=0,
                modules_done=modules_done,
                modules_total=modules_total,
                session_id=root.get("session_id", "0"),
                report_path=xml_path
            )
        except Exception as e:
            logger.error(f"Failed to parse results from {xml_path}: {e}")
            return None

    def build_command(self, shard_count: int = 1, retry_config: bool = False, exclude_filters: Optional[List[str]] = None) -> List[str]:
        cmd = []
        if retry_config:
            cmd.append("retry")
        else:
            cmd.extend(["run", "commandAndExit", self.plan])
        
        if shard_count > 1:
            cmd.extend(["--shard-count", str(shard_count)])
            
        if exclude_filters:
            for f in exclude_filters:
                cmd.extend(["--exclude-filter", f])
                
        return cmd

    def run_tradefed(self, cmd_args: List[str], devices: List[str], env: Optional[Dict[str, str]] = None) -> SuiteResult:
        tf_script = self._package_path / f"android-{self.name.lower()}" / "tools" / f"{self.name.lower()}-tradefed"
        
        if not tf_script.exists():
            raise FileNotFoundError(f"TradeFed script not found: {tf_script}")
            
        cmd = [str(tf_script)] + cmd_args
        
        for serial in devices:
            cmd.extend(["-s", serial])
            
        logger.info(f"Running TradeFed: {' '.join(cmd)}")
        
        try:
            # Setting Popen to stream output or capture
            result = subprocess.run(cmd, env=env, check=False, text=True, capture_output=True)
            
            results_dir = self.get_results_dir()
            parsed_results = self.parse_results(results_dir)
            
            if not parsed_results:
                parsed_results = TestResults(0, 0, 0, 0, 0, "unknown", Path(""))
                
            return SuiteResult(
                success=result.returncode == 0,
                results=parsed_results,
                log_path=results_dir / "latest" / "tradefed.log" # Approximation
            )
        except Exception as e:
            logger.error(f"Error running TradeFed: {e}")
            return SuiteResult(False, TestResults(0,0,0,0,0,"",Path("")), Path(""))
