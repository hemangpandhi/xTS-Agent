"""SQLite/PostgreSQL store and artifact archive."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from tests import support
from tests import test_triage as triage_tests  # module import keeps its TestCases out of this module
from xts_agent.execution.test_plan_executor import SuiteResult

setUpModule = support.setUpModule
tearDownModule = support.tearDownModule


class StorageBackendTests(unittest.TestCase):
    """Same scenarios on SQLite and, when XTS_TEST_POSTGRES_URL is set, PostgreSQL."""

    TABLES = ("triage_runs", "triage_modules", "triage_failures", "suite_runs", "ai_cache")

    def _backends(self):
        import os

        from xts_agent.storage.db import Database

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        backends = [Database(Path(tmp.name) / "t.db")]
        url = os.environ.get("XTS_TEST_POSTGRES_URL")
        if url:
            pg = Database(url)

            def drop():
                for table in self.TABLES:
                    pg.execute(f"DROP TABLE IF EXISTS {table}")

            drop()
            self.addCleanup(drop)  # leave the shared database as we found it
            backends.append(pg)
        return backends

    def test_history_store_and_cache_on_each_backend(self):
        from xts_agent.results.result_store import ResultStore
        from xts_agent.triage.ai_rca import _Cache
        from xts_agent.triage.history import FailureHistory

        run = triage_tests.FailureHistoryTests._run
        for db in self._backends():
            with self.subTest(backend=db.dialect):
                h = FailureHistory(db)
                # 13-digit epoch ms must fit (BIGINT on Postgres)
                self.assertIsNotNone(h.record_run("CTS", run(None, 1791202307566, "b1", {"t": "PASS", "f": "FAIL"}), results_dir="r1"))
                self.assertIsNotNone(h.record_run("CTS", run(None, 1791202307567, "b2", {"t": "FAIL", "f": "PASS"}), results_dir="r2"))
                # retry of the second invocation replaces it
                self.assertIsNotNone(h.record_run("CTS", run(None, 1791202307567, "b2", {"t": "FAIL", "f": "FAIL"}), results_dir="r3"))
                self.assertEqual(h.flaky_tests("CTS"), ["x86_64 CtsM c.T#t"])
                cur = run(None, 1791202307999, "b3", {"t": "FAIL", "f": "FAIL"})
                labels = triage_tests.FailureHistoryTests._classify(None, h, cur)
                self.assertEqual((labels["t"].label, labels["f"].label), ("FLAKY", "PERSISTENT"))

                store = ResultStore(db)
                store.save_suite_run("p", "certification", SuiteResult("cts", "FAILED", 9, 1, 0, 7200.0, 3, "", 1, device_serials=["a", "b"]))
                self.assertEqual(store.estimate_device_hours("CTS"), 4.0)
                self.assertEqual(store.get_trends("CTS"), {"pass_rate_trend": [90.0]})

                cache = _Cache(db)
                cache.put("sig", "m", {"root_cause": "a"})
                cache.put("sig", "m", {"root_cause": "b"})  # upsert
                self.assertEqual(cache.get("sig", "m"), {"root_cause": "b"})

    def test_database_repr_hides_password(self):
        from xts_agent.storage.db import Database

        self.assertNotIn("s3cret", repr(Database("postgresql://xts:s3cret@db.lab:5432/xts")))


class ArtifactStoreTests(unittest.TestCase):
    def test_reuses_tradefed_zip_zips_otherwise_and_respects_selection(self):
        from xts_agent.storage.artifacts import ArtifactConfig, ArtifactStore, record_artifacts
        from xts_agent.storage.db import Database

        client = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with_zip = tmp / "2026.10.06_01"
            with_zip.mkdir()
            (with_zip / "test_result.xml").write_text("<Result/>")
            (tmp / "2026.10.06_01.zip").write_text("tf zip")
            no_zip = tmp / "2026.10.07_01"
            no_zip.mkdir()
            (no_zip / "test_result.xml").write_text("<Result/>")
            report = tmp / "r.json"
            report.write_text("{}")
            suites = {
                "CTS": SuiteResult("CTS", "PASSED", 1, 0, 0, 1, 1, str(with_zip), 0),
                "VTS": SuiteResult("VTS", "FAILED", 0, 0, 0, 1, 1, str(no_zip), 0),
                "STS": SuiteResult("STS", "FAILED", 0, 0, 0, 1, 0, "", 0),  # no results dir
            }
            cfg = ArtifactConfig(store="s3", bucket="b", upload=["results"])
            urls = ArtifactStore(cfg, client).upload_run("My Plan", suites, [report])
            self.assertEqual(sorted(urls), ["CTS/results", "VTS/results"])  # reports not selected
            uploaded = {Path(c[0][0]).name for c in client.upload_file.call_args_list}
            self.assertIn("2026.10.06_01.zip", uploaded)  # TradeFed's own zip reused
            self.assertTrue(urls["CTS/results"].startswith("s3://b/xts/My_Plan/"))
            db = Database(tmp / "db.sqlite")
            record_artifacts(db, "My Plan", urls)
            self.assertEqual(db.query_one("SELECT COUNT(*) FROM run_artifacts")[0], 2)
        self.assertEqual(ArtifactStore(ArtifactConfig()).upload_run("p", suites, []), {})  # disabled


if __name__ == "__main__":
    unittest.main()
