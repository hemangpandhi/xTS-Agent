"""Per-test failure history across runs: NEW vs PERSISTENT vs FLAKY.

Only failures (plus the set of modules that actually executed) are stored,
not every passing test: a test is treated as having passed in a past run if
its module ran there and it did not fail. That keeps a full CTS run to a
few thousand rows instead of ~160k.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from xts_agent.results.result_parser import ResultParser, TestResults
from xts_agent.storage.db import Database

from .signature import compute_signature

NEW = "NEW"  # passed in the most recent comparable run(s): a regression
PERSISTENT = "PERSISTENT"  # failed every time it ran in the window
FLAKY = "FLAKY"  # both passed and failed within the window
NO_HISTORY = "NO_HISTORY"  # never executed in a recorded earlier run

LABEL_PRIORITY = (NEW, FLAKY, PERSISTENT, NO_HISTORY)


@dataclass
class TestHistory:
    label: str
    runs_considered: int = 0
    failures_in_window: int = 0
    last_pass_build: str = ""
    first_fail_build: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS triage_runs (
    id {pk},
    run_key TEXT UNIQUE,
    suite TEXT,
    plan TEXT,
    fingerprint TEXT,
    started_ms BIGINT,
    results_dir TEXT,
    recorded_at {float}
);
CREATE TABLE IF NOT EXISTS triage_modules (
    run_id BIGINT, module_id TEXT,
    PRIMARY KEY (run_id, module_id)
);
CREATE TABLE IF NOT EXISTS triage_failures (
    run_id BIGINT, module_id TEXT, test_id TEXT, signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_failures_test ON triage_failures(test_id);
CREATE INDEX IF NOT EXISTS idx_failures_run ON triage_failures(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_suite ON triage_runs(suite, started_ms)
"""


