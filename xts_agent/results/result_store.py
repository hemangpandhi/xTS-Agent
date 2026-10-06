"""Per-suite run summaries (SQLite by default, optional PostgreSQL).

One row per suite execution with its counts, completeness, build, devices
and duration. Per-test failure data lives in the triage history tables, so
no run stores a whole results blob (a full CTS run serialised to JSON is
well over 100 MB). An older ``runs`` table, if present, is left untouched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from xts_agent.storage.db import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS suite_runs (
    id {pk},
    plan TEXT,
    profile TEXT,
    suite TEXT,
    status TEXT,
    pass_count INTEGER,
    fail_count INTEGER,
    skip_count INTEGER,
    modules_done INTEGER,
    modules_total INTEGER,
    duration {float},
    device_count INTEGER,
    devices TEXT,
    fingerprint TEXT,
    session_id INTEGER,
    results_dir TEXT,
    retry_count INTEGER,
    started_ms BIGINT,
    recorded_at {float}
);
CREATE INDEX IF NOT EXISTS idx_suite_runs_suite ON suite_runs(suite, recorded_at)
"""

_COLUMNS = (
    "id", "plan", "profile", "suite", "status", "pass_count", "fail_count", "skip_count",
    "modules_done", "modules_total", "duration", "device_count", "devices", "fingerprint",
    "session_id", "results_dir", "retry_count", "started_ms", "recorded_at",
)


class ResultStore:
    def __init__(self, db: str | Path | Database = "results.db"):
        self.db = db if isinstance(db, Database) else Database(db)
        self.db.ddl(_SCHEMA)

    def save_suite_run(self, plan_name: str, profile: str, suite_result: Any) -> None:
        details = suite_result.details
        devices = list(suite_result.device_serials or [])
        self.db.execute(
            "INSERT INTO suite_runs (plan, profile, suite, status, pass_count, fail_count, "
            "skip_count, modules_done, modules_total, duration, device_count, devices, "
            "fingerprint, session_id, results_dir, retry_count, started_ms, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_name,
                profile,
                suite_result.name.upper(),
                suite_result.status,
                suite_result.pass_count,
                suite_result.fail_count,
                suite_result.skip_count,
                getattr(details, "modules_done", 0) or 0,
                getattr(details, "modules_total", 0) or 0,
                float(suite_result.duration or 0),
                len(devices),
                json.dumps(devices),
                (getattr(details, "device_info", None) or {}).get("build_fingerprint", ""),
                int(suite_result.session_id or 0),
                suite_result.results_dir or "",
                int(suite_result.retry_count or 0),
                int(getattr(details, "start_ms", 0) or 0),
                time.time(),
            ),
        )

    def recent_runs(self, suite: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Newest first."""
        sql = f"SELECT {', '.join(_COLUMNS)} FROM suite_runs"
        params: tuple = ()
        if suite:
            sql += " WHERE suite = ?"
            params = (suite.upper(),)
        sql += " ORDER BY recorded_at DESC LIMIT ?"
        rows = self.db.query(sql, params + (limit,))
        out = [dict(zip(_COLUMNS, row)) for row in rows]
        for row in out:
            row["devices"] = json.loads(row["devices"] or "[]")
        return out

    def estimate_device_hours(self, suite_name: str, window_runs: int = 5) -> Optional[float]:
        """Median device-hours (duration x devices) of recent runs that executed."""
        samples = [
            r["duration"] / 3600 * r["device_count"]
            for r in self.recent_runs(suite_name, window_runs * 3)
            if r["duration"] > 0 and r["device_count"] and r["status"] in ("PASSED", "FAILED", "INCOMPLETE")
        ][:window_runs]
        if not samples:
            return None
        samples.sort()
        return samples[len(samples) // 2]

    def get_trends(self, suite_name: str, limit: int = 50) -> Dict[str, List[float]]:
        """Pass rate per run, oldest first."""
        rates = []
        for r in reversed(self.recent_runs(suite_name, limit)):
            total = r["pass_count"] + r["fail_count"]
            rates.append(round(r["pass_count"] / total * 100, 2) if total else 0.0)
        return {"pass_rate_trend": rates}
