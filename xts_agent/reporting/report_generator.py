"""Master report generator."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

from .gitlab_report import GitLabReportGenerator
from .html_report import HTMLReportGenerator
from .json_report import JSONReportGenerator


class ReportGenerator:
    def generate_all(
        self,
        plan_result: Any,
        rca_report: Any,
        comparison: Any,
        output_dir: str | Path,
        formats: List[str] | None = None,
        basename: str = "report",
        triage: Any = None,
        junit_detail: str = "failures",
    ) -> dict:
        formats = formats or ["html", "json", "junit"]
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        written = {}

        if "html" in formats:
            path = output_dir / f"{basename}.html"
            HTMLReportGenerator().generate(plan_result, rca_report, comparison, path, triage=triage)
            written["html"] = path
        if "junit" in formats:
            # Single copy, under results/junit where GitLab CI collects it
            junit_dir = output_dir.parent / "junit"
            junit_dir.mkdir(parents=True, exist_ok=True)
            path = junit_dir / f"{basename}.xml"
            GitLabReportGenerator(junit_detail).generate(plan_result, path)
            written["junit"] = path
        if "json" in formats:
            path = output_dir / f"{basename}.json"
            JSONReportGenerator().generate(plan_result, rca_report, comparison, path, triage=triage)
            written["json"] = path
        return written
