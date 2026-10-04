"""TradeFed CLI wrapper for executing Android tests."""
from __future__ import annotations
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

@dataclass
class ExecutionResult:
    """Represents the result of a TradeFed execution."""
    success: bool
    session_id: Optional[int]
    return_code: int
    duration: float
    results_dir: Optional[str]
    log_path: str

class TradefedRunner:
    """Wrapper for the TradeFed CLI."""

    def __init__(self, suite_path: str, command_name: str):
        self.suite_path = Path(suite_path)
        self.command_name = command_name
        self._process: Optional[subprocess.Popen] = None
        self.tf_exit_codes = {
            0: "Success",
            1: "Configuration Error",
            2: "Device Not Available",
            3: "Fatal Error",
            4: "Timeout",
        }

    def build_run_command(self, plan: str, shard_count: int, retry_config: dict, exclude_filters: List[str], include_filters: List[str], extra_args: List[str]) -> List[str]:
        cmd = [f"./{self.command_name}", "run", "commandAndExit", plan]
        if shard_count > 1:
            cmd.extend(["--shard-count", str(shard_count)])
        
        # Add retry configuration
        if retry_config:
            if getattr(retry_config, "max_testcase_run_count", None) or retry_config and type(retry_config) is dict and "max_testcase_run_count" in retry_config:
                cmd.extend(["--max-testcase-run-count", str(retry_config["max_testcase_run_count"])])
            if "retry_strategy" in retry_config:
                cmd.extend(["--retry-strategy", retry_config["retry_strategy"]])
            if "retry_isolation_grade" in retry_config:
                cmd.extend(["--retry-isolation-grade", retry_config["retry_isolation_grade"]])
            if retry_config.get("reboot_at_last_retry"):
                cmd.append("--reboot-at-last-retry")

        cmd.extend(["--logcat-on-failure", "--screenshot-on-failure"])

        for f in exclude_filters:
            cmd.extend(["--exclude-filter", f])
            
        for f in include_filters:
            cmd.extend(["--include-filter", f])
            
        cmd.extend(extra_args)
        return cmd

    def build_retry_command(self, session_id: int, retry_type: str = "FAILED") -> List[str]:
        return [f"./{self.command_name}", "run", "retry", "--retry", str(session_id), "--retry-type", retry_type]

    def build_list_results_command(self) -> List[str]:
        return [f"./{self.command_name}", "list", "results"]

    def execute(self, command: List[str], timeout_hours: float, log_dir: str) -> ExecutionResult:
        log_path = Path(log_dir) / f"tradefed_run_{int(time.time())}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        start_time = time.time()
        timeout_seconds = timeout_hours * 3600
        session_id = None
        results_dir = None
        
        try:
            with open(log_path, "w") as log_file:
                cwd = str(self.suite_path / "tools") if (self.suite_path / "tools").exists() else str(self.suite_path)
                self._process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True
                )
                self._process.wait(timeout=timeout_seconds)
                
            return_code = self._process.returncode
            success = (return_code == 0)
            
            # Parse output for session_id and results_dir
            with open(log_path, "r") as log_file:
                output = log_file.read()
                session_id = self.get_session_id(output)
                match = re.search(r'Saved log to (.+)', output)
                if match:
                    results_dir = match.group(1)

        except subprocess.TimeoutExpired:
            logger.error(f"TradeFed execution timed out after {timeout_hours} hours.")
            self.kill()
            return_code = -1
            success = False
        except Exception as e:
            logger.error(f"TradeFed execution failed: {e}")
            return_code = -2
            success = False
            
        duration = time.time() - start_time
        return ExecutionResult(success, session_id, return_code, duration, results_dir, str(log_path))

    def get_session_id(self, output: str) -> Optional[int]:
        match = re.search(r'Session (\d+) completed', output)
        if match:
            return int(match.group(1))
        match = re.search(r'session_id: (\d+)', output)
        if match:
            return int(match.group(1))
        return None

    def kill(self):
        if self._process and self._process.poll() is None:
            self._process.kill()
            self._process = None
