"""Self-contained trends dashboard generated from the results database.

No CDN assets: inline CSS and server-rendered SVG charts, so it renders in
air-gapped labs and as a CI artifact. Regenerated after every run and by
``xts-agent dashboard``.
"""

from __future__ import annotations

import datetime as _dt
import html
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

WAIVER_WARN_DAYS = 14


def _short_build(fingerprint: str) -> str:
    """'brand/product/device:rel/BUILD_ID/incr:type/keys' -> 'BUILD_ID/incr'."""
    try:
        parts = fingerprint.split(":")[1].split("/")
        return f"{parts[1]}/{parts[2]}"
    except (IndexError, AttributeError):
        return fingerprint or "-"


def collect(store: Any, history: Any = None, known_issues: Any = None, ledger: Any = None,
            limit: int = 30) -> Dict[str, Any]:
    """Gather dashboard data (separate from rendering so it can be tested)."""
    runs = list(reversed(store.recent_runs(limit=limit * 6)))  # oldest first
    suites: Dict[str, List[Dict[str, Any]]] = {}
    for r in runs:
        total = r["pass_count"] + r["fail_count"]
        suites.setdefault(r["suite"], []).append({
            "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["recorded_at"])),
            "build": _short_build(r["fingerprint"]),
            "status": r["status"],
            "pass_rate": round(r["pass_count"] / total * 100, 2) if total else None,
            "fails": r["fail_count"],
            "modules": f"{r['modules_done']}/{r['modules_total']}" if r["modules_total"] else "-",
            "hours": round(r["duration"] / 3600, 1),
            "devices": r["device_count"],
            "profile": r["profile"],
        })
    for name in suites:
        suites[name] = suites[name][-limit:]

    data: Dict[str, Any] = {"suites": suites, "triage_runs": [], "flaky": {}, "waivers": [], "quarantine": {}}
    if history is not None:
        data["triage_runs"] = [
            {**r, "build": _short_build(r["fingerprint"]),
             "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["started_ms"] / 1000))}
            for r in history.run_stats(limit)
        ]
        data["flaky"] = {s: history.flaky_tests(s) for s in suites}
    if known_issues is not None:
        today = _dt.date.today()
        for issue in known_issues.issues:
            if issue.waiver:
                days = (issue.waiver.expires - today).days
                data["waivers"].append({
                    "id": issue.id, "title": issue.title, "jira": issue.jira,
                    "expires": issue.waiver.expires.isoformat(), "days_left": days,
                    "profiles": ", ".join(issue.waiver.profiles),
                })
        data["waivers"].sort(key=lambda w: w["days_left"])
    if ledger is not None:
        data["quarantine"] = ledger.quarantined()
    return data


def _svg_line(values: Sequence[Optional[float]], width: int = 520, height: int = 120,
              ymin: Optional[float] = None, ymax: Optional[float] = None, color: str = "#2563eb") -> str:
    points = [(i, v) for i, v in enumerate(values) if v is not None]
    if not points:
        return '<p class="muted">No data yet.</p>'
    lo = min(v for _, v in points) if ymin is None else ymin
    hi = max(v for _, v in points) if ymax is None else ymax
    hi = hi if hi > lo else lo + 1
    n = max(len(values) - 1, 1)
    pad = 24

    def xy(i: int, v: float) -> str:
        x = pad + (width - 2 * pad) * i / n
        y = height - pad - (height - 2 * pad) * (v - lo) / (hi - lo)
        return f"{x:.1f},{y:.1f}"

    path = " ".join(xy(i, v) for i, v in points)
    dots = "".join(
        f'<circle cx="{xy(i, v).split(",")[0]}" cy="{xy(i, v).split(",")[1]}" r="3" fill="{color}">'
        f"<title>{v}</title></circle>"
        for i, v in points
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">'
        f'<text x="2" y="{pad - 8}" class="axis">{hi:g}</text>'
        f'<text x="2" y="{height - 6}" class="axis">{lo:g}</text>'
        f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" class="grid"/>'
        f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{path}"/>{dots}</svg>'
    )


_CSS = """
body{font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1f2937}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
.muted{color:#6b7280}.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin:12px 0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eef0f3}
th{color:#6b7280;font-weight:600}.wrap{overflow-x:auto}
.chart{width:100%;height:auto}.axis{font-size:10px;fill:#9ca3af}.grid{stroke:#e5e7eb}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600}
.PASSED{background:#dcfce7;color:#166534}.FAILED{background:#fee2e2;color:#991b1b}
.INCOMPLETE{background:#fef3c7;color:#92400e}.bad{color:#b91c1c;font-weight:600}.warn{color:#b45309;font-weight:600}
"""


def render(data: Dict[str, Any]) -> str:
    e = html.escape
    out = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>xTS trends</title><style>{_CSS}</style></head><body><main>",
           "<h1>xTS trends</h1>",
           f"<p class='muted'>Generated {time.strftime('%Y-%m-%d %H:%M')} from the results database.</p>"]

    out.append("<h2>Suites</h2>")
    if not data["suites"]:
        out.append("<p class='muted'>No runs recorded yet.</p>")
    for suite, rows in sorted(data["suites"].items()):
        out.append(f"<div class='card'><h3>{e(suite)}</h3><div class='grid2'>")
        out.append("<div><div class='muted'>Pass rate %</div>"
                   + _svg_line([r["pass_rate"] for r in rows], ymin=0, ymax=100, color="#16a34a") + "</div>")
        out.append("<div><div class='muted'>Failing tests</div>"
                   + _svg_line([r["fails"] for r in rows], ymin=0, color="#dc2626") + "</div>")
        out.append("</div><div class='wrap'><table><tr><th>When</th><th>Build</th><th>Status</th><th>Pass %</th>"
                   "<th>Fails</th><th>Modules</th><th>Hours</th><th>Devices</th><th>Profile</th></tr>")
        for r in reversed(rows[-12:]):
            out.append(
                f"<tr><td>{e(r['when'])}</td><td>{e(r['build'])}</td>"
                f"<td><span class='pill {e(r['status'])}'>{e(r['status'])}</span></td>"
                f"<td>{'' if r['pass_rate'] is None else r['pass_rate']}</td><td>{r['fails']}</td>"
                f"<td>{e(r['modules'])}</td><td>{r['hours']}</td><td>{r['devices']}</td><td>{e(r['profile'] or '')}</td></tr>"
            )
        out.append("</table></div>")
        flaky = data["flaky"].get(suite) or []
        if flaky:
            items = "".join(f"<li>{e(t)}</li>" for t in flaky[:25])
            more = f"<li class='muted'>... and {len(flaky) - 25} more</li>" if len(flaky) > 25 else ""
            out.append(f"<details><summary>{len(flaky)} flaky test(s) in recent runs</summary><ul>{items}{more}</ul></details>")
        out.append("</div>")

    if data["triage_runs"]:
        tr = data["triage_runs"]
        out.append("<h2>Root causes per run</h2><div class='card'><div class='grid2'>")
        out.append("<div><div class='muted'>Failing tests</div>" + _svg_line([r["failures"] for r in tr], ymin=0, color="#dc2626") + "</div>")
        out.append("<div><div class='muted'>Distinct root-cause groups</div>" + _svg_line([r["signatures"] for r in tr], ymin=0, color="#7c3aed") + "</div>")
        out.append("</div><div class='wrap'><table><tr><th>When</th><th>Suite</th><th>Build</th><th>Failures</th><th>Groups</th></tr>")
        for r in reversed(tr[-12:]):
            out.append(f"<tr><td>{e(r['when'])}</td><td>{e(r['suite'])}</td><td>{e(r['build'])}</td>"
                       f"<td>{r['failures']}</td><td>{r['signatures']}</td></tr>")
        out.append("</table></div></div>")

    out.append("<h2>Waivers</h2><div class='card wrap'>")
    if data["waivers"]:
        out.append("<table><tr><th>Issue</th><th>Jira</th><th>Expires</th><th>Profiles</th></tr>")
        for w in data["waivers"]:
            cls = "bad" if w["days_left"] < 0 else ("warn" if w["days_left"] <= WAIVER_WARN_DAYS else "")
            when = "expired" if w["days_left"] < 0 else f"in {w['days_left']} d"
            out.append(f"<tr><td>{e(w['id'])} {e(w['title'])}</td><td>{e(w['jira'])}</td>"
                       f"<td class='{cls}'>{e(w['expires'])} ({when})</td><td>{e(w['profiles'])}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class='muted'>No waivers.</p>")
    out.append("</div>")

    out.append("<h2>Quarantined devices</h2><div class='card'>")
    if data["quarantine"]:
        out.append("<ul>" + "".join(f"<li><b>{e(s)}</b>: {e(r)}</li>" for s, r in data["quarantine"].items()) + "</ul>")
    else:
        out.append("<p class='muted'>None.</p>")
    out.append("</div></main></body></html>")
    return "".join(out)


def write_dashboard(path: str | Path, data: Dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data), encoding="utf-8")
    return path
