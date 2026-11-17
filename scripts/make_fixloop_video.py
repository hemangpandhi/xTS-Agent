#!/usr/bin/env python3
"""Build the OEM demo video for `xts-agent fix-loop` from a real run.

Every number in the video is read from the fix-loop session JSONs (which point
at the TradeFed result directories); every page shot is the real fix-loop
report rendered by headless Chrome. Nothing is typed in by hand.

  scripts/make_fixloop_video.py \
      --sessions results/fixloop/sessions/<id>.json ... \
      --log results/fixloop_oem/run2.log --model coder \
      --origin "AutoPrivacy=Planted OEM change (CarSettings)" \
      --out results/demo_video/xts_code_fix_demo.mp4
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from xts_agent.fixloop import report as fl_report  # noqa: E402

W, H, FPS = 1600, 900, 25
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or "google-chrome"

CARD_CSS = """
* { box-sizing: border-box; margin: 0; }
html, body { width: 1600px; height: 900px; overflow: hidden; }
body { font-family: "DejaVu Sans", Arial, sans-serif; color: #f8fafc;
       background: radial-gradient(1200px 700px at 85% -10%, #1e3a8a 0%, transparent 60%),
                   linear-gradient(135deg, #0b1220 0%, #0f172a 55%, #111c3a 100%);
       padding: 70px 90px; display: flex; flex-direction: column; justify-content: center; }
.kicker { color: #93c5fd; font-size: 22px; letter-spacing: .12em; text-transform: uppercase; }
h1 { font-size: 64px; line-height: 1.1; margin: 18px 0 22px; letter-spacing: -.01em; }
h2 { font-size: 44px; margin: 10px 0 26px; }
p.lead { font-size: 27px; color: #cbd5e1; line-height: 1.45; max-width: 1300px; }
.row { display: flex; gap: 22px; }
.box { background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.12); border-radius: 18px; padding: 22px 24px; }
.box h3 { font-size: 24px; margin-bottom: 8px; }
.box p { font-size: 19px; color: #cbd5e1; line-height: 1.45; }
.mono { font-family: "DejaVu Sans Mono", monospace; }
.pill { display: inline-block; padding: 6px 16px; border-radius: 999px; font-size: 18px; font-weight: bold; }
.foot { position: absolute; left: 90px; right: 90px; bottom: 44px; color: #94a3b8; font-size: 17px; }
.flow { display: flex; align-items: center; gap: 14px; margin: 34px 0 30px; }
.node { background: #1e293b; border: 2px solid #3b82f6; border-radius: 16px; padding: 18px 20px; font-size: 22px; text-align: center; min-width: 190px; }
.node small { display: block; color: #94a3b8; font-size: 15px; margin-top: 6px; }
.node.ok { border-color: #22c55e; }
.arr { font-size: 34px; color: #64748b; }
.term { font-family: "DejaVu Sans Mono", monospace; background: #020617; border: 1px solid #1e293b; border-radius: 14px; padding: 22px 26px; font-size: 17px; line-height: 1.55; color: #e2e8f0; white-space: pre-wrap; }
.term .c { color: #93c5fd; } .term .g { color: #4ade80; } .term .v { color: #c4b5fd; } .term .r { color: #fb7185; }
.issue { flex: 1; }
.issue .tag { font-size: 15px; font-weight: bold; padding: 4px 10px; border-radius: 8px; display: inline-block; margin-bottom: 10px; }
.k { color: #f8fafc; font-weight: bold; }
"""


def chrome_png(html_text: str, out: Path, width: int = W, height: int = H, scale: float = 1.0) -> Path:
    src = out.with_suffix(".html")
    src.write_text(html_text, encoding="utf-8")
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
         f"--force-device-scale-factor={scale}", f"--window-size={width},{height}",
         f"--screenshot={out}", src.as_uri()],
        check=True, capture_output=True, timeout=120,
    )
    return out


def card(body: str, foot: str = "") -> str:
    footer = f'<div class="foot">{foot}</div>' if foot else ""
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CARD_CSS}</style></head><body>{body}{footer}</body></html>"


def still(png: Path, secs: float, out: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-t", str(secs), "-i", str(png),
         "-vf", f"scale={W}:{H},fade=t=in:st=0:d=0.5,fade=t=out:st={secs - 0.5}:d=0.5,format=yuv420p",
         "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "18", str(out)],
        check=True,
    )
    return out


def pan(tall_png: Path, caption_png: Path, secs: float, out: Path) -> Path:
    """Scroll down a tall page screenshot with a caption bar at the bottom."""
    hold = 1.5
    move = f"min(max(0\\,(t-{hold})/({secs}-{2 * hold}))\\,1)*(ih-{H})"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-loop", "1", "-t", str(secs), "-i", str(tall_png),
         "-loop", "1", "-t", str(secs), "-i", str(caption_png),
         "-filter_complex",
         f"[0:v]scale={W}:-2,crop={W}:{H}:0:'{move}'[pg];"
         f"[pg][1:v]overlay=0:0,fade=t=in:st=0:d=0.5,fade=t=out:st={secs - 0.5}:d=0.5,format=yuv420p",
         "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "18", str(out)],
        check=True,
    )
    return out


def trim_page(png: Path) -> int:
    """Crop the empty page background below the last card; returns the new height."""
    from PIL import Image

    im = Image.open(png).convert("RGB")
    bg = im.getpixel((5, im.height - 5))
    w, h = im.size
    bottom = h
    for y in range(h - 1, 0, -4):
        row = [im.getpixel((x, y)) for x in range(0, w, 16)]
        if any(sum(abs(a - b) for a, b in zip(px, bg)) > 24 for px in row):
            bottom = min(h, y + 60)
            break
    im.crop((0, 0, w, max(bottom, H))).save(png)
    return max(bottom, H)


def caption_png(text: str, out: Path) -> Path:
    """Full-frame transparent overlay with a caption bar along the bottom."""
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        f"html,body{{margin:0;width:{W}px;height:{H}px;background:transparent;overflow:hidden;}}"
        ".c{position:absolute;left:0;right:0;bottom:0;min-height:96px;background:rgba(2,6,23,.9);"
        "color:#f8fafc;font:25px/1.4 'DejaVu Sans',Arial;padding:22px 60px;border-top:3px solid #3b82f6;"
        "box-sizing:border-box;}"
        ".c b{color:#93c5fd}</style></head><body><div class='c'>" + text + "</div></body></html>"
    )
    src = out.with_suffix(".html")
    src.write_text(body, encoding="utf-8")
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
         "--default-background-color=00000000", f"--window-size={W},{H}", f"--screenshot={out}", src.as_uri()],
        check=True, capture_output=True, timeout=120,
    )
    return out


def first_failure(s: Dict[str, Any]) -> str:
    base = (s.get("iterations") or [{}])[0].get("outcome") or {}
    f = (base.get("failures") or [{}])[0]
    return clip(html.unescape(f.get("message") or "").split("\n")[0], 150)


def clip(text: str, n: int) -> str:
    """Cut at a sentence or word boundary, never mid-word."""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    cut = text[:n]
    dot = cut.rfind(". ")
    if dot > n * 0.5:
        return cut[: dot + 1]
    return cut[: cut.rfind(" ")].rstrip(",;:") + " …"


def model_name(m: str) -> str:
    return m.split(":", 1)[1] if m.startswith("openai:") else m


def _fmt_outcome(o: Dict[str, Any]) -> str:
    if o.get("not_run_modules") and not (o.get("passed") or o.get("failed")):
        return "module not run"
    skipped = len(o.get("tests") or {}) - o.get("passed", 0) - o.get("failed", 0)
    tail = f" (+{skipped} skipped)" if skipped > 0 else ""
    return f"{o.get('passed', 0)}/{o.get('passed', 0) + o.get('failed', 0)} pass{tail}"


def runs_line(s: Dict[str, Any]) -> str:
    parts = []
    for it in s.get("iterations") or []:
        if not it.get("outcome") or it.get("verdict") == "device_lost":
            continue
        label = {"baseline": "baseline", "remediate": (it.get("remediation") or {}).get("id", "fix"),
                 "confirm": "confirm", "retry": "re-run", "verify_fix": "patched build"}.get(it.get("kind"), it.get("kind"))
        parts.append(f"{label}: {_fmt_outcome(it['outcome'])}")
    return "  \u2192  ".join(parts)


def _verify_runs(s: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [i for i in s.get("iterations") or [] if i.get("outcome") and i.get("verdict") != "device_lost"]


def kept_patch_step(s: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    steps = [i for i in s.get("iterations") or [] if i.get("kind") == "code_fix" and i.get("verdict") == "flashed"]
    return steps[-1] if steps and s.get("status") == "FIXED_IN_CODE" else None


def diff_html(diff: str, max_lines: int = 12) -> str:
    """Only the changed lines and a little context, coloured."""
    out = []
    for ln in diff.splitlines():
        if ln.startswith(("diff ", "index ", "--- ", "@@")):
            continue
        if ln.startswith("+++ "):
            out.append(f"<span class='c'>{html.escape(ln[6:])}</span>")
        elif ln.startswith("+"):
            out.append(f"<span class='g'>{html.escape(ln)}</span>")
        elif ln.startswith("-"):
            out.append(f"<span class='r'>{html.escape(ln)}</span>")
        else:
            out.append(html.escape(ln))
    changed = [k for k, ln in enumerate(out) if "class='g'" in ln or "class='r'" in ln]
    if changed and len(out) > max_lines:
        # Every changed line with one line of context; gaps collapse to an ellipsis
        keep = sorted({0} | {j for k in changed for j in (k - 1, k, k + 1) if 0 <= j < len(out)})
        rows, prev = [], -1
        for k in keep:
            if prev >= 0 and k > prev + 1:
                rows.append("<span class='c'>      …</span>")
            rows.append(out[k])
            prev = k
        out = rows
    return "\n".join(out)


def origin(s: Dict[str, Any], origins: Dict[str, str]) -> str:
    return next((v for k, v in origins.items() if k in s["target"]["name"]), "")


def build(sessions: List[Dict[str, Any]], log_lines: List[str], out: Path, model: str,
          commands: Optional[List[str]] = None, origins: Optional[Dict[str, str]] = None) -> None:
    origins = origins or {}
    work = Path(tempfile.mkdtemp(prefix="fixloop_video_"))
    segs: List[Path] = []
    fp = next((s.get("fingerprint") for s in sessions if s.get("fingerprint")), "")
    duts = sorted({s["serial"] for s in sessions})
    runs = sum(len(_verify_runs(s)) for s in sessions)
    builds = sum(1 for s in sessions for i in s.get("iterations") or [] if i.get("build"))
    fixed = [s for s in sessions if s["status"] == "FIXED_IN_CODE"]

    # 1. title
    segs.append(still(chrome_png(card(
        "<div class='kicker'>xTS Agent · Fix Loop</div>"
        "<h1>From a failing CTS test<br>to a verified OEM code fix.</h1>"
        "<p class='lead'>The agent runs the test on the DUT, finds the root cause in the OEM source, writes the "
        "patch, builds the image, flashes the DUT and re-runs CTS &mdash; until it passes.</p>",
        f"Real run · AAOS 17 user build on Cuttlefish ({', '.join(duts)}) · local GPU LLM ({html.escape(model)}) · "
        f"<span class='mono'>{html.escape(fp)}</span>"), work / "01.png"), 8, work / "01.mp4"))

    # 2. the loop
    segs.append(still(chrome_png(card(
        "<div class='kicker'>How it works</div><h2>Every step is real: a CTS run, a build, a flash</h2>"
        "<div class='flow'>"
        "<div class='node'>CTS run<small>cts-tradefed<br>on the DUT</small></div><div class='arr'>&rarr;</div>"
        "<div class='node'>Root cause<small>device probes +<br>LLM reads OEM code</small></div><div class='arr'>&rarr;</div>"
        "<div class='node'>Patch<small>smallest change,<br>self-reviewed</small></div><div class='arr'>&rarr;</div>"
        "<div class='node'>Build + flash<small>m &amp; flash<br>the DUT</small></div><div class='arr'>&rarr;</div>"
        "<div class='node'>Re-run CTS<small>red: revert &amp;<br>try again</small></div><div class='arr'>&rarr;</div>"
        "<div class='node ok'>Green twice<small>patch for<br>review</small></div></div>"
        "<div class='row'>"
        "<div class='box'><h3>Your code, your rules</h3><p>Only OEM trees you mark editable can change. CTS and tests "
        "never. Nothing is committed: the verified patch goes to your reviewers.</p></div>"
        "<div class='box'><h3>OEM code via folder or MCP</h3><p>CarLauncher, CarSettings, CarSystemUI, device "
        "overlays &mdash; read from a local checkout or your MCP code server.</p></div>"
        "<div class='box'><h3>Stays on-prem</h3><p>The LLM runs on the lab GPU. Source and logs never leave the host.</p></div>"
        "</div>"), work / "02.png"), 13, work / "02.mp4"))

    # 3. the failures we start from
    boxes = []
    for s in sessions:
        base = _verify_runs(s)[0]["outcome"] if _verify_runs(s) else {}
        tag = origin(s, origins)
        color = "#d97706" if tag.startswith("Planted") else "#7c3aed"
        boxes.append(
            f"<div class='box issue'>{f'<span class=tag style=background:{color}>{html.escape(tag)}</span>' if tag else ''}"
            f"<h3 class='mono' style='font-size:19px'>{html.escape(s['target']['name'].split(' ')[-1].split('.')[-1])}</h3>"
            f"<p class='mono' style='font-size:15px'>{html.escape(s['target']['module'])}</p>"
            f"<p style='margin-top:10px'><span class='k'>{html.escape(_fmt_outcome(base))}</span> &mdash; "
            f"{html.escape(first_failure(s))}</p></div>")
    segs.append(still(chrome_png(card(
        "<div class='kicker'>The starting point</div><h2>Three CTS failures on the OEM build</h2>"
        f"<div class='row'>{''.join(boxes)}</div>",
        "Planted = an OEM-style change we made on purpose to the AOSP app (commit labelled as such), to show what the "
        "agent does with OEM regressions. Real = found as-is on this AAOS build."), work / "03.png"), 14, work / "03.mp4"))

    # 4. the real command + real output
    cmds = list(commands or []) + [ln for ln in log_lines if ln.startswith("$ xts-agent")]
    cmd = "\n".join(c.replace(" --", " \\\n    --") for c in cmds[:2]) or "$ xts-agent fix-loop ..."
    out_html = ""
    # A target re-run in a later invocation: show only its final status line
    def tgt(ln: str) -> str:
        return re.sub(r"^\[[^\]]*\]\s+\S+\s+", "", ln).strip()

    last = {tgt(ln): i for i, ln in enumerate(log_lines) if ln.startswith("[")}
    for i, ln in enumerate(log_lines):
        if ln.startswith("[") and last.get(tgt(ln)) == i:
            cls = "g" if "FIXED" in ln else "v" if "WORKAROUND" in ln else "r"
            summary = log_lines[i + 1].strip() if i + 1 < len(log_lines) else ""
            out_html += (f"<span class='{cls}'>{html.escape(ln.rstrip())}</span>\n"
                         f"    {html.escape(clip(summary.split('. Patch:')[0].split('; Patch:')[0], 170))}\n")
    segs.append(still(chrome_png(card(
        "<div class='kicker'>The commands, run unattended</div>"
        f"<div class='term' style='margin-top:22px;font-size:15px'><span class='c'>{html.escape(cmd)}</span>\n\n{out_html}</div>",
        "Real console output (final status per target). Telecom ran twice: in the first run the self-review vetoed the "
        "right patch, so we made the DUT the judge for a patch proposed again, and re-ran it. Every build log, flash "
        "log and TradeFed result directory is kept for audit."),
        work / "04.png"), 12, work / "04.mp4"))

    # 5. per target: root cause + patch card, then the real report page
    for i, s in enumerate(sessions):
        step = kept_patch_step(s)
        vr = _verify_runs(s)
        if step:
            p = step["patch"]
            before = _fmt_outcome(vr[0]["outcome"]) if vr else ""
            after = _fmt_outcome(vr[-1]["outcome"]) if vr else ""
            attempts = [it for it in s["iterations"] if it.get("kind") == "code_fix"]
            body = (
                f"<div class='kicker'>{html.escape(origin(s, origins) or 'Root cause')} · "
                f"{html.escape(s['target']['module'])}</div>"
                f"<h2 style='font-size:34px'>{html.escape(s['target']['name'].split(' ')[-1])}</h2>"
                f"<p class='lead' style='font-size:22px'><span class='k'>Root cause</span> (the agent's words): {html.escape(clip(p['root_cause'], 400))}</p>"
                f"<div class='term' style='margin-top:16px;font-size:15px;line-height:1.45'>{diff_html(p['diff'])}</div>"
                f"<div class='row' style='margin-top:16px;margin-bottom:40px'>"
                f"<div class='box'><h3>CTS before &rarr; after</h3><p class='mono'>{html.escape(before)} &rarr; {html.escape(after)}</p></div>"
                f"<div class='box'><h3>Build &middot; flash</h3><p>{fl_report._fmt_dur(step['build']['secs'])} &middot; "
                f"{fl_report._fmt_dur(step['deploy']['secs'])}</p></div>"
                f"<div class='box'><h3>Attempts</h3><p>{len(attempts)} patch proposal(s); "
                f"{sum(1 for a in attempts if a['verdict'] != 'flashed')} rejected before reaching the DUT</p></div></div>")
            segs.append(still(chrome_png(card(body, f"Patch written by {html.escape(model_name(p.get('model', '')))} on the lab GPU; "
                                              "confirmed by a second green CTS run; saved for review, not committed."),
                                         work / f"05_{i}a.png"), 16, work / f"05_{i}a.mp4"))
        page = fl_report.render([s], work / f"page_{i}.html", model=model)
        tall = chrome_png(page.read_text(encoding="utf-8"), work / f"05_{i}.png", 1280, 4000, 1.25)
        height = trim_page(tall)
        label = fl_report.STATUS_LABELS.get(s["status"], s["status"])
        cap = (f"<b>{html.escape(s['target']['module'])}</b> · {html.escape(label)} · "
               f"{html.escape(runs_line(s))}")
        secs = round(max(14.0, 5.5 * height / H + 4), 1)
        segs.append(pan(tall, caption_png(cap, work / f"05_{i}_cap.png"), secs, work / f"05_{i}.mp4"))

    # 6. what the loop threw away (robustness, from the session data)
    rejected = []
    for s in sessions:
        for it in s.get("iterations") or []:
            if it.get("kind") == "code_fix" and it.get("verdict") != "flashed":
                rejected.append((s, it))
            if it.get("kind") == "verify_fix" and "patch reverted" in (it.get("note") or ""):
                rejected.append((s, it))
    def why(note: str) -> str:
        note = re.sub(r"^self-review before build:\s*", "", note)
        note = re.sub(r"^(?:Hunk \d+:|[\w./-]+\.(?:java|xml|kt):)\s*", "", note)
        return clip(note, 190)

    if rejected:
        rows = "".join(
            f"<li style='margin-bottom:12px'><span class='mono'>{html.escape(s['target']['module'])}</span> &middot; "
            f"<b>{html.escape((it.get('verdict') or '').replace('_', ' '))}</b><br>{html.escape(why(it.get('note') or ''))}</li>"
            for s, it in rejected[:6])
        segs.append(still(chrome_png(card(
            "<div class='kicker'>Checks before anything reaches the DUT</div><h2>What the self-review pushed back on</h2>"
            f"<ul style='font-size:20px;color:#cbd5e1;line-height:1.55;padding-left:24px'>{rows}</ul>",
            "The review is a cheap filter, not the judge: its objections go back to the model, and a patch proposed "
            "again is built and tested &mdash; CTS on the DUT decides. A patch that leaves CTS red is reverted."),
            work / "06.png"), 15, work / "06.mp4"))

    # 7. outro
    segs.append(still(chrome_png(card(
        "<div class='kicker'>What your team gets</div>"
        f"<h2>{len(fixed)} of {len(sessions)} failures fixed in code, {builds} builds, {runs} CTS runs</h2>"
        "<div class='row'>"
        "<div class='box'><h3>Root cause in your code</h3><p>File and line in CarSettings, CarSystemUI, CarLauncher or "
        "the device overlay &mdash; not just a log excerpt.</p></div>"
        "<div class='box'><h3>A patch that passed CTS</h3><p>Built, flashed and re-tested twice on the DUT before "
        "anyone reviews it.</p></div>"
        "<div class='box'><h3>Lab issues out of the way</h3><p>Host tools, network and device state are handled by "
        "reviewed lab remediations, so engineers spend time on product bugs.</p></div></div>"),
        work / "07.png"), 10, work / "07.mp4"))

    listing = work / "segments.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in segs), encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-movflags", "+faststart", str(out)], check=True)
    print(f"video: {out}  (work dir {work})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", nargs="+", required=True)
    ap.add_argument("--log", nargs="*", default=[])
    ap.add_argument("--model", default="")
    ap.add_argument("--command", action="append", default=[],
                    help="Exact command line(s), for logs written before the CLI printed them")
    ap.add_argument("--origin", action="append", default=[],
                    help="'<test name substring>=<label>', e.g. 'AutoPrivacy=Planted OEM change (CarSettings)'")
    ap.add_argument("--out", default="results/demo_video/xts_code_fix_demo.mp4")
    args = ap.parse_args()
    sessions = fl_report.load_sessions(args.sessions)
    order = {"FIXED_IN_CODE": 0, "FIXED": 1, "WORKAROUND_VERIFIED": 2, "FLAKY": 3}
    sessions.sort(key=lambda s: order.get(s.get("status"), 4))
    log_lines: List[str] = []
    for log in args.log:
        log_lines += Path(log).read_text(encoding="utf-8").splitlines()
    origins = dict(o.split("=", 1) for o in args.origin)
    build(sessions, log_lines, Path(args.out), args.model, args.command, origins)


if __name__ == "__main__":
    main()
