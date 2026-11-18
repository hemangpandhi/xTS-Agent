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
import shutil
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

import yaml

from xts_agent import settings as host_settings

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
TF_SERVED_SUFFIXES = {".html", ".xml", ".xsl", ".css", ".png", ".svg", ".gif", ".json", ".txt", ".log"}
GOOGLE_HTML_NAMES = ("test_result_failures_suite.html", "test_result.html")
TF_STATIC_ASSETS = {"compatibility_result.css", "compatibility_result.xsl", "logo.png"}
MAX_TF_VIEW_BYTES = 200 * 1024 * 1024


def google_suite_links(run_key: str, suite_name: str, results_dir: str) -> Optional[Dict[str, str]]:
    """URLs for Google's official TradeFed compatibility HTML, XML and submission .zip."""
    if not run_key or not suite_name or not results_dir:
        return None
    rdir = Path(results_dir)
    try:
        if not rdir.is_dir():
            return None
        html_name = next((n for n in GOOGLE_HTML_NAMES if (rdir / n).is_file()), "")
        has_xml = (rdir / "test_result.xml").is_file()
        has_zip = rdir.with_name(rdir.name + ".zip").is_file()
    except OSError:
        return None
    if not (html_name or has_xml or has_zip):
        return None
    rk = quote(str(run_key), safe="")
    sn = quote(str(suite_name), safe="")
    html_url = f"/tf-results/{rk}/{sn}/{html_name}" if html_name else ""
    xml_url = f"/tf-results/{rk}/{sn}/test_result.xml" if has_xml else ""
    zip_url = f"/tf-zip/{rk}/{sn}" if has_zip else ""
    return {
        "suite": str(suite_name),
        "session": rdir.name,
        "html_url": html_url,
        "xml_url": xml_url,
        "zip_url": zip_url,
        "primary_url": html_url or xml_url,
    }
# Module or "Module class#method" include filters, as TradeFed takes them
_FILTER_RE = re.compile(r"^[A-Za-z0-9_.$#\[\]-]+( [A-Za-z0-9_.$#\[\]-]+){0,2}$")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_MODULES = 500
MAX_DEVICES = 64
_PLAN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")
CUSTOM_PLAN_MARKER = "# created by xts-agent ui"


def _clean_filters(items: Any) -> List[str]:
    out = [str(m).strip() for m in (items or []) if str(m).strip()]
    if len(out) > MAX_MODULES:
        raise ValueError(f"at most {MAX_MODULES} modules")
    bad = [m for m in out if not _FILTER_RE.match(m)]
    if bad:
        raise ValueError(f"invalid module filter(s): {', '.join(bad[:3])}")
    return [_abi_first(m) for m in out]


def _clean_serials(items: Any) -> List[str]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("serials must be a list of ADB serials")
    out = list(dict.fromkeys(str(s).strip() for s in items if str(s).strip()))
    if len(out) > MAX_DEVICES:
        raise ValueError(f"at most {MAX_DEVICES} devices")
    bad = [s for s in out if not _SERIAL_RE.match(s)]
    if bad:
        raise ValueError(f"invalid device serial(s): {', '.join(bad[:3])}")
    return out


def _clean_shard_count(val: Any) -> Any:
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        raise ValueError(f"shard_count must be 'auto' or an integer between 1 and {MAX_DEVICES}")
    if isinstance(val, str) and val.strip().lower() == "auto":
        return "auto"
    try:
        n = int(str(val).strip())
    except (TypeError, ValueError):
        raise ValueError(f"shard_count must be 'auto' or an integer between 1 and {MAX_DEVICES}") from None
    if n < 1 or n > MAX_DEVICES:
        raise ValueError(f"shard_count must be between 1 and {MAX_DEVICES}")
    return n


_ABI_NAMES = {"x86_64", "x86", "arm64-v8a", "armeabi-v7a", "riscv64"}


