"""Fleet: one master console that controls the consoles on other lab PCs.

Every PC keeps its own agent, suite packages and USB devices and runs
``xts-agent ui`` (a *worker*). A *master* console lists those workers in
Settings and then, over the worker's existing HTTP API:

* shows every worker's devices and live runs on one page (:meth:`Fleet.status`);
* starts a plan on a chosen worker (:meth:`Fleet.dispatch`): the master's plan
  file is pushed to the worker as ``fleet-<name>.yaml`` and launched there;
* divides one suite's modules across several workers (:meth:`Fleet.split`),
  each running its share on its own devices, and merges the parts into one
  fleet run (:meth:`Fleet.runs`).

TradeFed always runs on the PC that holds the devices; nothing forwards adb
over the network. A split run produces one TradeFed result per worker and,
because each part is filtered, runs with the development profile.

Security: worker URLs and tokens come only from the saved settings file,
tokens are never returned to the browser, and only the allow-listed worker
paths below can be reached through the master.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from xts_agent import settings as host_settings

logger = logging.getLogger(__name__)

WORKER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(:\d{1,5})?$")
_GROUP_RE = re.compile(r"^fleet-\d{14}-[0-9a-f]{4}$")
MAX_WORKERS = 32
PROXY_GET = tuple(re.compile(p) for p in (
    r"^/api/(overview|devices|live|plans|runs|modules|tradefed-plans)$",
    r"^/api/runs/[A-Za-z0-9_.\-]+$",
    r"^/api/runs/[A-Za-z0-9_.\-]+/log$",
))
PROXY_POST = tuple(re.compile(p) for p in (
    r"^/api/launch$",
    r"^/api/runs/[A-Za-z0-9_.\-]+/stop$",
))
LAUNCH_KEYS = ("modules", "suite_modules", "ai_rca", "arch", "shard_count", "serials",
               "auto_retry", "resume", "dry_run")


class WorkerError(RuntimeError):
    """A worker could not be reached or refused the request."""


class Worker:
    def __init__(self, name: str, url: str, token: str = ""):
        self.name, self.url, self.token = name, url.rstrip("/"), token

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                body: Optional[Dict[str, Any]] = None, timeout: float = 8) -> Any:
        import requests

        headers = {"X-XTS-UI": "1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = requests.request(method, self.url + path, params=params, json=body, headers=headers,
                                    timeout=timeout, allow_redirects=False)
        except requests.RequestException as exc:
            raise WorkerError(f"{self.name}: unreachable ({type(exc).__name__})") from None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            detail = data.get("error") if isinstance(data, dict) else ""
            raise WorkerError(f"{self.name}: {detail or f'HTTP {resp.status_code}'}")
        return data

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Dict[str, Any], **kw: Any) -> Any:
        return self.request("POST", path, body=body, **kw)


def validate_workers(items: Any, existing: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, str]]:
    """Normalise the Settings page's worker list; a blank token keeps the saved one."""
    if not isinstance(items, list):
        raise ValueError("fleet.workers must be a list")
    if len(items) > MAX_WORKERS:
        raise ValueError(f"at most {MAX_WORKERS} workers")
    saved = {str(w.get("name")): str(w.get("token") or "") for w in existing if isinstance(w, dict)}
    out: List[Dict[str, str]] = []
    seen = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("each worker is an object with name, url and token")
        name = str(raw.get("name") or "").strip().lower()
        url = str(raw.get("url") or "").strip().rstrip("/")
        token = str(raw.get("token") or "").strip()
        if not WORKER_NAME_RE.match(name):
            raise ValueError(f"worker name {name!r}: lower-case letters, digits, '-' and '_' (max 32)")
        if name in seen:
            raise ValueError(f"worker {name} is listed twice")
        if not _URL_RE.match(url):
            raise ValueError(f"worker {name}: URL must look like http://host:8765")
        if len(token) > 256 or re.search(r"\s", token):
            raise ValueError(f"worker {name}: invalid token")
        seen.add(name)
        out.append({"name": name, "url": url, "token": token or saved.get(name, "")})
    return out


