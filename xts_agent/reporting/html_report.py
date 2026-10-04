from __future__ import annotations
from __future__ import annotations
"""
from __future__ import annotations
Rich HTML report using Jinja2.
"""
from pathlib import Path
from typing import Any
import jinja2

class HTMLReportGenerator:
    def generate(self, plan_result: Any, rca_report: Any, comparison: Any, output_path: str | Path):
        template_dir = Path(__file__).parent / "templates"
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir)))
        template = env.get_template("report_base.html")
        
        html = template.render(
            plan=plan_result,
            rca=rca_report,
            comp=comparison
        )
        
        with open(output_path, "w") as f:
            f.write(html)
