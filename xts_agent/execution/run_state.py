"""Checkpointed run state so an interrupted plan can resume instead of restarting.

A full certification plan runs for days; a host reboot or CI cancel at hour
40 must not throw that work away. After every suite milestone the executor
records, per suite, the TradeFed session/results dir and the build it ran
on. ``run --resume`` then skips suites that passed and continues the others
with ``run retry`` on the same build.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
                    "session_id": suite_result.session_id or None,
                    "results_dir": suite_result.results_dir or "",
                    "retry_count": suite_result.retry_count,
                    "updated_at": time.time(),
                }
            )
            self._write()

    def mark_complete(self, overall_status: str) -> None:
        with self._lock:
            self.data["complete"] = True
            self.data["overall_status"] = overall_status
            self._write()

    def _write(self) -> None:
        # Atomic replace so a crash mid-write never corrupts the checkpoint
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
