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
from typing import List, Optional, Sequence, Set

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

    # Printed by TradeFed at the end of a completed invocation
    RESULT_DIR_PATTERN = re.compile(r"^.*RESULT DIRECTORY\s*:\s*(\S+)\s*$", re.MULTILINE)

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
        retry_type: Optional[str] = "FAILED",
        device_serials: Optional[Sequence[str]] = None,
    ) -> List[str]:
        cmd = self.resolve_command_prefix() + ["run", "retry", "--retry", str(session_id)]
        # TradeFed accepts FAILED or NOT_EXECUTED; omitting the flag retries both
        if retry_type and str(retry_type).upper() != "BOTH":
            cmd.extend(["--retry-type", str(retry_type).upper()])
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
        # time_ns + pid keeps concurrent/back-to-back runs from sharing a log file
        log_path = Path(log_dir) / f"tradefed_run_{time.time_ns()}_{os.getpid()}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        start_time = time.time()
        timeout_seconds = max(timeout_hours * 3600, 60)
        session_id: Optional[int] = None
        results_dir: Optional[str] = None
        return_code = -1
        output_excerpt = ""

        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        # Result dirs that existed before this invocation; the new one is ours.
        before = self.snapshot_result_dirs()

        try:
            with open(log_path, "w", encoding="utf-8") as log_file:
                cwd = str(self.tools_dir)
                logger.info("TradeFed cwd=%s cmd=%s", cwd, " ".join(command))
                self._process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=run_env,
                    start_new_session=True,
                )
                self._process.wait(timeout=timeout_seconds)

            return_code = self._process.returncode if self._process else -1
            if return_code in self.TF_EXIT_CODES:
                logger.info(
                    "TradeFed exited %s (%s)",
                    return_code,
                    self.TF_EXIT_CODES[return_code],
                )
        except subprocess.TimeoutExpired:
            logger.error("TradeFed execution timed out after %s hours", timeout_hours)
            self.kill()
            return_code = 4
        except Exception as exc:
            logger.error("TradeFed execution failed: %s", exc)
            self.kill()
            return_code = -2

        # Post-processing must never turn a finished run into an execution error.
        # A timed-out run may still have flushed partial results worth retrying.
        try:
            output = log_path.read_text(encoding="utf-8", errors="replace")
            output_excerpt = output[-4000:] if output else ""
            results_dir = self.find_results_dir(output, before=before)
            if results_dir:
                session_id = self.resolve_session_id(results_dir)
            else:
                logger.warning("No TradeFed results directory found for %s", log_path)
        except Exception as exc:
            logger.error("Failed to locate TradeFed results for %s: %s", log_path, exc)

        duration = time.time() - start_time
        self._process = None
        return ExecutionResult(
            success=return_code == 0,
            session_id=session_id,
            return_code=return_code,
            duration=duration,
            results_dir=results_dir,
            log_path=str(log_path),
            output_excerpt=output_excerpt,
        )

    @property
    def results_roots(self) -> List[Path]:
        """Directories where TradeFed writes per-invocation result dirs."""
        roots: List[Path] = []
        for root in (self.suite_path / "results", self.tools_dir.parent / "results"):
            if root not in roots:
                roots.append(root)
        return roots

    @staticmethod
    def _session_dirs(results_root: Path) -> List[Path]:
        """Result dirs TradeFed counts as sessions (skips the ``latest`` symlink)."""
        if not results_root.is_dir():
            return []
        return sorted(
            (
                child
                for child in results_root.iterdir()
                if child.is_dir()
                and not child.is_symlink()
                and (child / "test_result.xml").exists()
            ),
            key=lambda p: p.name,
        )

    def snapshot_result_dirs(self) -> Set[str]:
        snapshot: Set[str] = set()
        for root in self.results_roots:
            if root.is_dir():
                snapshot.update(
                    str(c) for c in root.iterdir() if c.is_dir() and not c.is_symlink()
                )
        return snapshot

    def find_results_dir(
        self, output: str, before: Optional[Set[str]] = None
    ) -> Optional[str]:
        """Locate this invocation's results dir (must contain test_result.xml).

        Prefers the dir that appeared since ``before`` was snapshotted, then the
        ``RESULT DIRECTORY`` line TradeFed prints at the end of a completed run.
        Never falls back to "newest dir", which could belong to another run.
        """
        if before is not None:
            new_dirs = [
                d
                for root in self.results_roots
                for d in self._session_dirs(root)
                if str(d) not in before
            ]
            if new_dirs:
                if len(new_dirs) > 1:
                    logger.warning(
                        "Multiple new result dirs appeared (concurrent run?): %s",
                        [d.name for d in new_dirs],
                    )
                return str(max(new_dirs, key=lambda p: p.name))

        for match in reversed(list(self.RESULT_DIR_PATTERN.finditer(output))):
            path = Path(match.group(1).strip().strip("'\""))
            if path.name == "test_result.xml":
                path = path.parent
            if (path / "test_result.xml").exists():
                return str(path)
        return None

    def resolve_session_id(
        self, results_dir: str | Path, timeout_secs: int = 300
    ) -> Optional[int]:
        """Map a results dir to the session index used by ``run retry --retry``."""
        dir_name = Path(results_dir).name
        try:
            proc = subprocess.run(
                self.build_list_results_command(),
                cwd=str(self.tools_dir),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_secs,
            )
            session_id = self.parse_session_table(proc.stdout, dir_name)
            if session_id is not None:
                return session_id
            logger.warning("'list results' did not list %s", dir_name)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("'list results' failed (%s); deriving session from dir order", exc)

        # TradeFed numbers sessions by result-dir name order.
        names = [d.name for d in self._session_dirs(Path(results_dir).parent)]
        if dir_name in names:
            return names.index(dir_name)
        return None

    @staticmethod
    def parse_session_table(output: str, dir_name: str) -> Optional[int]:
        """Parse the session index for ``dir_name`` from ``list results`` output."""
        for line in output.splitlines():
            parts = line.split()
            if len(parts) > 1 and parts[0].isdigit() and dir_name in parts:
                return int(parts[0])
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
