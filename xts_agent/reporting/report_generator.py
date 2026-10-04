from __future__ import annotations
"""
from __future__ import annotations
Master report generator.
"""
from pathlib import Path
from typing import List, Any
from .html_report import HTMLReportGenerator
from .gitlab_report import GitLabReportGenerator
from .json_report import JSONReportGenerator

class ReportGenerator:
    def generate_all(self, plan_result: Any, rca_report: Any, comparison: Any, output_dir: str | Path, formats: List[str] = ['html','json','junit']):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        if 'html' in formats:
            HTMLReportGenerator().generate(plan_result, rca_report, comparison, output_dir / "report.html")
        if 'junit' in formats:
            GitLabReportGenerator().generate(plan_result, output_dir / "report.xml")
        if 'json' in formats:
            JSONReportGenerator().generate(plan_result, rca_report, comparison, output_dir / "report.json")
