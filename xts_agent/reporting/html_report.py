"""Rich HTML report using Jinja2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2

from .triage_report import TriageHTMLReport, _snippet, category_label


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
        triage_dict = None
        triage_ctx = None
        if triage is not None:
            triage_dict = triage.to_dict() if hasattr(triage, "to_dict") else dict(triage)
            triage_ctx = TriageHTMLReport().build_context(triage_dict)
            failures = int((triage_dict.get("summary") or {}).get("failures") or 1)
            for idx, g in enumerate(triage_dict.get("groups") or []):
                g["index"] = idx + 1
                g["share"] = round(100 * int(g.get("count") or 0) / failures, 1)
                g["category_label"] = category_label(g.get("classification"))
                ai = g.get("ai") or {}
                if ai:
                    g["confidence_pct"] = round(100 * float(ai.get("confidence") or 0))
                    g["ai_category_label"] = category_label(ai.get("classification"))
                    if idx < 30:
                        g["snippets"] = [
                            s for s in (_snippet(r) for r in ai.get("code_refs") or []) if s
                        ]

        template = env.get_template("report_base.html")
        html = template.render(
            plan=plan_result,
            rca=rca_report,
            comp=comparison,
            triage=triage_dict,
            triage_ctx=triage_ctx,
        )
        output_path.write_text(html, encoding="utf-8")
        return output_path

