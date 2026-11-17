"""Run a fix-loop target through TradeFed and read back what really happened."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Sequence

from xts_agent.execution.tradefed_runner import TradefedRunner
from xts_agent.results.result_parser import ResultParser
from xts_agent.triage.signature import normalize_message

logger = logging.getLogger(__name__)

# TradeFed setup errors worth surfacing from the console log when a module
# never reached its tests (the XML <Reason> is truncated)
SETUP_ERROR_RE = re.compile(r"(TargetSetupError\[[^\]]+\][^\n]*|Some preparation step failed[^\n]*|"
                            r"System status checker \[[^\]]+\] failed|NetworkConnectivityChecker is still failing[^\n]*)")


@dataclass
class TestTarget:
    """What the loop must turn green: one module and optionally specific tests."""

    __test__ = False  # not a pytest class

    module: str
    tests: List[str] = field(default_factory=list)  # "pkg.Class#method" or "pkg.Class"
    abi: str = ""
    label: str = ""

    @classmethod
    def parse(cls, spec: str) -> "TestTarget":
        """``"CtsOsTestCases android.os.cts.StrictModeTest#testNetwork"`` or just a module."""
        parts = spec.split()
        abi = ""
        if parts and re.fullmatch(r"(arm64-v8a|armeabi-v7a|x86_64|x86|riscv64)", parts[0]):
            abi = parts.pop(0)
        if not parts:
            raise ValueError(f"empty target spec: {spec!r}")
        return cls(module=parts[0], tests=parts[1:], abi=abi)

    def include_filters(self) -> List[str]:
        prefix = f"{self.abi} {self.module}" if self.abi else self.module
        return [f"{prefix} {t}" for t in self.tests] or [prefix]

    @property
    def name(self) -> str:
        return self.label or " ".join([self.module] + self.tests)


@dataclass
class FailureRecord:
    test_id: str  # "Module Class#method", or "Module" when the module never ran
    kind: str  # FAIL | MODULE_NOT_RUN
    message: str
    stack: str = ""

    @property
    def signature(self) -> str:
        return f"{self.kind}:{normalize_message(self.message, 120)}"


@dataclass
class RunOutcome:
    results_dir: str
    log_path: str
    return_code: int
    duration_secs: float
    passed: int = 0
    failed: int = 0
    not_run_modules: int = 0
    tests: Dict[str, str] = field(default_factory=dict)
    failures: List[FailureRecord] = field(default_factory=list)
    setup_errors: List[str] = field(default_factory=list)

    @property
    def green(self) -> bool:
        """Every targeted test executed and passed."""
        return self.passed > 0 and self.failed == 0 and self.not_run_modules == 0

    @property
    def problems(self) -> int:
        return self.failed + self.not_run_modules

    def failure_text(self) -> str:
        parts = [f"{f.test_id}\n{f.message}\n{f.stack[:3000]}" for f in self.failures]
        parts.extend(self.setup_errors)
        return "\n\n".join(parts)

    def signatures(self) -> List[str]:
        return sorted({f.signature for f in self.failures})

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["green"] = self.green
        return d


class TargetExecutor:
    def __init__(
        self,
        suite_path: str | Path,
        command_name: str = "cts-tradefed",
        plan: str = "cts",
        log_dir: str | Path = "results/fixloop/logs",
        timeout_hours: float = 1.0,
        extra_args: Sequence[str] = ("--disable-reboot",),
    ):
        self.runner = TradefedRunner(suite_path, command_name)
        self.runner.progress_interval_secs = 0
        self.plan = plan
        self.log_dir = Path(log_dir)
        self.timeout_hours = timeout_hours
        self.extra_args = list(extra_args)
        self.path_prepend: List[str] = []

    def env(self) -> Dict[str, str]:
        if not self.path_prepend:
            return {}
        return {"PATH": os.pathsep.join(self.path_prepend + [os.environ.get("PATH", "")])}

    def run(self, target: TestTarget, serial: str) -> RunOutcome:
        cmd = self.runner.build_run_command(
            self.plan,
            device_serials=[serial],
            include_filters=target.include_filters(),
            extra_args=self.extra_args,
            diagnostics={"logcat_on_failure": True, "screenshot_on_failure": False},
        )
        res = self.runner.execute(cmd, self.timeout_hours, self.log_dir, env=self.env())
        outcome = RunOutcome(
            results_dir=res.results_dir or "",
            log_path=res.log_path,
            return_code=res.return_code,
            duration_secs=round(res.duration, 1),
        )
        log_text = ""
        try:
            log_text = Path(res.log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        outcome.setup_errors = sorted(set(m.group(1)[:400] for m in SETUP_ERROR_RE.finditer(log_text)))[:20]
        xml = Path(res.results_dir or "") / "test_result.xml"
        if not res.results_dir or not xml.is_file():
            outcome.not_run_modules = 1
            outcome.failures.append(FailureRecord(
                target.module, "MODULE_NOT_RUN",
                f"TradeFed produced no results (exit {res.return_code})",
                "\n".join(outcome.setup_errors) or res.output_excerpt[-2000:],
            ))
            return outcome
        return self._read_results(outcome, xml, target, serial)

    @staticmethod
    def _wanted(target: TestTarget, class_name: str, test_name: str) -> bool:
        if not target.tests:
            return True
        full = f"{class_name}#{test_name}"
        return any(full == t or class_name == t or full.startswith(t + "[") for t in target.tests)

    def _read_results(self, outcome: RunOutcome, xml: Path, target: TestTarget, serial: str = "") -> RunOutcome:
        parsed = ResultParser().parse_xml(xml)
        if serial and parsed.devices and serial not in parsed.devices:
            # Never score a run on another DUT's results
            outcome.not_run_modules = 1
            outcome.failures.append(FailureRecord(
                target.module, "MODULE_NOT_RUN",
                f"result dir {xml.parent.name} belongs to {','.join(parsed.devices)}, not {serial}",
            ))
            return outcome
        for mod in parsed.modules:
            if mod.name != target.module:
                continue
            if not mod.done and not mod.test_cases:
                outcome.not_run_modules += 1
                outcome.failures.append(FailureRecord(
                    mod.module_id, "MODULE_NOT_RUN", (mod.reason or "module did not run").split("\n")[0][:500],
                    mod.reason[:4000],
                ))
                continue
            for tc in mod.test_cases:
                if not self._wanted(target, tc.class_name, tc.test_name):
                    continue
                status = tc.result.upper()
                outcome.tests[tc.test_id] = status
                if status == "PASS":
                    outcome.passed += 1
                elif status in ("FAIL", "ERROR"):
                    outcome.failed += 1
                    outcome.failures.append(FailureRecord(
                        tc.test_id, "FAIL", tc.message or "", tc.stack_trace or "",
                    ))
            if not mod.done and mod.reason:
                # Partial module: tests ran but the module aborted
                outcome.not_run_modules += 1
                outcome.failures.append(FailureRecord(
                    mod.module_id, "MODULE_NOT_RUN", mod.reason.split("\n")[0][:500], mod.reason[:4000],
                ))
        if not outcome.tests and not outcome.failures:
            outcome.not_run_modules = 1
            outcome.failures.append(FailureRecord(
                target.module, "MODULE_NOT_RUN", "no matching tests executed (check the filter)",
            ))
        return outcome
