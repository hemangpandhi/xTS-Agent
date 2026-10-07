"""test_result.xml parsing, ABI handling, suite status and per-test RCA classes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from tests import support
from tests.support import INCOMPLETE_XML, PASSING_XML, SAMPLE_XML
from xts_agent.results.result_parser import ResultParser, derive_suite_status, overall_status

setUpModule = support.setUpModule
tearDownModule = support.tearDownModule


class ResultParserTests(unittest.TestCase):
    def test_summary_uses_skipped_not_modules_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "test_result.xml"
            xml.write_text(SAMPLE_XML, encoding="utf-8")
            parsed = ResultParser().parse_xml(xml)
            self.assertEqual(parsed.summary["pass"], 2)
            self.assertEqual(parsed.summary["fail"], 1)
            self.assertEqual(parsed.summary["skip"], 2)
            failed = ResultParser().get_failed_tests(parsed)
            # ASSUMPTION_FAILURE carries <Failure> but is a skip, not a failure
            self.assertEqual(len(failed), 1)
            statuses = {tc.test_name: tc.result for m in parsed.modules for tc in m.test_cases}
            self.assertEqual(statuses["testE"], "SKIP")
            self.assertEqual(failed[0].rca_category, "Null Pointer Exception")


MULTI_ABI_XML = """<?xml version='1.0' encoding='UTF-8' standalone='no' ?>
<Result suite_name="CTS">
  <Summary pass="1" failed="1" modules_done="2" modules_total="2" />
  <Module name="CtsSample" abi="arm64-v8a" done="true" pass="1" runtime="10">
    <TestCase name="com.example.Foo"><Test result="pass" name="testA" /></TestCase>
  </Module>
  <Module name="CtsSample" abi="armeabi-v7a" done="true" pass="0" fail="1" runtime="10">
    <TestCase name="com.example.Foo">
      <Test result="fail" name="testA"><Failure message="boom" /></Test>
    </TestCase>
  </Module>
</Result>
"""


class AbiTests(unittest.TestCase):
    def test_same_test_on_two_abis_stays_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "test_result.xml"
            xml.write_text(MULTI_ABI_XML, encoding="utf-8")
            parsed = ResultParser().parse_xml(xml)
        failed = ResultParser().get_failed_tests(parsed)
        self.assertEqual([t.test_id for t in failed], ["armeabi-v7a CtsSample com.example.Foo#testA"])


class RcaClassificationTests(unittest.TestCase):
    def _tc(self, message: str, stack: str = ""):
        from xts_agent.results.result_parser import TestCaseResult

        return TestCaseResult("android.foo.cts.FooTest", "testBar", "FAIL", message, stack)

    def _analyze(self, tc, patterns_yaml: str = "patterns: []\n"):
        from xts_agent.rca.failure_classifier import FailureClassifier
        from xts_agent.rca.pattern_matcher import PatternMatcher
        from xts_agent.rca.rca_engine import RCAEngine
        from xts_agent.results.result_parser import ModuleResult, TestResults

        with tempfile.TemporaryDirectory() as tmp:
            pf = Path(tmp) / "patterns.yaml"
            pf.write_text(patterns_yaml, encoding="utf-8")
            engine = RCAEngine(
                config=MagicMock(),
                failure_classifier=FailureClassifier(),
                pattern_matcher=PatternMatcher(pf),
            )
        results = TestResults("CTS", {}, "", "", modules=[ModuleResult("M", True, 0, 1, 0, [tc])])
        return engine.analyze_failures(results).failures[0]

    def test_assertion_failure_is_product_bug_not_test_bug(self):
        stack = "java.lang.AssertionError: expected 1\n at android.foo.cts.FooTest.testBar"
        self.assertEqual(self._analyze(self._tc("expected 1", stack)).classification.name, "PRODUCT_BUG")

    def test_not_found_is_not_infrastructure(self):
        tc = self._tc("Activity not found: com.android.car.settings")
        self.assertEqual(self._analyze(tc).classification.name, "PRODUCT_BUG")

    def test_device_unavailable_is_infrastructure(self):
        tc = self._tc("", "com.android.tradefed.device.DeviceNotAvailableException: offline")
        self.assertEqual(self._analyze(tc).classification.name, "INFRASTRUCTURE_FAILURE")

    def test_curated_pattern_beats_rules(self):
        patterns = (
            "patterns:\n  - pattern: 'DeviceNotAvailableException.*known-lab-issue'\n"
            "    classification: TEST_BUG\n    description: tracked\n"
        )
        tc = self._tc("", "DeviceNotAvailableException known-lab-issue")
        result = self._analyze(tc, patterns)
        self.assertEqual(result.classification.name, "TEST_BUG")
        self.assertEqual(result.root_cause, "tracked")


class SuiteStatusTests(unittest.TestCase):
    def _parse(self, xml_text: str):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "test_result.xml"
            xml.write_text(xml_text, encoding="utf-8")
            return ResultParser().parse_xml(xml)

    def test_exit_zero_without_results_is_failed(self):
        status, reason = derive_suite_status(None, exec_success=True)
        self.assertEqual(status, "FAILED")
        self.assertIn("no test_result.xml", reason)

    def test_incomplete_modules_are_not_passed(self):
        parsed = self._parse(INCOMPLETE_XML)
        self.assertEqual((parsed.modules_done, parsed.modules_total), (421, 1251))
        status, reason = derive_suite_status(parsed, exec_success=True)
        self.assertEqual(status, "INCOMPLETE")
        self.assertIn("421 of 1251", reason)

    def test_failures_win_over_incomplete(self):
        status, _ = derive_suite_status(self._parse(SAMPLE_XML), exec_success=True)
        self.assertEqual(status, "FAILED")

    def test_complete_clean_run_passes(self):
        status, reason = derive_suite_status(self._parse(PASSING_XML), exec_success=True)
        self.assertEqual((status, reason), ("PASSED", ""))

    def test_overall_status_precedence(self):
        self.assertEqual(overall_status(["PASSED", "INCOMPLETE"]), "INCOMPLETE")
        self.assertEqual(overall_status(["INCOMPLETE", "FAILED"]), "FAILED")
        self.assertEqual(overall_status(["DRY_RUN", "DRY_RUN"]), "DRY_RUN")
        self.assertEqual(overall_status(["PASSED"]), "PASSED")
        self.assertEqual(overall_status([]), "FAILED")


if __name__ == "__main__":
    unittest.main()
