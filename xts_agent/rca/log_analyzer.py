"""Logcat/bugreport analysis helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class LogAnalysisResult:
    errors: List[str] = field(default_factory=list)
    crashes: List[str] = field(default_factory=list)
    stack_traces: List[str] = field(default_factory=list)
    relevant_section: str = ""
    severity: str = "LOW"


class LogAnalyzer:
    def analyze_logcat(
        self, logcat_path: str | Path, failure_time: Optional[str] = None
    ) -> LogAnalysisResult:
        path = Path(logcat_path)
        result = LogAnalysisResult()
        if not path.exists():
            return result
        text = path.read_text(encoding="utf-8", errors="replace")
        result.errors = self.extract_errors(text)
        result.crashes = self.extract_crashes(text)
        result.stack_traces = self.extract_stack_traces(text)
        if result.crashes:
            result.severity = "HIGH"
        elif result.errors:
            result.severity = "MEDIUM"
        if failure_time:
            result.relevant_section = self.correlate_with_test(text, failure_time, failure_time)
        return result

    def extract_errors(self, logcat: str) -> List[str]:
        return [line for line in logcat.splitlines() if " E " in line][:200]

    def extract_crashes(self, logcat: str) -> List[str]:
        return [
            line
            for line in logcat.splitlines()
            if "FATAL EXCEPTION" in line or "Fatal signal" in line
        ][:100]

    def extract_stack_traces(self, logcat: str) -> List[str]:
        traces: List[str] = []
        current: List[str] = []
        for line in logcat.splitlines():
            if "FATAL EXCEPTION" in line or line.strip().startswith("at "):
                current.append(line)
            elif current:
                traces.append("\n".join(current))
                current = []
        if current:
            traces.append("\n".join(current))
        return traces[:50]

    def correlate_with_test(
        self, logcat: str, test_start_time: str, test_end_time: str
    ) -> str:
        # Timestamp correlation is best-effort; return a truncated window
        lines = logcat.splitlines()
        if not lines:
            return ""
        mid = len(lines) // 2
        window = lines[max(0, mid - 50) : mid + 50]
        return "\n".join(window)
