"""Host-wide device reliability ledger with automatic quarantine.

A device that repeatedly fails to come back from reboots or drops offline
mid-suite poisons every run it is sharded into (its modules end up
incomplete or failed). After ``threshold`` consecutive failures it is
quarantined for ``hours`` so allocation skips it until a lab engineer looks
at it (or the quarantine expires). Any success resets the count.

The ledger is a JSON file shared by every agent process on the host, so
read-modify-write cycles are serialised with an exclusive flock.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)


class DeviceLedger:
    def __init__(self, path: str | Path, threshold: int = 3, hours: float = 24.0):
        self.path = Path(path)
        self.threshold = max(1, int(threshold))
        self.hours = float(hours)

    @contextmanager
    def _locked(self) -> Iterator[Dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            try:
                os.fchmod(fd, 0o666)
            except PermissionError:
                pass
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.pread(fd, 1 << 20, 0).decode() or "{}"
            try:
                data = json.loads(raw)
            except ValueError:
                logger.warning("Device ledger %s was corrupt; starting fresh", self.path)
                data = {}
            yield data
            payload = json.dumps(data, indent=2).encode()
            os.ftruncate(fd, 0)
            os.pwrite(fd, payload, 0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def record_failure(self, serial: str, reason: str) -> bool:
        """Record a failure; returns True if this put the device in quarantine."""
        with self._locked() as data:
            entry = data.setdefault(serial, {"consecutive_failures": 0})
            entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
            entry["last_failure"] = reason
            entry["last_failure_at"] = time.time()
            if entry["consecutive_failures"] >= self.threshold and not self._active(entry):
                entry["quarantined_until"] = time.time() + self.hours * 3600
                entry["quarantine_reason"] = (
                    f"{entry['consecutive_failures']} consecutive failures, last: {reason}"
                )
                logger.error(
                    "Quarantining %s for %.0fh: %s", serial, self.hours, entry["quarantine_reason"]
                )
                return True
        return False

    def record_success(self, serial: str) -> None:
        with self._locked() as data:
            entry = data.get(serial)
            if entry and entry.get("consecutive_failures"):
                entry["consecutive_failures"] = 0

    def quarantine_reason(self, serial: str) -> Optional[str]:
        if not self.path.exists():
            return None
        with self._locked() as data:
            entry = data.get(serial) or {}
            if self._active(entry):
                until = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["quarantined_until"]))
                return f"{entry.get('quarantine_reason', 'quarantined')} (until {until})"
        return None

    def quarantined(self) -> Dict[str, str]:
        if not self.path.exists():
            return {}
        with self._locked() as data:
            return {
                serial: entry.get("quarantine_reason", "")
                for serial, entry in data.items()
                if self._active(entry)
            }

    def release(self, serial: str) -> bool:
        with self._locked() as data:
            entry = data.get(serial)
            if not entry or not self._active(entry):
                return False
            entry.pop("quarantined_until", None)
            entry["consecutive_failures"] = 0
            logger.info("Released %s from quarantine", serial)
            return True

    @staticmethod
    def _active(entry: Dict[str, Any]) -> bool:
        return float(entry.get("quarantined_until") or 0) > time.time()
