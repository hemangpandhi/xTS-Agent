"""Checkpointed run state so an interrupted plan can resume instead of restarting.

A full certification plan runs for days; a host reboot or CI cancel at hour
40 must not throw that work away. After every suite milestone the executor
records, per suite, the TradeFed session/results dir and the build it ran
on. ``run --resume`` then skips suites that passed and continues the others
with ``run retry`` on the same build.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)


class RunState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data: Dict[str, Any] = {}

    @classmethod
    def for_plan(cls, results_dir: str | Path, plan_name: str) -> "RunState":
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", plan_name).strip("_") or "plan"
        return cls(Path(results_dir) / "run_state" / f"{slug}.json")

    def load(self) -> bool:
        """Load an unfinished state; returns False if none can be resumed."""
        if not self.path.exists():
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable run state %s: %s", self.path, exc)
            return False
        if data.get("complete"):
            logger.info("Previous run of %s already completed; nothing to resume", data.get("plan_name"))
            return False
        self.data = data
        return True

    def start(self, plan_name: str, profile: str) -> None:
        with self._lock:
            self.data = {
                "plan_name": plan_name,
                "profile": profile,
                "started_at": time.time(),
                "complete": False,
                "suites": {},
            }
            self._write()

    def attach_process(self, **flags: Any) -> None:
        """Record which process owns this run (fresh or resumed).

        Live views use ``pid`` to tell a running plan from a dead checkpoint
        and to send the graceful-cancel SIGTERM; ``run_id`` names the archive.
        """
        from xts_agent.utils.logger import run_id

        with self._lock:
            self.data.update(
                {
                    "run_id": run_id(),
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "user": getpass.getuser(),
                    "attached_at": time.time(),
                    "phase": "running",
                    "flags": flags,
                }
            )
            self.data.setdefault("timeline", []).append({"at": time.time(), "event": "started", **flags})
            self._write()

    def set_phase(self, phase: str, **extra: Any) -> None:
        """running -> rca/triage -> reports -> done (or cancelled / error)."""
        with self._lock:
            self.data["phase"] = phase
            self.data.update(extra)
            self.data.setdefault("timeline", []).append({"at": time.time(), "event": phase})
            self._write()

    def suite_retrying(self, name: str, attempt: int) -> None:
        with self._lock:
            entry = self.data.setdefault("suites", {}).setdefault(name, {})
            entry["status"] = "RETRYING"
            entry["retry_attempt"] = attempt
            entry["updated_at"] = time.time()
            self.data.setdefault("timeline", []).append(
                {"at": time.time(), "event": f"{name} retry {attempt}"}
            )
            self._write()

    def archive(self) -> Optional[Path]:
        """Copy the final state to ``run_state/history/<run_id>.json``.

        The live file is per plan and is overwritten by the next run; the
        archive keeps one record per run for the UI's run history.
        """
        rid = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(self.data.get("run_id") or "")).strip("_")
        if not rid or not self.path.exists():
            return None
        dest = self.path.parent / "history" / f"{rid}.json"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                shutil.copyfile(self.path, dest)
            return dest
        except OSError as exc:
            logger.warning("Could not archive run state to %s: %s", dest, exc)
            return None

    def suite(self, name: str) -> Optional[Dict[str, Any]]:
        return (self.data.get("suites") or {}).get(name)

    def suite_started(
        self, name: str, before: Iterable[str], fingerprint: str, serials: Iterable[str]
    ) -> None:
        with self._lock:
            self.data.setdefault("suites", {})[name] = {
                "status": "RUNNING",
                "session_id": None,
                "results_dir": "",
                "before": sorted(before),
                "fingerprint": fingerprint,
                "device_serials": list(serials),
                "retry_count": 0,
                "updated_at": time.time(),
            }
            self._write()

    def suite_updated(self, suite_result: Any) -> None:
        with self._lock:
            entry = self.data.setdefault("suites", {}).setdefault(suite_result.name, {})
            entry.update(
                {
                    "status": suite_result.status,
                    "session_id": suite_result.session_id,
                    "results_dir": suite_result.results_dir or "",
                    "retry_count": suite_result.retry_count,
                    "updated_at": time.time(),
                }
            )
            details = getattr(suite_result, "details", None)
            for key, src in (("pass_count", suite_result), ("fail_count", suite_result),
                             ("skip_count", suite_result), ("modules_done", details),
                             ("modules_total", details)):
                value = getattr(src, key, None)
                if isinstance(value, int):
                    entry[key] = value
            if getattr(suite_result, "error_message", ""):
                entry["error"] = str(suite_result.error_message)[:500]
            self.data.setdefault("timeline", []).append(
                {"at": time.time(), "event": f"{suite_result.name} {suite_result.status}"}
            )
            self._write()

    def suite_progress(self, name: str, progress: Dict[str, Any]) -> None:
        """Heartbeat of a running suite (also a status file for dashboards/ops)."""
        with self._lock:
            entry = self.data.setdefault("suites", {}).setdefault(name, {})
            entry["progress"] = progress
            self.data["heartbeat_at"] = time.time()
            self._write()

    def mark_complete(self, overall_status: str) -> None:
        with self._lock:
            self.data["complete"] = True
            self.data["overall_status"] = overall_status
            self._write()

    def _write(self) -> None:
        # Atomic replace so a crash mid-write never corrupts the checkpoint
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