class FailureHistory:
    def __init__(self, db: str | Path | Database, window: int = 5):
        self.db = db if isinstance(db, Database) else Database(db)
        self.window = window
        self.db.ddl(_SCHEMA)

    @staticmethod
    def run_key(suite: str, results: TestResults, results_dir: str = "") -> str:
        return f"{suite.upper()}:{results.start_ms or Path(results_dir).name}"

    def has_run(self, run_key: str) -> bool:
        return self.db.query_one("SELECT 1 FROM triage_runs WHERE run_key = ?", (run_key,)) is not None

    def record_run(
        self, suite: str, results: TestResults, plan: str = "", results_dir: str = ""
    ) -> Optional[int]:
        """Store a run's executed modules and failures (idempotent; retries upsert)."""
        key = self.run_key(suite, results, results_dir)
        # Only completed modules prove "not failing => passed"; in a partial
        # module an absent failure may just mean the test never ran.
        executed = [m.module_id for m in results.modules if m.done]
        failures = [
            (m.module_id, tc.test_id, compute_signature(tc))
            for m in results.modules
            for tc in m.test_cases
            if tc.result in ("FAIL", "ERROR")
        ]
        with self.db.transaction() as tx:
            existing = tx.query("SELECT id, results_dir FROM triage_runs WHERE run_key = ?", (key,))
            if existing:
                # `run retry` sessions keep the invocation's start time: same run.
                # Keep the latest (cumulative) session's outcome.
                run_id, stored_dir = existing[0]
                if not results_dir or Path(results_dir).name <= Path(stored_dir or "").name:
                    return None
                tx.execute("DELETE FROM triage_modules WHERE run_id = ?", (run_id,))
                tx.execute("DELETE FROM triage_failures WHERE run_id = ?", (run_id,))
                tx.execute(
                    "UPDATE triage_runs SET results_dir = ?, recorded_at = ? WHERE id = ?",
                    (results_dir, time.time(), run_id),
                )
            else:
                run_id = tx.insert_id(
                    "INSERT INTO triage_runs (run_key, suite, plan, fingerprint, started_ms, "
                    "results_dir, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        suite.upper(),
                        plan,
                        results.device_info.get("build_fingerprint", ""),
                        results.start_ms or int(time.time() * 1000),
                        results_dir,
                        time.time(),
                    ),
                )
            tx.executemany(
                "INSERT INTO triage_modules VALUES (?, ?) ON CONFLICT DO NOTHING",
                [(run_id, m) for m in executed],
            )
            tx.executemany(
                "INSERT INTO triage_failures VALUES (?, ?, ?, ?)", [(run_id, *f) for f in failures]
            )
            return run_id

    def import_results_dir(self, suite: str, results_dir: str | Path, plan: str = "") -> Optional[int]:
        xml = Path(results_dir) / "test_result.xml"
        results = ResultParser().parse_xml(xml)
        return self.record_run(suite, results, plan=plan, results_dir=str(results_dir))

    def flaky_tests(self, suite: str, window: Optional[int] = None) -> List[str]:
        """Tests that both failed and (by module completion) passed in recent runs."""
        with self.db.transaction() as tx:
            runs = [r[0] for r in tx.query(
                "SELECT id FROM triage_runs WHERE suite = ? ORDER BY started_ms DESC LIMIT ?",
                (suite.upper(), window or self.window),
            )]
            if len(runs) < 2:
                return []
            marks = ",".join("?" * len(runs))
            fails = tx.query(
                f"SELECT run_id, module_id, test_id FROM triage_failures WHERE run_id IN ({marks})", runs
            )
            executed = set(tx.query(
                f"SELECT run_id, module_id FROM triage_modules WHERE run_id IN ({marks})", runs
            ))
        failed_in: Dict[str, set] = {}
        module_of: Dict[str, str] = {}
        for run_id, module, test in fails:
            failed_in.setdefault(test, set()).add(run_id)
            module_of[test] = module
        return sorted(
            test for test, fruns in failed_in.items()
            if any((r, module_of[test]) in executed and r not in fruns for r in runs)
        )

    def run_stats(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Per recorded run: failures and distinct root causes, oldest first."""
        rows = self.db.query(
            "SELECT r.suite, r.started_ms, r.fingerprint, COUNT(f.test_id), COUNT(DISTINCT f.signature) "
            "FROM triage_runs r LEFT JOIN triage_failures f ON f.run_id = r.id "
            "GROUP BY r.id, r.suite, r.started_ms, r.fingerprint ORDER BY r.started_ms DESC LIMIT ?",
            (limit,),
        )
        keys = ("suite", "started_ms", "fingerprint", "failures", "signatures")
        return [dict(zip(keys, row)) for row in reversed(rows)]

    def _previous_runs(self, tx: Any, suite: str, before_ms: int) -> List[Tuple[int, str]]:
        return tx.query(
            "SELECT id, fingerprint FROM triage_runs WHERE suite = ? AND started_ms < ? "
            "ORDER BY started_ms DESC LIMIT ?",
            (suite.upper(), before_ms, self.window),
        )

    def classify(
        self, suite: str, results: TestResults, test_ids: Iterable[Tuple[str, str]]
    ) -> Dict[str, TestHistory]:
        """Label each (module_id, test_id) failing now against earlier runs."""
        before = results.start_ms or int(time.time() * 1000)
        current_build = results.device_info.get("build_fingerprint", "")
        out: Dict[str, TestHistory] = {}
        with self.db.transaction() as tx:
            runs = self._previous_runs(tx, suite, before)  # newest first
            if not runs:
                return {test: TestHistory(NO_HISTORY) for _, test in test_ids}
            run_ids = [r[0] for r in runs]
            marks = ",".join("?" * len(run_ids))
            executed = set(tx.query(
                f"SELECT run_id, module_id FROM triage_modules WHERE run_id IN ({marks})", run_ids
            ))
            failed = set(tx.query(
                f"SELECT run_id, test_id FROM triage_failures WHERE run_id IN ({marks})", run_ids
            ))
        for module, test in test_ids:
            # outcomes newest first, only for runs where the test is known to have run
            outcomes = [
                (fp, (run_id, test) in failed)
                for run_id, fp in runs
                if (run_id, module) in executed or (run_id, test) in failed
            ]
            if not outcomes:
                out[test] = TestHistory(NO_HISTORY)
                continue
            fails = sum(1 for _, f in outcomes if f)
            if fails == 0:
                label = NEW  # it passed every earlier time it ran
            elif fails == len(outcomes):
                label = PERSISTENT
            else:
                label = FLAKY
            out[test] = TestHistory(
                label=label,
                runs_considered=len(outcomes),
                failures_in_window=fails,
                last_pass_build=next((fp for fp, f in outcomes if not f), ""),
                # oldest build in the window for a persistent failure, else this build
                first_fail_build=outcomes[-1][0] if label == PERSISTENT else current_build,
            )
        return out


def group_label(labels: Iterable[str]) -> str:
    """Most actionable label among a group's tests (NEW first)."""
    present = set(labels)
    return next((label for label in LABEL_PRIORITY if label in present), NO_HISTORY)
