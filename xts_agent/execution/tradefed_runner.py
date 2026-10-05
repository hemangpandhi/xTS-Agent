"""TradeFed CLI wrapper for executing Android tests."""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

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
    output_excerpt: str = ""


class TradefedRunner:
    """Wrapper for the TradeFed CLI."""

    TF_EXIT_CODES = {
        0: "Success",
        1: "Configuration Error",
        2: "Device Not Available",
        3: "Fatal Error",
        4: "Timeout",
    }

    def __init__(self, suite_path: str | Path, command_name: str):
        self.suite_path = Path(suite_path)
        self.command_name = command_name
        self._process: Optional[subprocess.Popen] = None

    @property
    def tools_dir(self) -> Path:
        tools = self.suite_path / "tools"
        return tools if tools.exists() else self.suite_path

    @property
    def tradefed_script(self) -> Path:
        return self.tools_dir / self.command_name

    def resolve_command_prefix(self) -> List[str]:
        """Return argv prefix for invoking TradeFed (absolute path preferred)."""
        script = self.tradefed_script
        if script.exists():
            return [str(script)]
        # Fall back to relative invocation from tools_dir (legacy layout)
        return [f"./{self.command_name}"]

    def build_run_command(
        self,
        plan: str,
        shard_count: int = 1,
        retry_config: Optional[dict] = None,
        exclude_filters: Optional[Sequence[str]] = None,
        include_filters: Optional[Sequence[str]] = None,
        extra_args: Optional[Sequence[str]] = None,
        device_serials: Optional[Sequence[str]] = None,
        modules: Optional[Sequence[str]] = None,
    ) -> List[str]:
        cmd = self.resolve_command_prefix() + ["run", "commandAndExit", plan]
        retry_config = retry_config or {}
        exclude_filters = list(exclude_filters or [])
        include_filters = list(include_filters or [])
        extra_args = list(extra_args or [])
        device_serials = list(device_serials or [])
        modules = list(modules or [])

        if shard_count > 1:
            cmd.extend(["--shard-count", str(shard_count)])

        # Pin allocated devices so TradeFed does not grab unrelated ADB targets
        for serial in device_serials:
            cmd.extend(["-s", serial])

        # Intra-module TradeFed retry
        max_runs = retry_config.get("max_testcase_run_count")
        if max_runs and int(max_runs) > 1:
            cmd.extend(["--max-testcase-run-count", str(int(max_runs))])
            strategy = retry_config.get("retry_strategy") or retry_config.get("strategy")
            if strategy:
                cmd.extend(["--retry-strategy", str(strategy)])
            isolation = retry_config.get("retry_isolation_grade") or retry_config.get(
                "isolation_grade"
            )
            if isolation:
                cmd.extend(["--retry-isolation-grade", str(isolation)])
            if retry_config.get("reboot_at_last_retry"):
                cmd.append("--reboot-at-last-retry")

        cmd.extend(["--logcat-on-failure", "--screenshot-on-failure"])

        for f in exclude_filters:
            cmd.extend(["--exclude-filter", f])
        for f in include_filters:
            cmd.extend(["--include-filter", f])
        for module in modules:
            cmd.extend(["--module", module])

        cmd.extend(extra_args)
        return cmd

    def build_retry_command(
        self,
        session_id: int,
        retry_type: str = "FAILED",
        device_serials: Optional[Sequence[str]] = None,
    ) -> List[str]:
        cmd = self.resolve_command_prefix() + [
            "run",
            "retry",
            "--retry",
            str(session_id),
            "--retry-type",
            retry_type,
        ]
        for serial in device_serials or []:
            cmd.extend(["-s", serial])
        return cmd

    def build_list_results_command(self) -> List[str]:
        return self.resolve_command_prefix() + ["list", "results"]

    def execute(
        self,
        command: List[str],
        timeout_hours: float,
        log_dir: str | Path,
        env: Optional[dict] = None,
    ) -> ExecutionResult:
        log_path = Path(log_dir) / f"tradefed_run_{int(time.time())}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        start_time = time.time()
        timeout_seconds = max(timeout_hours * 3600, 60)
        session_id: Optional[int] = None
        results_dir: Optional[str] = None
        return_code = -1
        success = False
        output_excerpt = ""

        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        try:
            with open(log_path, "w", encoding="utf-8") as log_file:
                cwd = str(self.tools_dir)
                logger.info("TradeFed cwd=%s cmd=%s", cwd, " ".join(command))
                self._process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=run_env,
                    start_new_session=True,
                )
                self._process.wait(timeout=timeout_seconds)

            return_code = self._process.returncode if self._process else -1
            success = return_code == 0
            if return_code in self.TF_EXIT_CODES:
                logger.info(
                    "TradeFed exited %s (%s)",
                    return_code,
                    self.TF_EXIT_CODES[return_code],
                )

            output = log_path.read_text(encoding="utf-8", errors="replace")
            output_excerpt = output[-4000:] if output else ""
            session_id = self.get_session_id(output)
            results_dir = self.find_results_dir(output)

        except subprocess.TimeoutExpired:
            logger.error("TradeFed execution timed out after %s hours", timeout_hours)
            self.kill()
            return_code = 4
            success = False
        except Exception as exc:
            logger.error("TradeFed execution failed: %s", exc)
            self.kill()
            return_code = -2
            success = False

        duration = time.time() - start_time
        self._process = None
        return ExecutionResult(
            success=success,
            session_id=session_id,
            return_code=return_code,
            duration=duration,
            results_dir=results_dir,
            log_path=str(log_path),
            output_excerpt=output_excerpt,
        )

    def find_results_dir(self, output: str) -> Optional[str]:
        """Locate TradeFed results directory from log output or known layouts."""
        patterns = [
            r"Saved log(?:s)? to[: ]+(.+)",
            r"RESULTS? DIR(?:ECTORY)?[: =]+(.+)",
            r"Test results saved to[: ]+(.+)",
            r"Result XML path[: ]+(.+)/test_result\.xml",
            r"Generated suite summary report at (.+)/",
        ]
        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip().strip("'\"")
                path = Path(candidate)
                if path.is_file() and path.name == "test_result.xml":
                    return str(path.parent)
                if path.is_dir():
                    return str(path)
                # Even if not yet flushed, return the path TradeFed reported
                if candidate:
                    return candidate

        # Fall back: newest results dir under the suite package
        for results_root in (
            self.suite_path / "results",
            self.suite_path / "android-" + self.suite_path.name.replace("android-", "") / "results",
            self.tools_dir.parent / "results",
        ):
            found = self._newest_result_dir(results_root)
            if found:
                return found
        return None

    @staticmethod
    def _newest_result_dir(results_root: Path) -> Optional[str]:
        if not results_root.is_dir():
            return None
        candidates = []
        for child in results_root.iterdir():
            if child.is_dir() and (child / "test_result.xml").exists():
                candidates.append(child)
            elif child.name == "test_result.xml":
                return str(results_root)
        # Also accept timestamp dirs without waiting for xml if newest
        if not candidates:
            candidates = [c for c in results_root.iterdir() if c.is_dir()]
        if not candidates:
            return None
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        return str(newest)

    def get_session_id(self, output: str) -> Optional[int]:
        for pattern in (
            r"Session (\d+) completed",
            r"session[_ ]id[: =]+(\d+)",
            r"Invocation\[(\d+)\]",
        ):
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def kill(self) -> None:
        if not self._process or self._process.poll() is not None:
            self._process = None
            return
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self._process.pid, signal.SIGKILL)
                self._process.wait(timeout=5)
        except (ProcessLookupError, OSError) as exc:
            logger.warning("Failed to kill TradeFed process group: %s", exc)
            try:
                self._process.kill()
            except OSError:
                pass
        finally:
            self._process = None
