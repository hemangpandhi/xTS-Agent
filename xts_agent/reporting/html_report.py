"""Rich HTML report using Jinja2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2


class HTMLReportGenerator:
    def generate(
        self,
        plan_result: Any,
        rca_report: Any,
        comparison: Any,
        output_path: str | Path,
        triage: Any = None,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        template_dir = Path(__file__).parent / "templates"
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(template_dir)),
            autoescape=True,
        )
        template = env.get_template("report_base.html")
        html = template.render(
            plan=plan_result,
            rca=rca_report,
            comp=comparison,
            triage=triage.to_dict() if triage is not None else None,
        )
        output_path.write_text(html, encoding="utf-8")
        return output_path
