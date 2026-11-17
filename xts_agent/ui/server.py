"""``xts-agent ui``: a dependency-free web UI to launch, watch and review runs.

Standard library only (``http.server``), so it runs on the same Python 3.8
lab host as the agent with nothing extra to install.

* Live: every running plan with phase, per-suite status, which module each
  device is running, failures so far, heartbeat age and the TradeFed console
  log streamed incrementally. Stop sends the agent the same SIGTERM a CI
  cancel does: TradeFed flushes a partial result and the run can resume.
* Launch: start ``xts-agent run`` for a plan under ``config/test_plans``
  (optionally narrowed to some modules) as a detached process, so closing
  the browser or restarting the UI never stops a run.
* Runs / run detail: archived runs and older ones from the results DB, the
  module table from test_result.xml, failed tests, links to the HTML report,
  triage page and trends dashboard.
* Devices: adb state, build, which process leases each device, quarantine.

Security: binds 127.0.0.1 by default. Any other address requires a token
(``--token`` or XTS_UI_TOKEN), sent as ``Authorization: Bearer``; the page
picks it up from ``#token=`` once. State-changing calls also require the
``X-XTS-UI`` header (a cross-site form cannot set it), Host must match on
loopback (DNS rebinding), launch only accepts plan files from the plans
directory and module names from a strict pattern, stop only signals pids
recorded by the agent whose command line is the agent, and /files serves
only report-type files under the results directory.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from .data import (
    DevicesView,
    RunsView,
    agent_pid_alive,
    file_url,
    list_plans,
    tail_file,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SERVED_SUFFIXES = {".html", ".json", ".xml", ".css", ".js", ".png", ".svg", ".txt", ".log", ".csv"}
# Module or "Module class#method" include filters, as TradeFed takes them
_FILTER_RE = re.compile(r"^[A-Za-z0-9_.$#\[\]-]+( [A-Za-z0-9_.$#\[\]-]+){0,2}$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_MODULES = 200


class UiConfig:
    def __init__(
        self,
        root: Path,
        results_dir: Path,
        plans_dir: Path,
        db_path: Optional[Path],
        lease_dir: Optional[str],
        defaults_path: Optional[str] = None,
        token: str = "",
        host: str = "127.0.0.1",
        port: int = 8765,
        python: str = sys.executable,
    ):
        self.root = root
        self.results_dir = results_dir
        self.plans_dir = plans_dir
        self.db_path = db_path
        self.lease_dir = lease_dir
        self.defaults_path = defaults_path
        self.token = token
        self.host = host
        self.port = port
        self.python = python


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class UiApp:
    """Request-independent state and actions (unit-tested without HTTP)."""

    def __init__(self, cfg: UiConfig):
        self.cfg = cfg
        self.runs = RunsView(cfg.results_dir, cfg.db_path)
        self.devices = DevicesView(cfg.lease_dir)
        self._launch_lock = threading.Lock()

    # ----------------------------------------------------------- launch

    def _plan_path(self, name: str) -> Path:
        candidate = (self.cfg.plans_dir / Path(name).name).resolve()
        if candidate.parent != self.cfg.plans_dir.resolve() or candidate.suffix != ".yaml" or not candidate.exists():
            raise ValueError(f"unknown plan {name!r}")
        return candidate

    def build_launch(self, body: Dict[str, Any]) -> Tuple[List[str], Path, str]:
        """Validated command, console log and run id for a launch request."""
        plan_path = self._plan_path(str(body.get("plan") or ""))
        modules = [str(m).strip() for m in (body.get("modules") or []) if str(m).strip()]
        if len(modules) > MAX_MODULES:
            raise ValueError(f"at most {MAX_MODULES} modules")
        bad = [m for m in modules if not _FILTER_RE.match(m)]
        if bad:
            raise ValueError(f"invalid module filter(s): {', '.join(bad[:3])}")
        ai = body.get("ai_rca")
        stamp = time.strftime("%Y%m%d%H%M%S")
        run_id = f"ui-{stamp}-{os.getpid() % 10000:04d}"
        if modules or ai is not None:
            plan_path = self._derived_plan(plan_path, modules, ai, run_id)
        cmd = [self.cfg.python, "-m", "xts_agent.cli", "run", "--plan", str(plan_path)]
        if self.cfg.defaults_path:
            cmd += ["--config", self.cfg.defaults_path]
        if body.get("auto_retry"):
            cmd.append("--auto-retry")
        if body.get("resume"):
            cmd.append("--resume")
        if body.get("dry_run"):
            cmd.append("--dry-run")
        log = self.cfg.results_dir / "logs" / f"{run_id}.console.log"
        return cmd, log, run_id

    def _derived_plan(self, base: Path, modules: List[str], ai: Any, run_id: str) -> Path:
        data = yaml.safe_load(base.read_text(encoding="utf-8")) or {}
        if modules:
            enabled = [s for s in data.get("suites") or [] if isinstance(s, dict) and s.get("enabled", True)]
            if len(enabled) != 1:
                raise ValueError("module selection needs a plan with exactly one enabled suite")
            enabled[0]["include_filters"] = modules
            data["name"] = f"{data.get('name') or base.stem} [{len(modules)} module{'s' * (len(modules) > 1)}]"
            if data.get("profile") == "certification":
                # A filtered certification plan is refused by design; run it as development
                data["profile"] = "development"
        if ai is not None:
            data.setdefault("ai_rca", {})["enabled"] = bool(ai)
        out = self.cfg.results_dir / "ui_plans" / f"{base.stem}.{run_id}.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"# generated by xts-agent ui from {base.name}\n" + yaml.safe_dump(data, sort_keys=False),
                       encoding="utf-8")
        return out

    def launch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        with self._launch_lock:
            cmd, log, run_id = self.build_launch(body)
            plan_name = (yaml.safe_load(Path(cmd[cmd.index("--plan") + 1]).read_text(encoding="utf-8")) or {}).get("name", "")
            for view in self.runs.live():
                if view["state"] == "running" and view["plan"] == plan_name:
                    raise RuntimeError(f"{plan_name} is already running (pid {view['pid']})")
            log.parent.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ, XTS_RUN_ID=run_id)
            with open(log, "ab") as fh:
                proc = subprocess.Popen(  # noqa: S603 - fixed argv, validated inputs
                    cmd, cwd=str(self.cfg.root), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                    env=env, start_new_session=True,  # survives the UI and the browser
                )
        logger.info("UI launched %s (pid %s): %s", run_id, proc.pid, " ".join(cmd))
        return {"run_id": run_id, "pid": proc.pid, "command": cmd,
                "console_log": file_url(str(log), self.cfg.results_dir)}

    # ----------------------------------------------------------- stop

    def stop(self, key: str, force: bool = False) -> Dict[str, Any]:
        data = self.runs.live_file(key)
        if data is None:
            view = next((v for v in self.runs.live() if v["run_id"] == key), None)
            data = self.runs.live_file(view["key"]) if view else None
        if data is None:
            raise LookupError(f"no live run {key!r}")
        pid = int(data.get("pid") or 0)
        if data.get("phase") in ("done", "cancelled", "error") or not agent_pid_alive(pid):
            raise RuntimeError("run is not running")
        os.kill(pid, signal.SIGTERM)
        if force:
            time.sleep(0.5)
            if agent_pid_alive(pid):
                os.kill(pid, signal.SIGTERM)  # second signal: agent kills TradeFed and exits
        logger.info("UI sent %sSIGTERM to agent pid %s (%s)", "2x " if force else "", pid, key)
        return {"stopping": True, "pid": pid, "force": force}

    # ----------------------------------------------------------- links

    def decorate(self, view: Dict[str, Any]) -> Dict[str, Any]:
        rd = self.cfg.results_dir
        view["links"] = {
            "reports": [file_url(p, rd) for p in view.get("reports") or [] if file_url(p, rd)],
            "triage_html": file_url(view.get("triage_html") or "", rd),
            "triage_json": file_url(view.get("triage_json") or "", rd),
        }
        return view

    def overview(self) -> Dict[str, Any]:
        rd = self.cfg.results_dir
        reports = sorted((rd / "reports").glob("xts_report_*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        triage = sorted((rd / "triage").glob("triage_*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        dash = rd / "reports" / "dashboard.html"
        return {
            "host": os.uname().nodename,
            "results_dir": str(rd.resolve()),
            "latest_report": file_url(str(reports[0]), rd) if reports else "",
            "latest_triage": file_url(str(triage[0]), rd) if triage else "",
            "dashboard": file_url(str(dash), rd) if dash.exists() else "",
            "now": time.time(),
        }


class Handler(BaseHTTPRequestHandler):
    app: UiApp  # set on the subclass by make_server
    server_version = "xts-agent-ui"

    # ------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet access log
        logger.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(status, json.dumps(data, default=str).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        cfg = self.app.cfg
        if not is_loopback(cfg.host):
            return True  # token-protected; reverse proxies rewrite Host
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _authorized(self, query: Dict[str, List[str]]) -> bool:
        token = self.app.cfg.token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        given = auth[7:] if auth.startswith("Bearer ") else (query.get("t") or [""])[0]
        return hmac.compare_digest(given.encode(), token.encode())

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64_000:
            raise ValueError("request too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    # ------------------------------------------------------- routes

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = unquote(url.path)
        if not self._host_ok():
            return self._error(HTTPStatus.MISDIRECTED_REQUEST, "unexpected Host header")
        if path in ("/", "/index.html"):
            return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"})
        if path == "/healthz":
            return self._json({"ok": True})
        if not self._authorized(query):
            return self._error(HTTPStatus.UNAUTHORIZED, "token required")
        app = self.app
        try:
            if path == "/api/overview":
                return self._json(app.overview())
            if path == "/api/live":
                return self._json([app.decorate(v) for v in app.runs.live()])
            if path == "/api/runs":
                rows = app.runs.history(int((query.get("limit") or ["200"])[0]))
                rd = app.cfg.results_dir
                for r in rows:
                    r["report_url"] = file_url(r.get("report", ""), rd)
                    r["triage_url"] = file_url(r.get("triage_html", ""), rd)
                return self._json(rows)
            m = re.match(r"^/api/runs/([^/]+)$", path)
            if m:
                view = app.runs.detail(m.group(1))
                return self._json(app.decorate(view)) if view else self._error(404, "no such run")
            m = re.match(r"^/api/runs/([^/]+)/log$", path)
            if m:
                suite = (query.get("suite") or [""])[0]
                log = app.runs.log_path(m.group(1), suite)
                if log is None or not _under(log, app.cfg.results_dir):
                    return self._error(404, "no log for this suite")
                offset = int((query.get("offset") or ["-1"])[0])
                return self._json(tail_file(log, offset))
            if path == "/api/plans":
                return self._json(list_plans(app.cfg.plans_dir))
            if path == "/api/devices":
                return self._json(app.devices.list(refresh=bool(query.get("refresh"))))
            if path.startswith("/files/"):
                return self._file(path[len("/files/"):])
        except (ValueError, KeyError) as exc:
            return self._error(400, str(exc))
        return self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = unquote(url.path)
        if not self._host_ok():
            return self._error(HTTPStatus.MISDIRECTED_REQUEST, "unexpected Host header")
        if self.headers.get("X-XTS-UI") != "1":
            return self._error(HTTPStatus.FORBIDDEN, "missing X-XTS-UI header")
        if not self._authorized(query):
            return self._error(HTTPStatus.UNAUTHORIZED, "token required")
        app = self.app
        try:
            body = self._body()
            if path == "/api/launch":
                return self._json(app.launch(body), 202)
            m = re.match(r"^/api/runs/([^/]+)/stop$", path)
            if m:
                return self._json(app.stop(m.group(1), force=bool(body.get("force"))))
            m = re.match(r"^/api/devices/([^/]+)/release$", path)
            if m:
                serial = m.group(1)
                if not _SERIAL_RE.match(serial):
                    return self._error(400, "bad serial")
                return self._json({"released": app.devices.release_quarantine(serial)})
        except LookupError as exc:
            return self._error(404, str(exc))
        except RuntimeError as exc:
            return self._error(409, str(exc))
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, str(exc))
        return self._error(404, "not found")

    def _file(self, rel: str) -> None:
        base = self.app.cfg.results_dir.resolve()
        target = (base / rel).resolve()
        if not _under(target, base) or target.suffix.lower() not in SERVED_SUFFIXES or not target.is_file():
            return self._error(404, "not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix in (".log", ".txt"):
            ctype = "text/plain"
        if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
            ctype += "; charset=utf-8"
        # Reports are self-contained; sandbox them from the UI's origin
        extra = {"Content-Security-Policy": "sandbox allow-scripts allow-popups"} if target.suffix == ".html" else {}
        self._send(200, target.read_bytes(), ctype, extra)


def _under(path: Path, base: Path) -> bool:
    try:
        Path(os.path.abspath(path)).resolve().relative_to(Path(os.path.abspath(base)).resolve())
        return True
    except ValueError:
        return False


def make_server(cfg: UiConfig) -> ThreadingHTTPServer:
    if not is_loopback(cfg.host) and not cfg.token:
        raise ValueError(
            f"refusing to listen on {cfg.host} without a token: set --token or XTS_UI_TOKEN "
            "(launch/stop control lab devices)"
        )
    app = UiApp(cfg)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    server.daemon_threads = True
    return server