def _abi_first(flt: str) -> str:
    """TradeFed reads "[abi] module [test]"; fix "module abi [test]" typed the other way round."""
    parts = flt.split(" ")
    if len(parts) >= 2 and parts[1] in _ABI_NAMES and parts[0] not in _ABI_NAMES:
        parts[0], parts[1] = parts[1], parts[0]
    return " ".join(parts)


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
        settings_file: Optional[Path] = None,
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
        defaults_file = Path(defaults_path) if defaults_path else root / "config" / "default_config.yaml"
        self.defaults_file = defaults_file
        self.settings_file = settings_file or host_settings.settings_path(defaults_file)


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
        from xts_agent.triage.discussion import TriageWorkspace

        self.cfg = cfg
        self.runs = RunsView(cfg.results_dir, cfg.db_path)
        self.devices = DevicesView(cfg.lease_dir)
        self.triage = TriageWorkspace(cfg.root, cfg.results_dir, cfg.db_path)
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
        modules = _clean_filters(body.get("modules"))
        suite_modules = {}
        for suite, items in (body.get("suite_modules") or {}).items():
            cleaned = _clean_filters(items)
            if cleaned:
                suite_modules[str(suite).lower()] = cleaned
        ai = body.get("ai_rca")
        arch = str(body.get("arch") or "")
        if arch and arch not in host_settings.ARCHES:
            raise ValueError(f"architecture must be one of {', '.join(host_settings.ARCHES)}")
        shard_count = _clean_shard_count(body.get("shard_count"))
        serials = _clean_serials(body.get("serials"))
        stamp = time.strftime("%Y%m%d%H%M%S")
        run_id = f"ui-{stamp}-{os.getpid() % 10000:04d}"
        if modules or suite_modules or ai is not None or arch or shard_count is not None or serials:
            plan_path = self._derived_plan(
                plan_path, modules, ai, run_id, arch, suite_modules,
                shard_count=shard_count, serials=serials,
            )
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

    def _derived_plan(self, base: Path, modules: List[str], ai: Any, run_id: str, arch: str = "",
                      suite_modules: Optional[Dict[str, List[str]]] = None,
                      shard_count: Any = None,
                      serials: Optional[List[str]] = None) -> Path:
        data = yaml.safe_load(base.read_text(encoding="utf-8")) or {}
        enabled = [s for s in data.get("suites") or [] if isinstance(s, dict) and s.get("enabled", True)]
        suite_modules = dict(suite_modules or {})
        if modules:
            if len(enabled) != 1:
                raise ValueError("module selection needs a plan with exactly one enabled suite")
            suite_modules[str(enabled[0].get("name", "")).lower()] = modules
        if suite_modules:
            names = {str(s.get("name", "")).lower(): s for s in enabled}
            unknown = sorted(set(suite_modules) - set(names))
            if unknown:
                raise ValueError(f"suite(s) not in this plan: {', '.join(unknown)}")
            for suite, items in suite_modules.items():
                names[suite]["include_filters"] = items
            # Only the suites with a selection run; the others are switched off for this run
            for s in enabled:
                if str(s.get("name", "")).lower() not in suite_modules:
                    s["enabled"] = False
            total = sum(len(v) for v in suite_modules.values())
            data["name"] = f"{data.get('name') or base.stem} [{total} module{'s' * (total > 1)}]"
            if data.get("profile") == "certification":
                # A filtered certification plan is refused by design; run it as development
                data["profile"] = "development"
        if ai is not None:
            data.setdefault("ai_rca", {})["enabled"] = bool(ai)
        if arch:
            data["arch"] = arch
            for s in data.get("suites") or []:
                if isinstance(s, dict):
                    s.pop("arch", None)  # the launch choice applies to every suite
        if serials or shard_count is not None:
            raw_devs = data.get("devices") or data.get("device_requirements") or {}
            devs = dict(raw_devs) if isinstance(raw_devs, dict) else {}
            data.pop("device_requirements", None)
            data["devices"] = devs
            if serials:
                devs["serials"] = list(serials)
                if int(devs.get("min_devices") or 1) > len(serials):
                    devs["min_devices"] = len(serials)
                if shard_count is None:
                    for s in data.get("suites") or []:
                        if isinstance(s, dict) and s.get("enabled", True):
                            sh = s.setdefault("sharding", {})
                            sh["enabled"] = True
                            sh["shard_count"] = len(serials)
            if shard_count is not None:
                for s in data.get("suites") or []:
                    if isinstance(s, dict):
                        sh = s.setdefault("sharding", {})
                        sh["enabled"] = True
                        sh["shard_count"] = shard_count
                if isinstance(shard_count, int) and int(devs.get("min_devices") or 1) > shard_count:
                    devs["min_devices"] = shard_count
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

    def resolve_tf_suite_dir(self, run_key: str, suite_name: str) -> Optional[Path]:
        """Recorded TradeFed results_dir for (run_key, suite_name), or None."""
        view = self.runs.get(run_key)
        if view is None and run_key.startswith("db-"):
            view = self.runs.detail(run_key)
        if view is None:
            return None
        want = str(suite_name or "").lower()
        suite = next(
            (s for s in (view.get("suites") or []) if str(s.get("name") or "").lower() == want),
            None,
        )
        if not suite or not suite.get("results_dir"):
            return None
        try:
            rdir = Path(str(suite["results_dir"])).resolve()
            return rdir if rdir.is_dir() else None
        except OSError:
            return None

    def decorate(self, view: Dict[str, Any]) -> Dict[str, Any]:
        rd = self.cfg.results_dir
        rkey = str(view.get("run_id") or view.get("key") or "")
        google_reports = []
        for s in view.get("suites") or []:
            gl = google_suite_links(rkey, str(s.get("name") or ""), str(s.get("results_dir") or ""))
            s["google"] = gl
            if gl:
                google_reports.append(gl)
        view["links"] = {
            "reports": [file_url(p, rd) for p in view.get("reports") or [] if file_url(p, rd)],
            "triage_html": file_url(view.get("triage_html") or "", rd),
            "triage_json": file_url(view.get("triage_json") or "", rd),
            "google_reports": google_reports,
        }
        return view

    # ----------------------------------------------------------- settings

    def effective_defaults(self) -> Dict[str, Any]:
        from xts_agent.config_loader import ConfigLoader

        return ConfigLoader("unused.yaml", self.cfg.defaults_file).load_defaults()

    def settings(self) -> Dict[str, Any]:
        local = host_settings.load(self.cfg.settings_file)
        eff = self.effective_defaults()
        paths = eff.get("paths") or {}
        installs = []
        for item in host_settings.installations(local.get("suites") or {}):
            installs.append({**item, "inspect": host_settings.inspect_package(str(item.get("path", "")), str(item.get("suite", "")))})
        ai = eff.get("ai_rca") or {}
        return {
            "file": str(self.cfg.settings_file),
            "file_exists": Path(self.cfg.settings_file).exists(),
            "suites_known": list(host_settings.SUITES),
            "arches": list(host_settings.ARCHES),
            "default_arch": (local.get("suites") or {}).get("default_arch") or host_settings.ARCHES[0],
            "installations": installs,
            "paths": {k: paths.get(k, "") for k in ("xts_packages_dir", "java_home", "android_sdk", "adb_path")},
            "paths_local": (local.get("paths") or {}),
            "paths_check": {k: host_settings.check_tool_path(k, str(paths.get(k) or ""))
                            for k in ("xts_packages_dir", "java_home", "android_sdk", "adb_path")},
            "ai_rca": {
                "provider": str(ai.get("provider") or "openai_compatible").lower(),
                "enabled": bool(ai.get("enabled", False)),
                "allow_external_providers": bool(ai.get("allow_external_providers", False)),
                "openai_base_url": ai.get("openai_base_url", ""),
                "openai_model": ai.get("openai_model", ""),
                "llama_model_path": ai.get("llama_model_path", ""),
                "llama_n_ctx": int(ai.get("llama_n_ctx") or 16384),
                "llama_n_gpu_layers": int(ai["llama_n_gpu_layers"]) if ai.get("llama_n_gpu_layers") is not None else -1,
                "gemini_model": ai.get("gemini_model", "gemini-2.5-flash"),
                "fallbacks": ai.get("openai_fallbacks") or [],
                "llama_check": host_settings.check_tool_path("llama_model_path", str(ai.get("llama_model_path") or "")),
                "has_llama_cpp_pkg": host_settings.has_llama_cpp_pkg(),
                "has_llm_api_key": bool(os.environ.get("XTS_LLM_API_KEY")),
                "has_gemini_api_key": bool(os.environ.get("XTS_GEMINI_API_KEY")),
            },
            "discovered": host_settings.scan(str(paths.get("xts_packages_dir") or "/opt/xts")),
            # What an empty cell falls back to: <xts_packages_dir>/android-<suite>
            "fallback": {
                s: host_settings.inspect_package(str(Path(paths.get("xts_packages_dir") or "/opt/xts") / f"android-{s}"), s)
                for s in host_settings.SUITES
            },
        }

    def save_settings(self, body: Dict[str, Any]) -> Dict[str, Any]:
        clean = host_settings.validate(body)
        current = host_settings.load(self.cfg.settings_file)
        current.update(clean)
        for section in ("paths", "suites", "ai_rca"):
            if section in body and section not in clean:
                current.pop(section, None)  # cleared in the form
        host_settings.save(self.cfg.settings_file, current)
        logger.info("Host settings saved to %s", self.cfg.settings_file)
        return self.settings()

    @staticmethod
    def _probe_llm(base: str, model: str, api_key: str = "") -> Dict[str, Any]:
        import requests

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            resp = requests.get(base + "/models", headers=headers, timeout=6)
            resp.raise_for_status()
            models = [m.get("id") for m in (resp.json().get("data") or []) if isinstance(m, dict)]
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: cannot reach {base}/models"}
        return {"ok": True, "models": models[:50], "model_found": (model in models) if model else None}

    def test_llm(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Probe the configured LLM provider (local HTTP, llama.cpp GGUF, opted-in online API, or disabled)."""
        from xts_agent.rca.llm_provider import is_local_endpoint

        provider = str(body.get("provider") or "openai_compatible").strip().lower()
        if provider not in host_settings.AI_PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        if provider == "none":
            return {
                "ok": True,
                "provider": "none",
                "models": [],
                "note": "AI disabled — deterministic Level-1 triage, team notes, verdicts, and Copy-Prompt remain active.",
            }
        if provider == "llama_cpp":
            chk = host_settings.check_tool_path("llama_model_path", str(body.get("llama_model_path") or "").strip())
            return {
                "ok": chk["ok"],
                "provider": "llama_cpp",
                "models": [],
                "note": chk["note"],
                "error": "" if chk["ok"] else chk["note"],
            }
        allow_ext = bool(body.get("allow_external_providers"))
        if provider == "gemini":
            if not allow_ext:
                raise ValueError("enable 'Allow external AI API' first before testing Google Gemini")
            gkey = os.environ.get("XTS_GEMINI_API_KEY", "")
            if not gkey:
                return {
                    "ok": False,
                    "provider": "gemini",
                    "models": [],
                    "error": "XTS_GEMINI_API_KEY environment variable is not set on this host",
                }
            import requests

            model = str(body.get("gemini_model") or body.get("model") or "gemini-2.5-flash").strip()
            try:
                resp = requests.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": gkey},
                    timeout=6,
                )
                resp.raise_for_status()
                raw_models = resp.json().get("models") or []
                models = [str(m.get("name") or "").replace("models/", "") for m in raw_models if isinstance(m, dict)]
            except Exception as exc:
                return {"ok": False, "provider": "gemini", "models": [], "error": f"{type(exc).__name__}: Gemini API unreachable or invalid key"}
            return {"ok": True, "provider": "gemini", "models": models[:50], "model_found": (model in models) if model else None}

        base = str(body.get("base_url") or "").rstrip("/")
        if not re.match(r"^https?://", base):
            raise ValueError("base URL must start with http:// or https://")
        local_ep = is_local_endpoint(base)
        if not local_ep:
            if not allow_ext or not base.startswith("https://"):
                raise ValueError(
                    "only on-prem endpoints (this host or a private network) can be tested unless "
                    "'Allow external AI API' is enabled with an https:// endpoint"
                )
        api_key = os.environ.get("XTS_LLM_API_KEY", "")
        if not local_ep and not api_key:
            return {
                "ok": False,
                "provider": "openai_compatible",
                "models": [],
                "error": "XTS_LLM_API_KEY environment variable is not set on this host",
            }
        # The key leaves this host only for the endpoint an admin saved in Settings:
        # a request must not be able to point it at another URL.
        saved_ai = self.effective_defaults().get("ai_rca") or {}
        saved_base = str(saved_ai.get("openai_base_url") or "").rstrip("/")
        key_allowed = bool(api_key) and base == saved_base and (
            local_ep or bool(saved_ai.get("allow_external_providers"))
        )
        if not local_ep and not key_allowed:
            return {
                "ok": False,
                "provider": "openai_compatible",
                "models": [],
                "error": "Save these settings first: the API key is only sent to the saved endpoint",
            }
        model = str(body.get("model") or "")
        out = self._probe_llm(base, model, api_key=api_key if key_allowed else "")
        if not local_ep or (out["ok"] and out["model_found"] is not False):
            return out
        fallbacks = []
        for fb in (self.effective_defaults().get("ai_rca") or {}).get("openai_fallbacks") or []:
            fb_base = str(fb.get("base_url") or "").rstrip("/")
            if not fb_base or not is_local_endpoint(fb_base):
                continue
            probe = self._probe_llm(fb_base, str(fb.get("model") or ""))
            fallbacks.append({"base_url": fb_base, "model": fb.get("model", ""), "ok": probe["ok"],
                              "model_found": probe.get("model_found"), "error": probe.get("error", "")})
        out["fallbacks"] = fallbacks
        usable = next((f for f in fallbacks if f["ok"] and f["model_found"] is not False), None)
        out["runs_use"] = usable
        return out

    def _package_for(self, suite: str, arch: str) -> Tuple[str, str]:
        """(package path, java_home) for a suite on an architecture, from the effective settings."""
        if suite not in host_settings.SUITES:
            raise ValueError(f"unknown suite {suite!r}")
        if arch and arch not in host_settings.ARCHES:
            raise ValueError(f"unknown architecture {arch!r}")
        eff = self.effective_defaults()
        installs = eff.get("suite_installations") or {}
        paths = eff.get("paths") or {}
        pkg = host_settings.resolve_package_path(suite, arch, installs, paths.get("xts_packages_dir") or "/opt/xts")
        info = host_settings.inspect_package(pkg, suite)
        if not info["ok"]:
            raise LookupError(f"{suite} for {arch or 'the default architecture'} is not installed at {pkg}: "
                              f"{info['problems'][0]}. Set it in Settings.")
        if arch and info["arch"] not in ("", "multi", arch):
            raise LookupError(f"no {suite} package for {arch}: {pkg} is {info['arch']}. Add the {arch} "
                              "package in Settings.")
        return pkg, str(paths.get("java_home") or "")

    def modules(self, suite: str, arch: str, refresh: bool = False) -> Dict[str, Any]:
        from xts_agent.suites import catalog

        pkg, java = self._package_for(suite, arch)
        return catalog.list_modules(pkg, suite, self.cfg.results_dir / "ui_cache", java, refresh=refresh)

    def tradefed_plans(self, suite: str, arch: str, refresh: bool = False) -> Dict[str, Any]:
        from xts_agent.suites import catalog

        pkg, java = self._package_for(suite, arch)
        return catalog.list_plans(pkg, suite, self.cfg.results_dir / "ui_cache", java, refresh=refresh)

    # ----------------------------------------------------------- plan editor

    def _plan_file(self, name: str, must_exist: bool = False) -> Path:
        stem = name[:-5] if name.endswith(".yaml") else name
        if not _PLAN_NAME_RE.match(stem):
            raise ValueError("file name: lower-case letters, digits, '-' and '_' only (max 60)")
        path = self.cfg.plans_dir / f"{stem}.yaml"
        if must_exist and not path.exists():
            raise LookupError(f"no plan {path.name}")
        return path

    @staticmethod
    def _is_custom(path: Path) -> bool:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.readline().startswith(CUSTOM_PLAN_MARKER)
        except OSError:
            return False

    def plan_source(self, name: str) -> Dict[str, Any]:
        path = self._plan_file(name, must_exist=True)
        text = path.read_text(encoding="utf-8")
        return {"file": path.name, "custom": self._is_custom(path), "yaml": text,
                "data": yaml.safe_load(text) or {}}

    def validate_plan(self, text: str) -> Dict[str, Any]:
        """Parse and load a plan exactly as a run would; raises ValueError with the reason."""
        from xts_agent.config_loader import ConfigLoader

        if len(text) > 200_000:
            raise ValueError("plan is too large")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"YAML error: {exc}") from None
        if not isinstance(data, dict):
            raise ValueError("a plan is a YAML mapping (name, suites, ...)")
        suites = data.get("suites")
        if not isinstance(suites, list) or not suites:
            raise ValueError("a plan needs at least one suite")
        devs_raw = data.get("devices") or data.get("device_requirements") or {}
        if isinstance(devs_raw, dict) and devs_raw.get("serials") is not None:
            raw_ser = devs_raw["serials"]
            _clean_serials([raw_ser] if isinstance(raw_ser, str) else raw_ser)
        for s in suites:
            name = str((s or {}).get("name", "")).lower() if isinstance(s, dict) else ""
            if name not in host_settings.SUITES:
                raise ValueError(f"suite name must be one of {', '.join(host_settings.SUITES)} (got {name or 'nothing'})")
            sharding = s.get("sharding")
            if isinstance(sharding, dict) and sharding.get("shard_count") is not None:
                _clean_shard_count(sharding["shard_count"])
            for f in list(s.get("include_filters") or []) + list(s.get("exclude_filters") or []):
                if not _FILTER_RE.match(str(f)):
                    raise ValueError(f"invalid filter {str(f)[:60]!r}")
            if s.get("package_path") and not str(s["package_path"]).startswith("/"):
                raise ValueError("package_path must be absolute")
        tmp = self.cfg.results_dir / "ui_plans" / f"validate-{os.getpid()}-{threading.get_ident()}.yaml"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        try:
            plan = ConfigLoader(tmp, self.cfg.defaults_file).load_plan()
        except Exception as exc:
            raise ValueError(f"{type(exc).__name__}: {exc}"[:400]) from None
        finally:
            tmp.unlink(missing_ok=True)
        return {"name": plan.name, "profile": plan.profile,
                "suites": [{"name": s.name, "plan": s.plan, "package_path": s.package_path, "arch": s.arch,
                            "include_filters": len(s.include_filters), "enabled": s.enabled} for s in plan.suites]}

    def save_plan(self, body: Dict[str, Any]) -> Dict[str, Any]:
        path = self._plan_file(str(body.get("file") or ""))
        text = str(body.get("yaml") or "")
        if not text and isinstance(body.get("data"), dict):
            text = yaml.safe_dump(body["data"], sort_keys=False)
        text = "\n".join(ln for ln in text.splitlines() if not ln.startswith(CUSTOM_PLAN_MARKER)).strip() + "\n"
        summary = self.validate_plan(text)
        if path.exists() and not self._is_custom(path):
            raise RuntimeError(f"{path.name} is a built-in plan; save your version under a new file name")
        if path.exists() and not body.get("overwrite"):
            raise RuntimeError(f"{path.name} already exists")
        header = f"{CUSTOM_PLAN_MARKER} on {time.strftime('%Y-%m-%d %H:%M')}. Edit here or in the console.\n"
        path.write_text(header + text, encoding="utf-8")
        logger.info("UI saved plan %s", path)
        return {"file": path.name, "saved": True, **summary}

    def delete_plan(self, name: str) -> Dict[str, Any]:
        path = self._plan_file(name, must_exist=True)
        if not self._is_custom(path):
            raise RuntimeError(f"{path.name} is a built-in plan and cannot be deleted from the console")
        path.unlink()
        logger.info("UI deleted plan %s", path)
        return {"file": path.name, "deleted": True}

    def plans(self) -> List[Dict[str, Any]]:
        """Plans plus where each suite resolves for every architecture."""
        eff = self.effective_defaults()
        installs = eff.get("suite_installations") or {}
        pkgdir = (eff.get("paths") or {}).get("xts_packages_dir") or "/opt/xts"
        cache: Dict[str, Dict[str, Any]] = {}
        out = list_plans(self.cfg.plans_dir)
        for plan in out:
            plan["custom"] = self._is_custom(self.cfg.plans_dir / plan["file"])
            if plan.get("error"):
                continue
            try:
                raw = yaml.safe_load((self.cfg.plans_dir / plan["file"]).read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            plan["arch"] = str(raw.get("arch") or ((raw.get("plan") or {}).get("arch") if isinstance(raw.get("plan"), dict) else "") or "")
            by_name = {str(s.get("name", "")).lower(): s for s in raw.get("suites") or [] if isinstance(s, dict)}
            for s in plan["suites"]:
                spec = by_name.get(str(s["name"]).lower()) or {}
                s["resolved"] = {}
                for arch in host_settings.ARCHES:
                    path = spec.get("package_path") or host_settings.resolve_package_path(
                        str(s["name"]), str(spec.get("arch") or arch), installs, pkgdir)
                    if path not in cache:
                        cache[path] = host_settings.inspect_package(path, str(s["name"]).lower())
                    info = cache[path]
                    problems = list(info["problems"])
                    if info["ok"] and info["arch"] not in ("", "multi", arch):
                        problems.append(f"this package is {info['arch']}, not {arch}")
                    s["resolved"][arch] = {"path": path, "ok": not problems, "abis": info["abis"], "arch": info["arch"],
                                           "pinned": bool(spec.get("package_path")), "problems": problems}
        return out

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

    # ----------------------------------------------------------- interactive triage

    def triage_runs(self) -> Dict[str, Any]:
        return self.triage.list_runs(self.runs)

    def triage_run(self, key: str) -> Dict[str, Any]:
        out = self.triage.load_triage(key, self.runs, self.effective_defaults())
        out["triage_html_url"] = file_url(out.get("triage_html") or "", self.cfg.results_dir)
        view = self.runs.get(key) or (self.runs.detail(key) if key.startswith("db-") else None)
        google_reports = []
        if view:
            rkey = str(view.get("run_id") or view.get("key") or key)
            for s in view.get("suites") or []:
                gl = google_suite_links(rkey, str(s.get("name") or ""), str(s.get("results_dir") or ""))
                if gl:
                    google_reports.append(gl)
        out["google_reports"] = google_reports
        return out

    def triage_group(self, key: str, signature: str) -> Dict[str, Any]:
        return self.triage.group_detail(key, signature, self.runs, self.effective_defaults())

    def triage_analyze(self, key: str, signature: str, force: bool = False) -> Dict[str, Any]:
        return self.triage.analyze_group(key, signature, self.runs, self.effective_defaults(), force=force)

    def triage_discuss(self, key: str, signature: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.triage.discuss_group(
            key=key,
            signature=signature,
            message=str(body.get("message") or ""),
            mode=str(body.get("mode") or "ask"),
            author=str(body.get("author") or ""),
            runs_view=self.runs,
            defaults=self.effective_defaults(),
        )

    def triage_verdict(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.triage.record_verdict(body, self.effective_defaults())

    def triage_known_issue(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.triage.promote_to_known_issues(body, self.effective_defaults())


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
        is_tf_boilerplate = path.startswith("/tf-results/") and path.rsplit("/", 1)[-1] in TF_STATIC_ASSETS
        if not is_tf_boilerplate and not self._authorized(query):
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
                    gl = google_suite_links(
                        str(r.get("run_id") or r.get("key") or ""),
                        str((r.get("suites") or ["cts"])[0]),
                        str(r.get("results_dir") or ""),
                    )
                    r["google_url"] = gl["primary_url"] if gl else ""
                    r["google_zip_url"] = gl["zip_url"] if gl else ""
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
            if path in ("/api/modules", "/api/tradefed-plans"):
                suite = (query.get("suite") or [""])[0].lower()
                arch = (query.get("arch") or [""])[0]
                refresh = bool(query.get("refresh"))
                fn = app.modules if path == "/api/modules" else app.tradefed_plans
                return self._json(fn(suite, arch, refresh))
            m = re.match(r"^/api/plans/([A-Za-z0-9_.-]+)$", path)
            if m:
                return self._json(app.plan_source(m.group(1)))
            if path == "/api/plans":
                return self._json(app.plans())
            if path == "/api/settings":
                return self._json(app.settings())
            if path == "/api/devices":
                return self._json(app.devices.list(refresh=bool(query.get("refresh"))))
            if path == "/api/triage/runs":
                return self._json(app.triage_runs())
            m = re.match(r"^/api/triage/([^/]+)/([0-9a-f]{8,24})$", path)
            if m:
                return self._json(app.triage_group(m.group(1), m.group(2)))
            m = re.match(r"^/api/triage/([^/]+)$", path)
            if m:
                return self._json(app.triage_run(m.group(1)))
            if path.startswith("/files/"):
                return self._file(path[len("/files/"):])
            m = re.match(r"^/tf-results/([^/]+)/([^/]+)/(.+)$", path)
            if m:
                return self._tf_file(m.group(1), m.group(2), m.group(3))
            m = re.match(r"^/tf-zip/([^/]+)/([^/]+)$", path)
            if m:
                return self._tf_zip(m.group(1), m.group(2))
        except (ValueError, KeyError) as exc:
            return self._error(400, str(exc).strip("'\""))
        except LookupError as exc:  # suite not installed, plan not found
            return self._error(404, str(exc))
        except Exception:
            logger.exception("GET %s failed", path)
            return self._error(500, "internal error; see the console's log")
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
            if path == "/api/settings":
                return self._json(app.save_settings(body))
            if path == "/api/settings/scan":
                return self._json(host_settings.scan(str(body.get("root") or ""), max_depth=3))
            if path == "/api/settings/check":
                kind = str(body.get("kind") or "")
                value = str(body.get("path") or "")
                if kind == "suite":
                    return self._json(host_settings.inspect_package(value, str(body.get("suite") or "")))
                if kind not in ("xts_packages_dir", "java_home", "android_sdk", "adb_path", "llama_model_path"):
                    return self._error(400, "unknown kind")
                return self._json(host_settings.check_tool_path(kind, value))
            if path == "/api/plans/validate":
                return self._json(app.validate_plan(str(body.get("yaml") or "")))
            if path == "/api/plans/parse":
                text = str(body.get("yaml") or "")
                if len(text) > 200_000:
                    return self._error(400, "plan is too large")
                try:
                    data = yaml.safe_load(text)
                except yaml.YAMLError as exc:
                    return self._error(400, f"YAML error: {exc}")
                if not isinstance(data, dict):
                    return self._error(400, "a plan is a YAML mapping (name, suites, ...)")
                return self._json({"data": data})
            if path == "/api/plans/render":
                data = body.get("data")
                if not isinstance(data, dict):
                    return self._error(400, "data must be an object")
                return self._json({"yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)})
            if path == "/api/plans/save":
                return self._json(app.save_plan(body))
            if path == "/api/plans/delete":
                return self._json(app.delete_plan(str(body.get("file") or "")))
            if path == "/api/settings/test-llm":
                return self._json(app.test_llm(body))
            if path == "/api/triage/verdict":
                return self._json(app.triage_verdict(body))
            if path == "/api/triage/known-issue":
                return self._json(app.triage_known_issue(body))
            m = re.match(r"^/api/triage/([^/]+)/([0-9a-f]{8,24})/analyze$", path)
            if m:
                return self._json(app.triage_analyze(m.group(1), m.group(2), force=bool(body.get("force"))))
            m = re.match(r"^/api/triage/([^/]+)/([0-9a-f]{8,24})/discuss$", path)
            if m:
                return self._json(app.triage_discuss(m.group(1), m.group(2), body))
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
        except Exception:
            logger.exception("POST %s failed", path)
            return self._error(500, "internal error; see the console's log")
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

    def _tf_file(self, run_key: str, suite_name: str, rel: str) -> None:
        base = self.app.resolve_tf_suite_dir(run_key, suite_name)
        if base is None:
            return self._error(404, "not found")
        target = (base / rel).resolve()
        if not _under(target, base) or target.suffix.lower() not in TF_SERVED_SUFFIXES or not target.is_file():
            return self._error(404, "not found")
        if target.stat().st_size > MAX_TF_VIEW_BYTES:
            return self._error(413, "result file too large to stream inline; download the submission .zip instead")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".xsl":
            ctype = "application/xslt+xml"
        elif target.suffix in (".log", ".txt"):
            ctype = "text/plain"
        if ctype.startswith("text/") or ctype in ("application/json", "application/xml", "application/xslt+xml"):
            ctype += "; charset=utf-8"
        extra = {
            "Content-Security-Policy": "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
        }
        self._send(200, target.read_bytes(), ctype, extra)

    def _tf_zip(self, run_key: str, suite_name: str) -> None:
        base = self.app.resolve_tf_suite_dir(run_key, suite_name)
        if base is None:
            return self._error(404, "not found")
        tf_zip = base.with_name(base.name + ".zip")
        if not tf_zip.is_file():
            return self._error(404, "submission zip not found")
        size = tf_zip.stat().st_size
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", tf_zip.name)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            with open(tf_zip, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile, length=65536)


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
