"""Fix-loop HTML page: one card per target with its iteration timeline."""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

import jinja2

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "FIXED": "Fixed",
    "FIXED_IN_CODE": "Fixed in OEM code",
    "WORKAROUND_VERIFIED": "Root cause proven",
    "ALREADY_PASSING": "Already passing",
    "FLAKY": "Flaky",
    "NEEDS_CODE_FIX": "Needs code fix",
    "NO_FIX_FOUND": "No fix found",
    "DEVICE_LOST": "Device lost",
    "ABORTED": "Aborted",
    "ERROR": "Error",
    "RUNNING": "Running",
}
KIND_LABELS = {"baseline": "Baseline", "remediate": "Fix", "retry": "Re-run", "confirm": "Confirm",
               "failover": "DUT failover", "not_applied": "Not applied", "code_fix": "Patch · build · flash",
               "verify_fix": "Re-run on new build", "restore": "Restore original build"}
NOT_RUNS = ("failover", "not_applied", "code_fix", "restore")


def load_sessions(paths: Iterable[str | Path]) -> List[Dict[str, Any]]:
    sessions = []
    for p in paths:
        try:
            sessions.append(json.loads(Path(p).read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable session %s: %s", p, exc)
    return sessions


def _secs(iso_a: Any, iso_b: Any) -> int:
    try:
        return int((datetime.fromisoformat(iso_b) - datetime.fromisoformat(iso_a)).total_seconds())
    except (TypeError, ValueError):
        return 0


def _fmt_dur(secs: float) -> str:
    secs = int(secs or 0)
    return f"{secs // 3600}h {secs % 3600 // 60:02d}m" if secs >= 3600 else f"{secs // 60}m {secs % 60:02d}s"


def _view(session: Dict[str, Any]) -> Dict[str, Any]:
    steps = []
    for it in session.get("iterations") or []:
        out = it.get("outcome") or {}
        passed, failed, not_run = out.get("passed", 0), out.get("failed", 0), out.get("not_run_modules", 0)
        total = max(passed + failed + not_run, 1)
        rem = it.get("remediation") or {}
        steps.append({
            **it,
            "label": KIND_LABELS.get(it.get("kind"), it.get("kind", "")),
            "title": rem.get("id") or "",
            "passed": passed,
            "failed": failed,
            "not_run": not_run,
            # assumption failures / ignored: skips, neither pass nor fail
            "skipped": max(len(out.get("tests") or {}) - passed - failed, 0),
            "pass_pct": round(100 * passed / total),
            "fail_pct": round(100 * failed / total),
            "notrun_pct": round(100 * not_run / total),
            "green": out.get("green", False),
            # TradeFed stores some messages already XML-escaped (&lt;true&gt;)
            "failures": [dict(f, message=html.unescape(f.get("message") or "")) for f in (out.get("failures") or [])[:6]],
            "results_dir": out.get("results_dir", ""),
            "log_path": out.get("log_path", ""),
            "duration": _fmt_dur(out.get("duration_secs", 0)),
            "build_time": _fmt_dur((it.get("build") or {}).get("secs", 0)),
            "flash_time": _fmt_dur((it.get("deploy") or {}).get("secs", 0)),
            "patch_files": [f.split("/")[-1] for f in (it.get("patch") or {}).get("files") or []],
        })
    diag_ai = next((s.get("diagnosis") for s in steps if (s.get("diagnosis") or {}).get("ai")), None) or {}
    oem = dict((diag_ai.get("oem") or {})) if _product_side(session, diag_ai) else {}
    if oem.get("hits"):
        # Hits the LLM's answer refers to (file:line) go first and are marked
        said = " ".join(str((diag_ai.get("ai") or {}).get(k, "")) for k in ("root_cause", "suggested_fix", "evidence"))
        hits = [dict(h, cited=f"{h['file']}:{h['line']}" in said or (h["file"] in said and h["line"] == 0))
                for h in oem["hits"]]
        oem["hits"] = sorted(hits, key=lambda h: not h["cited"])
    first_diag = next((s.get("diagnosis") for s in steps if s.get("diagnosis")), None) or {}
    patched_files = {f for st in steps for f in (st.get("patch") or {}).get("files") or []
                     if st.get("kind") == "code_fix" and st.get("verdict") == "flashed"}
    if oem.get("hits") and patched_files and session.get("status") == "FIXED_IN_CODE":
        oem["hits"] = sorted((dict(h, patched=h["file"] in patched_files) for h in oem["hits"]),
                             key=lambda h: not h["patched"])
    # The patch that made it green: the last one flashed before a FIXED_IN_CODE end
    flashed = [st for st in steps if st.get("kind") == "code_fix" and st.get("verdict") == "flashed"]
    kept = flashed[-1]["patch"] if flashed and session.get("status") == "FIXED_IN_CODE" else None
    return {
        **session,
        "status_label": STATUS_LABELS.get(str(session.get("status")), session.get("status")),
        "steps": steps,
        "runs": sum(1 for st in steps if st.get("kind") not in NOT_RUNS and st.get("verdict") != "device_lost"),
        "builds": sum(1 for st in steps if st.get("build")),
        "patch": kept,
        "patch_attempts": [st for st in steps if st.get("kind") == "code_fix"],
        "duration": _fmt_dur(_secs(session.get("started"), session.get("finished"))),
        "baseline": next((st for st in steps if st.get("kind") == "baseline" and st.get("verdict") != "device_lost"), None),
        "final": next((st for st in reversed(steps) if st.get("outcome") and st.get("verdict") != "device_lost"), None),
        "ai": diag_ai.get("ai") or {},
        # OEM code matters when the cause is product code; for lab/host fixes
        # it is noise (it stays in the session JSON)
        "oem": oem,
        "device_state": first_diag.get("device") or {},
    }


def _product_side(session: Dict[str, Any], diag: Dict[str, Any]) -> bool:
    return session.get("status") in ("WORKAROUND_VERIFIED", "NEEDS_CODE_FIX", "FIXED_IN_CODE") or (
        (diag.get("ai") or {}).get("classification") == "PRODUCT_BUG"
    )


def _wall_secs(sessions: List[Dict[str, Any]]) -> int:
    """Time the loop was actually running: the union of session intervals.

    Sessions on different DUTs overlap, and sessions from separate
    invocations leave idle gaps; neither should inflate the figure.
    """
    spans = []
    for s in sessions:
        try:
            spans.append((datetime.fromisoformat(str(s["started"])), datetime.fromisoformat(str(s["finished"]))))
        except (KeyError, ValueError):
            continue
    total, end = 0.0, None
    for a, b in sorted(spans):
        if end is None or a > end:
            total += (b - a).total_seconds()
            end = b
        elif b > end:
            total += (b - end).total_seconds()
            end = b
    return int(total)


def render(sessions: List[Dict[str, Any]], out_path: str | Path, title: str = "xTS fix loop",
           model: str = "") -> Path:
    views = [_view(s) for s in sessions]
    counts: Dict[str, int] = {}
    for v in views:
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    total_secs = _wall_secs(sessions)
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(Path(__file__).parent.parent / "reporting" / "templates")),
        autoescape=jinja2.select_autoescape(["html"]),
    )
    html = env.get_template("fixloop_report.html").render(
        title=title,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        sessions=views,
        counts=counts,
        runs=sum(v["runs"] for v in views),
        builds=sum(v["builds"] for v in views),
        total_duration=_fmt_dur(total_secs),
        fingerprint=next((s.get("fingerprint") for s in sessions if s.get("fingerprint")), ""),
        devices=sorted({s.get("serial", "") for s in sessions}),
        model=model,
        labels=STATUS_LABELS,
    )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
