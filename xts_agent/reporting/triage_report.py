"""Self-contained HTML triage dashboard: groups, history, owners, AI root cause, source, Jira.

One page per triage run, written next to the triage JSON. No CDN, no
external assets, so it opens from a file share or a CI artifact on an
air-gapped lab host. Source snippets for AI ``code_refs`` are read from the
local tree at report time, so the page shows the lines the model saw.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import jinja2

logger = logging.getLogger(__name__)

CARD_LIMIT = 25           # groups shown as full cards; the rest go in a compact table
SNIPPET_LINES = 28        # source lines shown per code_ref
MAX_SOURCE_BYTES = 2 * 1024 * 1024
TEAM_BARS = 8             # teams charted individually; the tail folds into "other"

# Fixed category set (see triage.ai_rca.CLASSIFICATIONS) with the names shown to people
CATEGORY_LABELS = {
    "PRODUCT_BUG": "Product bug (actual failure)",
    "TEST_BUG": "Test / suite issue",
    "FLAKY_TEST": "Flaky test",
    "INFRASTRUCTURE_FAILURE": "Infrastructure (host, adb, TradeFed)",
    "ENVIRONMENT_ISSUE": "Environment (lab / device setup)",
    "UNKNOWN": "Unclassified",
}
CATEGORY_SHORT = {
    "Product bug (actual failure)": "Product bug",
    "Test / suite issue": "Test / suite issue",
    "Flaky test": "Flaky test",
    "Infrastructure (host, adb, TradeFed)": "Infrastructure",
    "Environment (lab / device setup)": "Environment",
}
MINUTES_PER_ITEM = 5      # manual effort assumption shown next to the estimate


def category_label(code: Optional[str]) -> str:
    return CATEGORY_LABELS.get(str(code or "").upper(), str(code or "Unclassified"))


def _snippet(ref: str) -> Optional[Dict[str, Any]]:
    """{file, start, lines:[(n, text)]} for a "path:line" code_ref, None if unreadable."""
    path_str, _, line_str = ref.rpartition(":")
    if not path_str or not line_str.isdigit():
        return None
    path = Path(path_str)
    short = "/".join(path.parts[-4:])
    start = max(1, int(line_str))
    try:
        if path.is_file() and path.stat().st_size <= MAX_SOURCE_BYTES:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            chunk = lines[start - 1 : start - 1 + SNIPPET_LINES]
            if chunk:
                return {
                    "file": str(path),
                    "name": path.name,
                    "short": short,
                    "start": start,
                    "lines": [(start + i, text) for i, text in enumerate(chunk)],
                }
    except OSError:
        pass

    return None


def _bars(counter: Counter, limit: Optional[int] = None, fold: str = "other") -> List[Dict[str, Any]]:
    """Sorted bar rows with widths as a percentage of the largest value."""
    items = counter.most_common()
    if limit and len(items) > limit:
        head, tail = items[: limit - 1], items[limit - 1 :]
        items = head + [(fold, sum(v for _, v in tail))]
    top = max((v for _, v in items), default=0) or 1
    total = sum(v for _, v in items) or 1
    return [
        {"label": k, "value": v, "pct": round(100 * v / top, 1), "share": round(100 * v / total)}
        for k, v in items
    ]


class TriageHTMLReport:
    def build_context(
        self,
        report: Any,
        jira_preview: Optional[List[Dict[str, Any]]] = None,
        run_info: Optional[Dict[str, Any]] = None,
        run_stats: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """``run_info``: devices/duration/pass/skip of this run. ``run_stats``: history rows,
        oldest first, the last being this run (see FailureHistory.run_stats)."""
        data = report.to_dict() if hasattr(report, "to_dict") else dict(report)
        groups: List[Dict[str, Any]] = list(data.get("groups") or [])
        summary = dict(data.get("summary") or {})
        failures = int(summary.get("failures") or sum(int(g["count"]) for g in groups))
        n_groups = int(summary.get("groups") or len(groups))

        by_team_failures: Counter = Counter()
        by_class: Counter = Counter()          # groups per category
        by_class_failures: Counter = Counter()  # failing tests per category
        by_ai_class: Counter = Counter()       # the AI's suggested category, where it ran
        by_label_failures: Counter = Counter()
        for g in groups:
            by_team_failures[g["owner"]["team"]] += int(g["count"])
            by_class[category_label(g["classification"])] += 1
            by_class_failures[category_label(g["classification"])] += int(g["count"])
            by_label_failures[g["label"]] += int(g["count"])
            if g.get("ai"):
                by_ai_class[category_label(g["ai"].get("classification"))] += 1

        top10 = sum(int(g["count"]) for g in groups[:10])
        known_failures = sum(int(g["count"]) for g in groups if g.get("known_issue"))
        waived_failures = sum(
            int(g["count"]) for g in groups if (g.get("known_issue") or {}).get("waived")
        )
        new_groups = sum(1 for g in groups if g["label"] == "NEW")
        new_failures = sum(int(g["count"]) for g in groups if g["label"] == "NEW")

        previous = None
        stats = [r for r in (run_stats or []) if r.get("failures") is not None]
        if len(stats) >= 2:
            prev, cur = stats[-2], stats[-1]
            prev_f, cur_f = int(prev["failures"]), int(cur["failures"])
            previous = {
                "failures": prev_f,
                "signatures": int(prev.get("signatures") or 0),
                "delta": cur_f - prev_f,
                "pct": round(100 * (cur_f - prev_f) / prev_f) if prev_f else 0,
                "when": datetime.fromtimestamp(int(prev["started_ms"]) / 1000).strftime("%Y-%m-%d")
                if prev.get("started_ms") else "",
            }
        run = None
        if run_info:
            run = {
                "devices": int(run_info.get("devices") or 0),
                "hours": round(float(run_info.get("duration") or 0) / 3600, 1),
                "passed": int(run_info.get("passed") or 0),
                "skipped": int(run_info.get("skipped") or 0),
            }
        effort = {
            "minutes": MINUTES_PER_ITEM,
            "manual_h": round(failures * MINUTES_PER_ITEM / 60),
            "grouped_h": round(max(n_groups - int(summary.get("waived_groups") or 0), 0) * MINUTES_PER_ITEM / 60),
        }
        ai_groups = [g for g in groups if g.get("ai")]
        ai_models = sorted({str(g["ai"].get("model", "")) for g in ai_groups if g["ai"].get("model")})

        cards = []
        rest = []
        for i, g in enumerate(groups):
            item = dict(g)
            item["index"] = i + 1
            item["share"] = round(100 * int(g["count"]) / (failures or 1), 1)
            item["category"] = category_label(g["classification"])
            cls_code = str(g.get("classification") or "").upper()
            if i < CARD_LIMIT or cls_code in ("PRODUCT_BUG", "TEST_BUG", "INFRASTRUCTURE_FAILURE"):
                ai = g.get("ai") or {}
                item["snippets"] = [s for s in (_snippet(r) for r in ai.get("code_refs") or []) if s]
                item["confidence_pct"] = round(100 * float(ai.get("confidence") or 0)) if ai else 0
                item["ai_category"] = category_label(ai.get("classification")) if ai else ""
                item["ai_class_code"] = str(ai.get("classification") or "").upper() if ai else ""
                cards.append(item)
            else:
                rest.append(item)

        preview = list(jira_preview or [])
        tickets = []
        for t in preview:
            tickets.append({
                "summary": t.get("summary", ""),
                "component": ", ".join(c.get("name", "") for c in t.get("components") or []),
                "labels": [lbl for lbl in t.get("labels") or []],
                "assignee": (t.get("assignee") or {}).get("name") or (t.get("assignee") or {}).get("accountId") or "",
            })

        teams = sorted({g["owner"]["team"] for g in groups})
        classes = sorted(by_class)
        label_to_code = {v: k for k, v in CATEGORY_LABELS.items()}
        categories = [
            {"code": code, "label": label, "groups": by_class.get(label, 0),
             "failures": by_class_failures.get(label, 0), "ai_groups": by_ai_class.get(label, 0)}
            for code, label in CATEGORY_LABELS.items() if code != "UNKNOWN"
        ]
        return {
            "plan_name": data.get("plan_name", ""),
            "profile": data.get("profile", ""),
            "fingerprint": data.get("fingerprint", ""),
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "failures": failures,
            "n_groups": n_groups,
            "reduction": round(failures / n_groups, 1) if n_groups else 0,
            "top10": top10,
            "top10_pct": round(100 * top10 / failures) if failures else 0,
            "actionable": int(summary.get("actionable_groups") or 0),
            "known_groups": int(summary.get("known_groups") or 0),
            "known_failures": known_failures,
            "waived_groups": int(summary.get("waived_groups") or 0),
            "ai_groups": len(ai_groups),
            "ai_models": ai_models,
            "ai_agreement": summary.get("ai_agreement") or {},
            "tickets": tickets,
            "team_bars": _bars(by_team_failures, TEAM_BARS),
            "class_bars": [
                {
                    **b,
                    "code": label_to_code.get(b["label"], "UNKNOWN"),
                    "label": CATEGORY_SHORT.get(b["label"], b["label"]),
                }
                for b in _bars(by_class_failures)
            ],
            "waived_failures": waived_failures,
            "signal_failures": failures - waived_failures,
            "new_groups": new_groups,
            "new_failures": new_failures,
            "previous": previous,
            "run": run,
            "effort": effort,
            "categories": categories,
            "label_bars": _bars(by_label_failures),
            "cards": cards,
            "rest": rest,
            "teams": teams,
            "classes": classes,
            "label_order": [lbl for lbl in ("NEW", "FLAKY", "PERSISTENT", "NO_HISTORY") if lbl in by_label_failures],
        }

    def generate(
        self,
        report: Any,
        output_path: str | Path,
        jira_preview: Optional[List[Dict[str, Any]]] = None,
        run_info: Optional[Dict[str, Any]] = None,
        run_stats: Optional[List[Dict[str, Any]]] = None,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")),
            autoescape=True,
        )
        context = self.build_context(report, jira_preview, run_info, run_stats)
        html = env.get_template("triage_report.html").render(**context)
        output_path.write_text(html, encoding="utf-8")
        return output_path
