"""Read-only views over what a run leaves on disk, for the execution UI.

The UI never talks to a running agent. Every ``xts-agent run`` (from the CLI,
CI or the UI) writes ``results/run_state/<plan>.json`` with its pid, phase,
suites, devices and per-device TradeFed progress, and archives it to
``run_state/history/<run_id>.json`` when it ends. These functions turn those
files, the results database, TradeFed's ``test_result.xml`` and ``adb`` into
plain dicts for the JSON API, so the UI also shows runs it did not start and
keeps working after it is restarted.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

TERMINAL_PHASES = {"done", "cancelled", "error"}
MAX_FAILED_TESTS = 300
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


# ---------------------------------------------------------------- processes


def agent_pid_alive(pid: Any) -> bool:
    """True if ``pid`` is a live xTS Agent process (not a recycled pid)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return False
    return b"xts_agent" in cmdline or b"xts-agent" in cmdline


def _load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _state_for_view(key: str, data: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Normalise a run-state dict (live or archived) for the UI."""
    phase = data.get("phase") or ("done" if data.get("complete") else "running")
    alive = agent_pid_alive(data.get("pid"))
    if phase in TERMINAL_PHASES:
        state = phase
    elif alive:
        state = "running"
    elif data.get("pid"):
        state = "interrupted"  # agent died mid-run: `xts-agent run --resume` continues it
    else:
        state = "unknown"  # written by an agent older than the live view
    suites = []
    for name, entry in (data.get("suites") or {}).items():
        progress = entry.get("progress") or {}
        suites.append(
            {
                "name": name,
                "status": entry.get("status", ""),
                "devices": entry.get("device_serials") or [],
                "fingerprint": entry.get("fingerprint", ""),
                "session_id": entry.get("session_id"),
                "results_dir": entry.get("results_dir", ""),
                "retry_count": entry.get("retry_count", 0),
                "retry_attempt": entry.get("retry_attempt"),
                "pass_count": entry.get("pass_count"),
                "fail_count": entry.get("fail_count"),
                "skip_count": entry.get("skip_count"),
                "modules_done": entry.get("modules_done"),
                "modules_total": entry.get("modules_total"),
                "error": entry.get("error", ""),
                "progress": {
                    "modules_completed": progress.get("modules_completed"),
                    "modules_started": progress.get("modules_started"),
                    "failures": progress.get("failures"),
                    "current": progress.get("current") or {},
                    "elapsed_secs": progress.get("elapsed_secs"),
                    "quiet_secs": progress.get("quiet_secs"),
                    "heartbeat_at": progress.get("heartbeat_at"),
                    "has_log": bool(progress.get("log")),
                },
                "updated_at": entry.get("updated_at"),
            }
        )
    return {
        "key": key,
        "source": source,
        "plan": data.get("plan_name", ""),
        "profile": data.get("profile", ""),
        "run_id": data.get("run_id", ""),
        "state": state,
        "phase": phase,
        "pid": data.get("pid"),
        "host": data.get("host", ""),
        "user": data.get("user", ""),
        "flags": data.get("flags") or {},
        "started_at": data.get("attached_at") or data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "heartbeat_at": data.get("heartbeat_at"),
        "overall_status": data.get("overall_status", ""),
        "totals": data.get("totals") or {},
        "suites": suites,
        "timeline": (data.get("timeline") or [])[-200:],
        "reports": data.get("reports") or [],
        "triage_html": data.get("triage_html", ""),
        "triage_json": data.get("triage_json", ""),
        "triage_summary": data.get("triage_summary"),
        "error": data.get("error", ""),
    }


# ---------------------------------------------------------------- runs


class RunsView:
    def __init__(self, results_dir: Path, db_path: Optional[Path] = None):
        self.results_dir = Path(results_dir)
        self.state_dir = self.results_dir / "run_state"
        self.db_path = db_path
        self._xml_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._xml_lock = threading.Lock()

    def live(self) -> List[Dict[str, Any]]:
        """Every per-plan state file; the UI shows running ones first."""
        out = []
        for path in sorted(self.state_dir.glob("*.json")):
            data = _load(path)
            if data is not None:
                out.append(_state_for_view(path.stem, data, "live"))
        order = {"running": 0, "interrupted": 1}
        out.sort(key=lambda r: (order.get(r["state"], 2), -(r["started_at"] or 0)))
        return out

    def live_file(self, key: str) -> Optional[Dict[str, Any]]:
        if not _KEY_RE.match(key):
            return None
        return _load(self.state_dir / f"{key}.json")

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """A run by archive id (``run_id``) or live key (plan slug)."""
        if not _KEY_RE.match(key):
            return None
        archived = _load(self.state_dir / "history" / f"{key}.json")
        if archived is not None:
            return _state_for_view(key, archived, "history")
        live = self.live_file(key)
        if live is not None:
            return _state_for_view(key, live, "live")
        for view in self.live():  # a running run is addressed by its run_id too
            if view["run_id"] == key:
                return view
        return None

    def history(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Archived runs (newest first), then older runs known only to the DB."""
        runs: List[Dict[str, Any]] = []
        seen_dirs: Set[str] = set()
        for path in (self.state_dir / "history").glob("*.json"):
            data = _load(path)
            if data is None:
                continue
            view = _state_for_view(path.stem, data, "history")
            runs.append(_history_row(view))
            seen_dirs.update(s["results_dir"] for s in view["suites"] if s["results_dir"])
        for row in self._db_suite_runs(limit):
            if row["results_dir"] in seen_dirs:
                continue
            runs.append(row)
        runs.sort(key=lambda r: -(r.get("started_at") or 0))
        return runs[:limit]

    def _db_suite_runs(self, limit: int) -> List[Dict[str, Any]]:
        if not self.db_path or not Path(self.db_path).exists():
            return []
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM suite_runs ORDER BY recorded_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
            con.close()
        except sqlite3.Error:
            return []
        out = []
        for r in rows:
            started = (r["started_ms"] or 0) / 1000 or r["recorded_at"]
            out.append(
                {
                    "key": f"db-{r['id']}",
                    "source": "db",
                    "plan": r["plan"],
                    "run_id": "",
                    "state": "done",
                    "status": r["status"],
                    "suites": [r["suite"]],
                    "started_at": started,
                    "duration": r["duration"],
                    "pass": r["pass_count"],
                    "fail": r["fail_count"],
                    "skip": r["skip_count"],
                    "devices": json.loads(r["devices"] or "[]") if (r["devices"] or "").startswith("[")
                    else [d for d in (r["devices"] or "").split(",") if d],
                    "results_dir": r["results_dir"],
                    "report": "",
                    "triage_html": "",
                    "modules_done": r["modules_done"],
                    "modules_total": r["modules_total"],
                }
            )
        return out

    # ------------------------------------------------------- run detail

    def detail(self, key: str) -> Optional[Dict[str, Any]]:
        view = self.get(key)
        if view is None:
            if key.startswith("db-"):
                return self._db_detail(key)
            return None
        for suite in view["suites"]:
            suite["result"] = self.result_tables(suite["results_dir"]) if suite["results_dir"] else None
        return view

    def _db_detail(self, key: str) -> Optional[Dict[str, Any]]:
        row = next((r for r in self._db_suite_runs(1000) if r["key"] == key), None)
        if row is None:
            return None
        return {
            "key": key, "source": "db", "plan": row["plan"], "run_id": "", "state": "done",
            "phase": "done", "overall_status": row["status"], "started_at": row["started_at"],
            "finished_at": None, "totals": {"pass": row["pass"], "fail": row["fail"], "skip": row["skip"],
                                            "duration": row["duration"]},
            "suites": [{"name": row["suites"][0], "status": row["status"], "devices": row["devices"],
                        "results_dir": row["results_dir"], "progress": {},
                        "result": self.result_tables(row["results_dir"])}],
            "timeline": [], "reports": [], "triage_html": "", "flags": {},
        }

    def result_tables(self, results_dir: str) -> Optional[Dict[str, Any]]:
        """Module table and failed tests from TradeFed's test_result.xml (cached by mtime)."""
        xml = Path(results_dir) / "test_result.xml"
        try:
            mtime = xml.stat().st_mtime
        except OSError:
            return None
        with self._xml_lock:
            cached = self._xml_cache.get(str(xml))
            if cached and cached[0] == mtime:
                return cached[1]
        from xts_agent.results.result_parser import ResultParser

        try:
            parsed = ResultParser().parse_xml(xml)
        except Exception as exc:
            return {"error": f"cannot parse {xml}: {type(exc).__name__}"}
        modules: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for m in parsed.modules:
            total = len(m.test_cases) or (m.pass_count + m.fail_count)
            skipped = sum(1 for t in m.test_cases if t.result not in ("PASS", "FAIL"))
            if m.test_cases:  # authoritative: module attributes are not refreshed by every retry
                m_pass = sum(1 for t in m.test_cases if t.result == "PASS")
                m_fail = sum(1 for t in m.test_cases if t.result == "FAIL")
            else:
                m_pass, m_fail = m.pass_count, m.fail_count
            modules.append(
                {
                    "abi": m.abi,
                    "name": m.name,
                    "done": m.done,
                    "pass": m_pass,
                    "fail": m_fail,
                    "skip": skipped,
                    "total": total,
                    "runtime_secs": round((m.runtime or 0) / 1000),
                    "reason": m.reason,
                }
            )
            for t in m.test_cases:
                if t.result == "FAIL" and len(failed) < MAX_FAILED_TESTS:
                    failed.append(
                        {
                            "module": f"{m.abi} {m.name}".strip(),
                            "test": f"{t.class_name}#{t.test_name}",
                            "message": (t.message or "").split("\n", 1)[0][:300],
                        }
                    )
        out = {
            "results_dir": str(results_dir),
            "device_info": {k: v for k, v in (parsed.device_info or {}).items() if isinstance(v, str)},
            "summary": parsed.summary,
            "modules_done": parsed.modules_done,
            "modules_total": parsed.modules_total,
            "devices": parsed.devices,
            "start_ms": parsed.start_ms,
            "end_ms": parsed.end_ms,
            "modules": modules,
            "failed_tests": failed,
            "failed_truncated": max(0, parsed.summary.get("fail", 0) - len(failed)),
        }
        with self._xml_lock:
            if len(self._xml_cache) > 20:
                self._xml_cache.clear()
            self._xml_cache[str(xml)] = (mtime, out)
        return out

    # ------------------------------------------------------- logs

    def log_path(self, key: str, suite: str) -> Optional[Path]:
        """The TradeFed console log a run is (or was) writing, if it is ours to show."""
        data = self.live_file(key)
        if data is None:
            data = _load(self.state_dir / "history" / f"{key}.json") if _KEY_RE.match(key) else None
        if data is None:
            for view in self.live():
                if view["run_id"] == key:
                    data = self.live_file(view["key"])
                    break
        if data is None:
            return None
        entry = (data.get("suites") or {}).get(suite) or {}
        raw = (entry.get("progress") or {}).get("log") or ""
        return Path(raw) if raw else None


def _history_row(view: Dict[str, Any]) -> Dict[str, Any]:
    totals = view.get("totals") or {}
    suites = view["suites"]

    def total(key: str, field: str) -> Optional[int]:
        if totals.get(key) is not None:
            return totals.get(key)
        values = [s.get(field) for s in suites if isinstance(s.get(field), int)]
        return sum(values) if values else None

    devices: List[str] = []
    for s in suites:
        devices.extend(d for d in s["devices"] if d not in devices)
    html_reports = [r for r in view.get("reports") or [] if r.endswith(".html")]
    return {
        "key": view["key"],
        "source": "history",
        "plan": view["plan"],
        "run_id": view["run_id"],
        "state": view["state"],
        "status": view["overall_status"] or view["state"].upper(),
        "suites": [s["name"] for s in suites],
        "started_at": view["started_at"],
        "duration": totals.get("duration")
        or ((view["finished_at"] - view["started_at"]) if view["finished_at"] and view["started_at"] else None),
        "pass": total("pass", "pass_count"),
        "fail": total("fail", "fail_count"),
        "skip": total("skip", "skip_count"),
        "devices": devices,
        "results_dir": next((s["results_dir"] for s in suites if s["results_dir"]), ""),
        "report": html_reports[0] if html_reports else "",
        "triage_html": view.get("triage_html", ""),
        "triage_groups": (view.get("triage_summary") or {}).get("groups"),
        "modules_done": total("_", "modules_done"),
        "modules_total": total("_", "modules_total"),
    }


def tail_file(path: Path, offset: int = -1, max_bytes: int = 64_000) -> Dict[str, Any]:
    """Bytes of ``path`` from ``offset`` (or its last ``max_bytes`` when offset < 0)."""
    try:
        size = path.stat().st_size
    except OSError:
        return {"text": "", "offset": 0, "size": 0, "missing": True}
    if offset < 0 or offset > size:
        offset = max(0, size - max_bytes)
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read(max_bytes)
    text = chunk.decode("utf-8", errors="replace")
    if offset and not text.startswith("\n"):
        # Started mid-line on the first read: drop the fragment
        cut = text.find("\n")
        if cut >= 0 and offset == max(0, size - max_bytes):
            text = text[cut + 1:]
    return {"text": text, "offset": offset + len(chunk), "size": size, "missing": False}


# ---------------------------------------------------------------- plans


def list_plans(plans_dir: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path in sorted(Path(plans_dir).glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            out.append({"file": path.name, "name": path.stem, "error": str(exc)[:200]})
            continue
        suites = []
        for s in data.get("suites") or []:
            if not isinstance(s, dict):
                continue
            sharding = s.get("sharding") or {}
            suites.append(
                {
                    "name": s.get("name", ""),
                    "plan": s.get("plan", ""),
                    "enabled": s.get("enabled", True),
                    "include_filters": list(s.get("include_filters") or []),
                    "exclude_filters": len(s.get("exclude_filters") or []),
                    "shard_count": sharding.get("shard_count", ""),
                    "max_retries": (s.get("retry") or {}).get("max_retries"),
                }
            )
        devs_raw = data.get("devices") or data.get("device_requirements") or {}
        if not isinstance(devs_raw, dict):
            devs_raw = {}
        raw_serials = devs_raw.get("serials")
        serials = (
            [str(x).strip() for x in raw_serials if str(x).strip()]
            if isinstance(raw_serials, list)
            else ([str(raw_serials).strip()] if raw_serials else [])
        )
        out.append(
            {
                "file": path.name,
                "name": data.get("name") or ((data.get("plan") or {}).get("name") if isinstance(data.get("plan"), dict) else "") or path.stem,
                "description": data.get("description") or ((data.get("plan") or {}).get("description") if isinstance(data.get("plan"), dict) else "") or "",
                "profile": data.get("profile", "development"),
                "min_devices": devs_raw.get("min_devices"),
                "serials": serials,
                "ai_rca": bool((data.get("ai_rca") or {}).get("enabled")),
                "suites": suites,
            }
        )
    return out


# ---------------------------------------------------------------- devices

_PROPS = ("ro.product.model", "ro.build.type", "ro.build.fingerprint", "sys.boot_completed", "ro.product.cpu.abi")


def _adb(args: List[str], timeout: float = 8) -> str:
    try:
        return subprocess.run(
            ["adb", *args], capture_output=True, text=True, timeout=timeout, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


class DevicesView:
    """adb devices + build info + lease holder + quarantine, cached briefly."""

    def __init__(self, lease_dir: Optional[str] = None, ttl_secs: float = 8):
        self.lease_dir = lease_dir
        self.ttl = ttl_secs
        self._cache: Tuple[float, List[Dict[str, Any]]] = (0.0, [])
        self._lock = threading.Lock()

    def _manager(self):
        from xts_agent.device.device_manager import DeviceManager

        return DeviceManager(lease_dir=self.lease_dir)

    def list(self, refresh: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            if not refresh and time.time() - self._cache[0] < self.ttl:
                return self._cache[1]
        rows: List[Dict[str, Any]] = []
        for line in _adb(["devices"]).splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                rows.append({"serial": parts[0], "state": parts[1]})
        online = [r for r in rows if r["state"] == "device"]

        def props(serial: str) -> Dict[str, str]:
            script = "; ".join(f"echo {p}=$(getprop {p})" for p in _PROPS)
            out = _adb(["-s", serial, "shell", script])
            values = {}
            for ln in out.splitlines():
                k, _, v = ln.strip().partition("=")
                if k in _PROPS:
                    values[k] = v.strip()
            return values

        with ThreadPoolExecutor(max_workers=8) as pool:
            for row, values in zip(online, pool.map(lambda r: props(r["serial"]), online)):
                row.update(
                    {
                        "model": values.get("ro.product.model", ""),
                        "build_type": values.get("ro.build.type", ""),
                        "abi": values.get("ro.product.cpu.abi", ""),
                        "fingerprint": values.get("ro.build.fingerprint", ""),
                        "booted": values.get("sys.boot_completed") == "1",
                    }
                )
        manager = self._manager()
        try:
            quarantined = manager.ledger.quarantined()
        except OSError:
            quarantined = {}
        for row in rows:
            try:
                row["leased_by"] = manager.leased_elsewhere(row["serial"]) or ""
            except OSError:
                row["leased_by"] = ""
            row["quarantine"] = quarantined.get(row["serial"], "")
        for serial, reason in quarantined.items():
            if not any(r["serial"] == serial for r in rows):
                rows.append({"serial": serial, "state": "absent", "leased_by": "", "quarantine": reason})
        with self._lock:
            self._cache = (time.time(), rows)
        return rows

    def release_quarantine(self, serial: str) -> bool:
        released = self._manager().ledger.release(serial)
        with self._lock:
            self._cache = (0.0, [])
        return released


def file_url(path: str, results_dir: Path) -> str:
    """/files/... URL for a path under the results dir, else ''."""
    if not path:
        return ""
    try:
        rel = Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(results_dir)))
    except ValueError:
        return ""
    return "/files/" + rel.as_posix()