def partition(modules: Sequence[str], capacity: Dict[str, int]) -> Dict[str, List[str]]:
    """Deal ``modules`` to workers in proportion to their device count.

    Each module goes to the worker with the lowest load per device, so a PC
    with four devices gets about twice the modules of one with two. The
    result is deterministic and keeps the given module order within a part.
    """
    names = [n for n, c in capacity.items() if c > 0]
    if not names:
        raise ValueError("no worker has a free device")
    parts: Dict[str, List[str]] = {n: [] for n in names}
    for mod in modules:
        target = min(names, key=lambda n: ((len(parts[n]) + 1) / capacity[n], names.index(n)))
        parts[target].append(mod)
    return {n: p for n, p in parts.items() if p}


def _free(devices: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [d for d in devices if d.get("state") == "device" and d.get("booted", True)
            and not d.get("leased_by") and not d.get("quarantine")]


class Fleet:
    def __init__(self, settings_file: Path, results_dir: Path, plans_dir: Path):
        self.settings_file = Path(settings_file)
        self.results_dir = Path(results_dir)
        self.plans_dir = Path(plans_dir)
        self.runs_dir = self.results_dir / "fleet_runs"
        self._lock = threading.Lock()
        self._status: Tuple[float, List[Dict[str, Any]]] = (0.0, [])
        self._history: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    # ------------------------------------------------------------ registry

    def _raw_workers(self) -> List[Dict[str, Any]]:
        fleet = host_settings.load(self.settings_file).get("fleet") or {}
        return [w for w in (fleet.get("workers") or []) if isinstance(w, dict) and w.get("name") and w.get("url")]

    def workers(self) -> List[Worker]:
        return [Worker(str(w["name"]), str(w["url"]), str(w.get("token") or "")) for w in self._raw_workers()]

    def worker(self, name: str) -> Worker:
        for w in self.workers():
            if w.name == name:
                return w
        raise LookupError(f"no worker {name!r}; add it in Settings")

    def public_workers(self) -> List[Dict[str, Any]]:
        """The registry as the browser may see it: never the token."""
        return [{"name": w.name, "url": w.url, "has_token": bool(w.token),
                 "plain_http": w.url.startswith("http://")} for w in self.workers()]

    # ------------------------------------------------------------ status

    def _probe(self, w: Worker) -> Dict[str, Any]:
        row: Dict[str, Any] = {"name": w.name, "url": w.url, "online": False, "error": "", "host": "",
                               "version": "", "devices": [], "live": []}
        try:
            over = w.get("/api/overview", timeout=4)
            row.update(host=over.get("host", ""), version=over.get("version", ""))
            row["devices"] = [{**d, "worker": w.name} for d in w.get("/api/devices", timeout=20)]
            row["live"] = [{**r, "worker": w.name} for r in w.get("/api/live", timeout=6)]
            row["online"] = True
        except WorkerError as exc:
            row["error"] = str(exc).split(": ", 1)[-1]
        free = _free(row["devices"])
        row["counts"] = {
            "devices": sum(1 for d in row["devices"] if d.get("state") == "device"),
            "free": len(free),
            "running": sum(1 for r in row["live"] if r.get("state") == "running"),
        }
        row["fingerprints"] = sorted({str(d.get("fingerprint")) for d in free if d.get("fingerprint")})
        return row

    def status(self, refresh: bool = False, ttl: float = 4) -> List[Dict[str, Any]]:
        with self._lock:
            stamp, rows = self._status
            if not refresh and time.time() - stamp < ttl:
                return rows
        workers = self.workers()
        if not workers:
            rows = []
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(workers))) as pool:
                rows = list(pool.map(self._probe, workers))
        with self._lock:
            self._status = (time.time(), rows)
        return rows

    # ------------------------------------------------------------ proxy

    def proxy(self, name: str, method: str, path: str, query: Optional[Dict[str, Any]] = None,
              body: Optional[Dict[str, Any]] = None) -> Any:
        allowed = PROXY_POST if method == "POST" else PROXY_GET
        if not any(p.match(path) for p in allowed):
            raise LookupError("this worker path is not available through the master")
        slow = path in ("/api/modules", "/api/tradefed-plans", "/api/devices") or method == "POST"
        return self.worker(name).request(method, path, params=query, body=body, timeout=320 if "modules" in path or "tradefed" in path else (30 if slow else 10))

    # ------------------------------------------------------------ dispatch

    def _plan_text(self, plan: str) -> Tuple[Path, str]:
        path = (self.plans_dir / Path(plan).name).resolve()
        if path.parent != self.plans_dir.resolve() or path.suffix != ".yaml" or not path.exists():
            raise ValueError(f"unknown plan {plan!r}")
        return path, path.read_text(encoding="utf-8")

    def push_plan(self, w: Worker, plan: str) -> str:
        """Save the master's plan on the worker as ``fleet-<name>.yaml``; returns that file name."""
        path, text = self._plan_text(plan)
        stem = re.sub(r"[^a-z0-9_-]", "-", path.stem.lower())
        remote = f"fleet-{stem}"[:60]
        w.post("/api/plans/save", {"file": remote, "yaml": text, "overwrite": True}, timeout=30)
        return f"{remote}.yaml"

    def dispatch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        w = self.worker(str(body.get("worker") or ""))
        remote = self.push_plan(w, str(body.get("plan") or ""))
        launch = {"plan": remote, **{k: body[k] for k in LAUNCH_KEYS if body.get(k) not in (None, "", [], {})}}
        out = w.post("/api/launch", launch, timeout=60)
        logger.info("Fleet dispatched %s to %s as %s", body.get("plan"), w.name, out.get("run_id"))
        with self._lock:
            self._status = (0.0, [])
        return {"worker": w.name, "plan": remote, "run_id": out.get("run_id", ""), "pid": out.get("pid")}

    # ------------------------------------------------------------ split

    def _split_plan(self, body: Dict[str, Any]) -> Dict[str, Any]:
        plan = str(body.get("plan") or "")
        _, text = self._plan_text(plan)
        data = yaml.safe_load(text) or {}
        enabled = [s for s in data.get("suites") or [] if isinstance(s, dict) and s.get("enabled", True)]
        want = str(body.get("suite") or "").lower()
        if want:
            enabled = [s for s in enabled if str(s.get("name", "")).lower() == want]
        if len(enabled) != 1:
            raise ValueError("a split needs one suite: pick the suite, or a plan with one enabled suite")
        suite = enabled[0]
        suite_name = str(suite.get("name", "")).lower()
        arch = str(body.get("arch") or suite.get("arch") or data.get("arch") or "")

        rows = {r["name"]: r for r in self.status(refresh=True)}
        names = [str(n) for n in (body.get("workers") or [])] or [n for n, r in rows.items() if r["online"]]
        if not names:
            raise ValueError("no worker is online")
        raw_pins = body.get("serials")
        pins: Dict[str, Any] = raw_pins if isinstance(raw_pins, dict) else {}
        capacity: Dict[str, int] = {}
        devices: Dict[str, List[str]] = {}
        prints: Dict[str, List[str]] = {}
        for n in names:
            row = rows.get(n)
            if row is None:
                raise LookupError(f"no worker {n!r}; add it in Settings")
            if not row["online"]:
                raise RuntimeError(f"worker {n} is offline: {row['error']}")
            free = _free(row["devices"])
            pinned = [str(s) for s in (pins.get(n) or [])]
            if pinned:
                missing = sorted(set(pinned) - {d["serial"] for d in free})
                if missing:
                    raise RuntimeError(f"worker {n}: device(s) not free: {', '.join(missing)}")
                free = [d for d in free if d["serial"] in pinned]
            if not free:
                raise RuntimeError(f"worker {n} has no free device")
            capacity[n] = len(free)
            devices[n] = [d["serial"] for d in free]
            prints[n] = sorted({str(d.get("fingerprint") or "?") for d in free})
        builds = sorted({fp for fps in prints.values() for fp in fps})
        if len(builds) > 1:
            detail = "; ".join(f"{n}: {', '.join(fps)}" for n, fps in prints.items())
            raise RuntimeError(f"the free devices do not all run one build ({detail}). "
                               "Select workers on the same build, or pin devices.")

        modules = [str(m).strip() for m in (body.get("modules") or []) if str(m).strip()]
        source, suite_build = "request", ""
        if not modules:
            modules = [str(m) for m in suite.get("include_filters") or []]
            source = "plan filters"
        if not modules:
            source = f"{suite_name}-tradefed list modules"
            suite_builds: Dict[str, str] = {}
            listed: Dict[str, List[str]] = {}
            for n in names:
                got = self.proxy(n, "GET", "/api/modules", {"suite": suite_name, "arch": arch})
                suite_builds[n] = str(got.get("build") or "?")
                listed[n] = [str(m["name"]) for m in got.get("modules") or []]
            if len(set(suite_builds.values())) > 1:
                detail = ", ".join(f"{n}: {b}" for n, b in suite_builds.items())
                raise RuntimeError(f"workers have different {suite_name} packages ({detail})")
            suite_build = next(iter(suite_builds.values()))
            modules = listed[names[0]]
        if not modules:
            raise RuntimeError(f"no modules to split for {suite_name}")
        modules = list(dict.fromkeys(modules))
        parts = partition(modules, capacity)
        return {
            "plan": plan, "plan_name": data.get("name") or Path(plan).stem, "suite": suite_name, "arch": arch,
            "total_modules": len(modules), "module_source": source, "suite_build": suite_build,
            "fingerprint": builds[0] if builds else "",
            "parts": [{"worker": n, "devices": devices[n], "pinned": bool(pins.get(n)),
                       "modules": mods} for n, mods in parts.items()],
        }

    def split(self, body: Dict[str, Any], preview: bool = False) -> Dict[str, Any]:
        """Divide one suite's modules across workers; with ``preview`` nothing is started."""
        plan = self._split_plan(body)
        if preview:
            return {**plan, "parts": [{**p, "modules": len(p["modules"]), "sample": p["modules"][:6]}
                                      for p in plan["parts"]]}
        gid = f"fleet-{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(2)}"
        parts_out = []
        for part in plan["parts"]:
            req: Dict[str, Any] = {
                "worker": part["worker"], "plan": plan["plan"],
                "suite_modules": {plan["suite"]: part["modules"]},
                "arch": plan["arch"], "ai_rca": body.get("ai_rca"),
                "auto_retry": bool(body.get("auto_retry")), "dry_run": bool(body.get("dry_run")),
            }
            if part["pinned"]:
                req["serials"] = part["devices"]
            row = {"worker": part["worker"], "devices": part["devices"], "modules": len(part["modules"]),
                   "run_id": "", "error": ""}
            try:
                row["run_id"] = self.dispatch(req)["run_id"]
            except (WorkerError, LookupError, ValueError) as exc:
                row["error"] = str(exc)
                logger.error("Fleet split %s: part for %s not started: %s", gid, part["worker"], exc)
            parts_out.append(row)
        record = {"id": gid, "created_at": time.time(), "plan": plan["plan"], "plan_name": plan["plan_name"],
                  "suite": plan["suite"], "total_modules": plan["total_modules"],
                  "fingerprint": plan["fingerprint"], "dry_run": bool(body.get("dry_run")), "parts": parts_out}
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        (self.runs_dir / f"{gid}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        if not any(p["run_id"] for p in parts_out):
            raise RuntimeError("no part could be started: " + "; ".join(p["error"] for p in parts_out))
        return self.run(gid)

    # ------------------------------------------------------------ fleet runs

    def _worker_runs(self, name: str) -> List[Dict[str, Any]]:
        """Live plus archived runs of a worker (short cache), or [] when it is unreachable."""
        with self._lock:
            hit = self._history.get(name)
            if hit and time.time() - hit[0] < 4:
                return hit[1]
        try:
            w = self.worker(name)
            rows = list(w.get("/api/live", timeout=6)) + list(w.get("/api/runs", params={"limit": 200}, timeout=10))
        except (WorkerError, LookupError):
            return []
        with self._lock:
            self._history[name] = (time.time(), rows)
        return rows

    def _merge(self, record: Dict[str, Any]) -> Dict[str, Any]:
        totals = {"pass": 0, "fail": 0, "skip": 0}
        states = []
        for part in record["parts"]:
            part.setdefault("state", "")
            if part.get("error"):
                part["state"], part["status"] = "not started", "ERROR"
            elif part.get("final"):
                pass
            else:
                hit = next((r for r in self._worker_runs(part["worker"]) if r.get("run_id") == part["run_id"]), None)
                if hit is None:
                    part["state"], part["status"] = "unknown", "worker unreachable or run not reported yet"
                else:
                    t = hit.get("totals") or {}
                    part.update(
                        state=hit.get("state", ""), phase=hit.get("phase", ""),
                        status=hit.get("overall_status") or hit.get("status") or str(hit.get("state", "")).upper(),
                        counts={k: int((hit[k] if hit.get(k) is not None else t.get(k)) or 0) for k in totals},
                        key=hit.get("key", ""), started_at=hit.get("started_at"),
                        modules_done=hit.get("modules_done"),
                    )
                    part["final"] = part["state"] not in ("running", "")
            for k in totals:
                totals[k] += int((part.get("counts") or {}).get(k) or 0)
            states.append(part["state"])
        if any(s == "running" for s in states):
            state = "running"
        elif any(s == "unknown" for s in states):
            state = "unknown"
        else:
            state = "done"
        statuses = {str(p.get("status") or "") for p in record["parts"]}
        if state != "done":
            status = state.upper()
        elif statuses <= {"PASSED", "DRY_RUN"}:
            status = "DRY_RUN" if statuses == {"DRY_RUN"} else "PASSED"
        elif "ERROR" in statuses or "FAILED" in statuses:
            status = "FAILED"
        else:
            status = "INCOMPLETE"
        return {**record, "state": state, "status": status, "totals": totals}

    def _load(self, gid: str) -> Dict[str, Any]:
        if not _GROUP_RE.match(gid):
            raise LookupError("no such fleet run")
        try:
            return json.loads((self.runs_dir / f"{gid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise LookupError("no such fleet run") from None

    def run(self, gid: str) -> Dict[str, Any]:
        record = self._load(gid)
        merged = self._merge(record)
        if all(p.get("final") or p.get("error") for p in record["parts"]):
            # every part finished: keep the outcome so a retired worker does not lose it
            (self.runs_dir / f"{gid}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return merged

    def runs(self, limit: int = 30) -> List[Dict[str, Any]]:
        if not self.runs_dir.is_dir():
            return []
        files = sorted(self.runs_dir.glob("fleet-*.json"), reverse=True)[:limit]
        return [self.run(f.stem) for f in files if _GROUP_RE.match(f.stem)]

    def stop(self, gid: str) -> Dict[str, Any]:
        record = self._merge(self._load(gid))
        out = []
        for part in record["parts"]:
            if part.get("state") != "running":
                continue
            try:
                self.worker(part["worker"]).post(f"/api/runs/{part['run_id']}/stop", {}, timeout=15)
                out.append({"worker": part["worker"], "stopping": True})
            except (WorkerError, LookupError) as exc:
                out.append({"worker": part["worker"], "stopping": False, "error": str(exc)})
        return {"id": gid, "parts": out}
