"""Golden tests on real TradeFed output from this lab (see tests/golden/README.md).

Synthetic fixtures only exercise the shapes we thought of; these pin the
parser, completeness gate, results-dir detection, heartbeat and grouping to
what CTS 17_r2 actually wrote. Result counts were taken independently of
the agent (ElementTree / grep over the source files); the grouping numbers
are a snapshot of current behaviour, so a change there must be deliberate.
"""

from __future__ import annotations

import gzip
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from tests import support
from xts_agent.execution.progress import TradefedProgress
from xts_agent.execution.tradefed_runner import TradefedRunner
from xts_agent.results.result_parser import ResultParser, derive_suite_status, has_unexecuted_modules
from xts_agent.triage.signature import group_failures

setUpModule = support.setUpModule
tearDownModule = support.tearDownModule

GOLDEN = Path(__file__).parent / "golden"


def _text(name: str) -> str:
    return gzip.decompress((GOLDEN / name).read_bytes()).decode("utf-8")


def _parse(name: str):
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "test_result.xml"
        xml.write_text(_text(name), encoding="utf-8")
        return ResultParser().parse_xml(xml)


class FailingRunGoldenTests(unittest.TestCase):
    """15 modules cut from the 2.4 h, 11-device run 2026.10.05_21.11.12.345_5909."""

    @classmethod
    def setUpClass(cls):
        cls.results = _parse("cts_failures_subset.xml.gz")
        cls.failed = ResultParser().get_failed_tests(cls.results)

    def test_counts(self):
        # 66 pass, 216 fail, 132 ASSUMPTION_FAILURE + 1 IGNORED (= skip)
        self.assertEqual(self.results.summary, {"pass": 66, "fail": 216, "skip": 133, "error": 0})
        self.assertEqual(len(self.failed), 216)

    def test_interrupted_modules_make_it_incomplete(self):
        self.assertEqual((self.results.modules_done, self.results.modules_total), (12, 15))
        self.assertFalse(self.results.is_complete)
        suite = type("S", (), {"details": self.results})()
        self.assertTrue(has_unexecuted_modules(suite))
        # Failures outrank INCOMPLETE; retries then re-run NOT_EXECUTED too
        self.assertEqual(derive_suite_status(self.results, exec_success=True)[0], "FAILED")

    def test_failures_keep_abi_and_parameterized_module_names(self):
        by_module = Counter(t.module for t in self.failed)
        self.assertEqual(by_module["x86_64 CtsWindowManagerDeviceInput"], 72)
        self.assertEqual(by_module["x86_64 CtsHostsideNetworkPolicyTests"], 20)
        self.assertEqual(by_module["x86_64 CtsHostsideNetworkPolicyTests[instant]"], 20)
        self.assertTrue(all(t.test_id.startswith("x86_64 Cts") for t in self.failed))
        self.assertEqual(len({t.test_id for t in self.failed}), 216)  # no collisions between [instant] twins

    def test_grouping(self):
        groups = group_failures(("CTS", t) for t in self.failed)
        self.assertEqual(len(groups), 38)
        self.assertEqual([g.count for g in groups[:3]], [51, 36, 22])
        # A module and its [instant] variant failing the same way are one root cause
        hostside = [g for g in groups if any("NetworkPolicyTests[instant]" in t.module for t in g.tests)]
        self.assertTrue(hostside)
        for g in hostside:
            self.assertEqual(
                {t.module for t in g.tests},
                {"x86_64 CtsHostsideNetworkPolicyTests", "x86_64 CtsHostsideNetworkPolicyTests[instant]"},
            )


class CompleteRunGoldenTests(unittest.TestCase):
    """Console log + results of the single-module run 2026.10.06_01.02.22.903_1303."""

    def test_result_counts_and_status(self):
        results = _parse("bionic_result.xml.gz")
        self.assertEqual(results.summary["pass"], 3275)
        self.assertEqual(results.summary["fail"], 0)
        self.assertTrue(results.is_complete)
        self.assertEqual(derive_suite_status(results, exec_success=True)[0], "PASSED")

    def test_results_dir_found_from_console_log(self):
        log = _text("bionic_console.log.gz")
        with tempfile.TemporaryDirectory() as tmp:
            # The log names /opt/xts/...; point it at a temp copy of that dir
            real = "/opt/xts/android-cts/results/2026.10.06_01.02.22.903_1303"
            rdir = Path(tmp) / "results" / Path(real).name
            rdir.mkdir(parents=True)
            with gzip.open(GOLDEN / "bionic_result.xml.gz", "rb") as src, open(rdir / "test_result.xml", "wb") as dst:
                shutil.copyfileobj(src, dst)
            runner = TradefedRunner(tmp, "cts-tradefed")
            self.assertEqual(runner.find_results_dir(log.replace(real, str(rdir))), str(rdir))
            # Without a matching dir on disk it must not guess
            self.assertIsNone(runner.find_results_dir(log.replace(real, str(Path(tmp) / "gone"))))

    def test_heartbeat_on_unsharded_run(self):
        p = TradefedProgress()
        p.feed(_text("bionic_console.log.gz"))
        # The run never prints "Sharded test completed"; the module is counted as started
        self.assertEqual((p.modules_started, p.modules_completed, p.failures), (1, 0, 0))
        self.assertEqual(p.as_dict()["current"], {"0.0.0.0:6522": "CtsBionicTestCases"})


if __name__ == "__main__":
    unittest.main()
