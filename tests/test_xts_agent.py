"""Unit tests for production-hardened xTS Agent."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from xts_agent.config_loader import ConfigLoader, SuiteConfig
from xts_agent.execution.shard_manager import ShardManager
from xts_agent.execution.test_plan_executor import PlanResult, SuiteResult, TestPlanExecutor
from xts_agent.execution.tradefed_runner import TradefedRunner
from xts_agent.results.result_parser import ResultParser, derive_suite_status, overall_status


SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='no' ?>
<Result start_display="now" end_display="later" suite_name="CTS">
  <Build device_serial="emulator-5554" />
  <Summary pass="2" failed="1" skipped="2" modules_done="3" modules_total="3" />
  <Module name="CtsSample" done="true" pass="2" fail="1" runtime="10">
    <TestCase name="com.example.Foo">
      <Test result="pass" name="testA" />
      <Test result="pass" name="testB" />
      <Test result="fail" name="testC">
        <Failure message="boom">
          <StackTrace>java.lang.NullPointerException
at com.example.Foo.testC</StackTrace>
        </Failure>
      </Test>
      <Test result="IGNORED" name="testD" />
      <Test result="ASSUMPTION_FAILURE" name="testE">
        <Failure message="got: &lt;false&gt;, expected: is &lt;true&gt;">
          <StackTrace>org.junit.AssumptionViolatedException: got: false
at org.junit.Assume.assumeTrue(Assume.java:50)</StackTrace>
        </Failure>
      </Test>
    </TestCase>
  </Module>
</Result>
"""


PASSING_XML = """<?xml version='1.0' encoding='UTF-8' standalone='no' ?>
<Result suite_name="CTS">
  <Summary pass="1" failed="0" modules_done="1" modules_total="1" />
  <Module name="CtsSample" abi="x86_64" done="true" pass="1" runtime="10">
    <TestCase name="com.example.Foo"><Test result="pass" name="testA" /></TestCase>
  </Module>
</Result>
"""

# Shape of the real interrupted run 2026.10.04_16.16.31.269_4285 (421 of 1251)
INCOMPLETE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='no' ?>
<Result suite_name="CTS">
  <Summary pass="161222" failed="0" modules_done="421" modules_total="1251" />
  <Module name="CtsSample" abi="x86_64" done="false" pass="1" runtime="10">
    <TestCase name="com.example.Foo"><Test result="pass" name="testA" /></TestCase>
  </Module>
</Result>
"""


class ConfigLoaderTests(unittest.TestCase):
    def test_loads_smoke_plan(self):
        loader = ConfigLoader("config/test_plans/smoke_test.yaml")
        plan = loader.load_plan()
        self.assertEqual(plan.name, "Official CTS Run")
        self.assertEqual(len(plan.suites), 1)
        self.assertEqual(plan.suites[0].include_filters, ["CtsBionicTestCases"])
        self.assertEqual(plan.suites[0].retry.max_retries, 0)

    def test_loads_full_certification_schema(self):
        loader = ConfigLoader("config/test_plans/full_certification.yaml")
        plan = loader.load_plan()
        self.assertEqual(plan.name, "full_aaos_certification")
        self.assertGreaterEqual(plan.devices.min_devices, 6)
        self.assertEqual(plan.devices.device_type, "any")

        cts = next(s for s in plan.suites if s.name == "cts")
        self.assertEqual(cts.priority, 1)
        self.assertEqual(cts.sharding.shard_count, "auto")
        self.assertEqual(cts.retry.max_retries, 3)
        self.assertEqual(cts.retry.isolation_grade, "REBOOT_ISOLATED")
        self.assertTrue(cts.package_path.endswith("android-cts"))
        self.assertEqual(cts.command, "cts-tradefed")
        self.assertIn("--skip-system-status-check", cts.extra_args)
        # exclude_filters.file should expand to a list (may be empty if file only comments)
        self.assertIsInstance(cts.exclude_filters, list)

        # Priority ascending: cts first
        ordered = sorted(plan.suites, key=lambda s: s.priority)
        self.assertEqual(ordered[0].name, "cts")
        self.assertEqual(ordered[-1].name, "catbox")

        self.assertTrue(plan.post_execution.suite_retry.enabled)
        self.assertTrue(plan.post_execution.rca.enabled)
        self.assertIn("html", plan.post_execution.reporting.formats)


class AiRcaConfigTests(unittest.TestCase):
    def _load(self, defaults: str, plan: str):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "defaults.yaml"
            d.write_text(defaults, encoding="utf-8")
            p = Path(tmp) / "plan.yaml"
            p.write_text("name: t\nsuites: []\n" + plan, encoding="utf-8")
            return ConfigLoader(p, defaults_path=d).load_plan()

    def test_ai_rca_loaded_from_defaults_and_plan_override(self):
        plan = self._load(
            "ai_rca:\n  enabled: true\n  provider: llama_cpp\n  llama_n_ctx: 8192\n",
            "ai_rca:\n  llama_n_ctx: 4096\n",
        )
        self.assertTrue(plan.ai_rca.enabled)
        self.assertEqual(plan.ai_rca.provider, "llama_cpp")
        self.assertEqual(plan.ai_rca.llama_n_ctx, 4096)

    def test_legacy_rca_ai_powered_enables_ai_rca(self):
        plan = self._load("rca:\n  ai_powered: true\n  ai_api_key: k\n", "")
        self.assertTrue(plan.ai_rca.enabled)
        self.assertEqual(plan.ai_rca.gemini_api_key, "k")

    def test_ai_rca_disabled_by_default(self):
        self.assertFalse(self._load("", "").ai_rca.enabled)


class ShardManagerTests(unittest.TestCase):
    def test_auto_shard_count(self):
        mgr = ShardManager(device_manager=MagicMock())
        suite = SuiteConfig(name="cts", sharding=__import__(
            "xts_agent.config_loader", fromlist=["ShardingConfig"]
        ).ShardingConfig(shard_count="auto", max_shards=16))
        self.assertEqual(mgr.calculate_shard_count(10, suite), 10)
        self.assertEqual(mgr.calculate_shard_count(20, suite), 16)

    def test_fixed_shard_capped_by_devices(self):
        mgr = ShardManager(device_manager=MagicMock())
        suite = SuiteConfig(
            name="cts",
            sharding=__import__(
                "xts_agent.config_loader", fromlist=["ShardingConfig"]
            ).ShardingConfig(shard_count=15, max_shards=16),
        )
        self.assertEqual(mgr.calculate_shard_count(4, suite), 4)


class TradefedRunnerTests(unittest.TestCase):
    def test_build_run_command_includes_serials_and_retry(self):
        runner = TradefedRunner("/tmp/android-cts", "cts-tradefed")
        cmd = runner.build_run_command(
            plan="cts",
            shard_count=2,
            retry_config={
                "max_testcase_run_count": 3,
                "retry_strategy": "RETRY_ANY_FAILURE",
                "isolation_grade": "REBOOT_ISOLATED",
                "reboot_at_last_retry": True,
            },
            exclude_filters=["BadModule"],
            include_filters=["GoodModule"],
            device_serials=["0.0.0.0:6520", "0.0.0.0:6524"],
            extra_args=["--skip-system-status-check"],
        )
        self.assertIn("-s", cmd)
        self.assertIn("0.0.0.0:6520", cmd)
        self.assertIn("0.0.0.0:6524", cmd)
        self.assertIn("--shard-count", cmd)
        self.assertIn("--max-testcase-run-count", cmd)
        self.assertIn("--retry-strategy", cmd)
        self.assertIn("--retry-isolation-grade", cmd)
        self.assertIn("--reboot-at-last-retry", cmd)
        self.assertIn("--exclude-filter", cmd)
        self.assertIn("--include-filter", cmd)
        self.assertIn("--skip-system-status-check", cmd)

    @staticmethod
    def _make_result_dir(root: Path, name: str) -> Path:
        d = root / name
        d.mkdir(parents=True)
        (d / "test_result.xml").write_text("<Result/>", encoding="utf-8")
        return d

    def test_find_results_dir_from_result_directory_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = TradefedRunner(Path(tmp) / "android-cts", "cts-tradefed")
            results = self._make_result_dir(
                Path(tmp) / "android-cts" / "results", "2026.10.06_01.02.22.903_1303"
            )
            # Real TradeFed tail: the global log line must not be mistaken for results
            output = (
                "Saved log to /tmp/tradefed_global_log_17628663554220524335.txt\n"
                f"10-06 01:04:10 I/ResultReporter: RESULT DIRECTORY            : {results}\n"
            )
            self.assertEqual(runner.find_results_dir(output), str(results))

    def test_find_results_dir_ignores_saved_log_and_missing_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = TradefedRunner(Path(tmp) / "android-cts", "cts-tradefed")
            empty = Path(tmp) / "android-cts" / "results" / "2026.01.01_00.00.00.000_1"
            empty.mkdir(parents=True)
            output = (
                "Saved log to /tmp/tradefed_global_log_1.txt\n"
                f"RESULT DIRECTORY : {empty}\n"
            )
            self.assertIsNone(runner.find_results_dir(output))

    def test_find_results_dir_prefers_new_dir_since_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "android-cts" / "results"
            runner = TradefedRunner(Path(tmp) / "android-cts", "cts-tradefed")
            self._make_result_dir(root, "2026.10.03_13.21.28.012_2796")
            (root / "latest").symlink_to(root / "2026.10.03_13.21.28.012_2796")
            before = runner.snapshot_result_dirs()
            new = self._make_result_dir(root, "2026.10.04_16.16.31.269_4285")
            # Interrupted runs print no RESULT DIRECTORY line
            self.assertEqual(runner.find_results_dir("", before=before), str(new))

    def test_parse_session_table_from_list_results(self):
        table = (
            "Session  Pass    Fail  Warning  Modules Complete  Result Directory              "
            "Test Plan  Device serial(s)  Build ID         Product\n"
            "0        3275    0     0        1 of 1            2026.10.03_13.21.28.012_2796  "
            "cts        0.0.0.0:6520      CP2A.260605.016  aosp_cf_x86_64_auto\n"
            "4        161222  2067  0        421 of 1251       2026.10.04_16.16.31.269_4285  "
            "cts        0.0.0.0:6522, 0.0.0.0:6533  CP2A.260605.016  aosp_cf_x86_64_auto\n"
        )
        parse = TradefedRunner.parse_session_table
        self.assertEqual(parse(table, "2026.10.04_16.16.31.269_4285"), 4)
        self.assertEqual(parse(table, "2026.10.03_13.21.28.012_2796"), 0)
        self.assertIsNone(parse(table, "2026.10.05_00.00.00.000_1"))
        # The ATS console id must never be read as a session index
        self.assertIsNone(parse("{olc_session_id=93400c03}", "93400c03"))

    def test_resolve_session_id_falls_back_to_dir_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "android-cts" / "results"
            runner = TradefedRunner(Path(tmp) / "android-cts", "cts-tradefed")
            self._make_result_dir(root, "2026.10.03_13.21.28.012_2796")
            target = self._make_result_dir(root, "2026.10.04_16.16.31.269_4285")
            (root / "latest").symlink_to(target)
            (root / "2026.10.04_16.16.31.269_4285.zip").write_text("", encoding="utf-8")
            # No tradefed script in the temp layout => 'list results' cannot run
            self.assertEqual(runner.resolve_session_id(target), 1)


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


class ExecutorPriorityAndDevicesTests(unittest.TestCase):
    def test_priority_ascending_and_serials_passed(self):
        from xts_agent.config_loader import (
            DeviceRequirements,
            PathsConfig,
            ShardingConfig,
            TestPlanConfig,
        )

        low = SuiteConfig(name="cts", priority=1, plan="cts", sharding=ShardingConfig(shard_count=1))
        high = SuiteConfig(name="ats", priority=5, plan="ats", sharding=ShardingConfig(shard_count=1))
        plan = TestPlanConfig(
            name="t",
            suites=[high, low],
            devices=DeviceRequirements(min_devices=1),
            paths=PathsConfig(xts_packages_dir="/opt/xts"),
        )

        device = MagicMock()
        device.serial = "serial-1"
        device.device_type = "aaos"

        dm = MagicMock()
        dm.get_available_devices.return_value = [device]
        dm.allocate_devices.return_value = [device]

        shard = ShardManager(dm)
        registry = MagicMock()
        registry.get_suite.return_value = None

        executor = TestPlanExecutor(plan, dm, shard, None, registry, results_dir="/tmp/xts-results")
        result = executor.execute_plan(plan, dry_run=True)
        self.assertEqual(list(result.suites_results.keys()), ["cts", "ats"])
        self.assertTrue(result.device_serials[0].startswith("dry-run-device-"))
        self.assertEqual(result.overall_status, "DRY_RUN")
        # dry-run does not allocate live devices
        dm.allocate_devices.assert_not_called()

    def test_release_on_failure(self):
        from xts_agent.config_loader import DeviceRequirements, PathsConfig, TestPlanConfig

        suite = SuiteConfig(name="cts", plan="cts")
        plan = TestPlanConfig(
            name="t",
            suites=[suite],
            devices=DeviceRequirements(min_devices=1),
            paths=PathsConfig(),
        )
        device = MagicMock(serial="s1", device_type="phone")
        dm = MagicMock()
        dm.get_available_devices.return_value = [device]
        dm.allocate_devices.return_value = [device]

        executor = TestPlanExecutor(
            plan, dm, ShardManager(dm), None, MagicMock(get_suite=MagicMock(return_value=None)),
            results_dir="/tmp/xts-results",
        )
        # Force missing tradefed script => failure path after allocate
        with patch.object(TestPlanExecutor, "_resolve_package_path", return_value=Path("/no/such/suite")):
            res = executor.execute_suite(suite, dry_run=False)
        self.assertEqual(res.status, "FAILED")
        dm.release_devices.assert_called()

    def test_live_allocate_passes_serials_into_command(self):
        from xts_agent.config_loader import DeviceRequirements, PathsConfig, TestPlanConfig
        from xts_agent.execution.tradefed_runner import ExecutionResult

        suite = SuiteConfig(name="cts", plan="cts")
        plan = TestPlanConfig(
            name="t",
            suites=[suite],
            devices=DeviceRequirements(min_devices=1),
            paths=PathsConfig(),
        )
        device = MagicMock(serial="serial-1", device_type="aaos")
        dm = MagicMock()
        dm.get_available_devices.return_value = [device]
        dm.allocate_devices.return_value = [device]

        executor = TestPlanExecutor(
            plan,
            dm,
            ShardManager(dm),
            None,
            MagicMock(get_suite=MagicMock(return_value=None)),
            results_dir="/tmp/xts-results",
        )

        with tempfile.TemporaryDirectory() as tmp:
            suite_dir = Path(tmp) / "android-cts" / "tools"
            suite_dir.mkdir(parents=True)
            (suite_dir / "cts-tradefed").write_text("#!/bin/sh\n", encoding="utf-8")
            results_dir = Path(tmp) / "android-cts" / "results" / "2026.10.06_01.02.22.903_1303"
            results_dir.mkdir(parents=True)
            (results_dir / "test_result.xml").write_text(PASSING_XML, encoding="utf-8")
            with patch.object(
                TestPlanExecutor, "_resolve_package_path", return_value=suite_dir.parent
            ), patch.object(
                TradefedRunner,
                "execute",
                return_value=ExecutionResult(True, 1, 0, 1.0, str(results_dir), "log"),
            ) as exec_mock:
                res = executor.execute_suite(suite, dry_run=False)

        self.assertEqual(res.status, "PASSED")
        cmd = exec_mock.call_args[0][0]
        self.assertIn("-s", cmd)
        self.assertIn("serial-1", cmd)
        dm.release_devices.assert_called_with(["serial-1"])


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
        from xts_agent.results.result_aggregator import ResultAggregator
        from xts_agent.results.result_comparator import ResultComparator

        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "test_result.xml"
            xml.write_text(MULTI_ABI_XML, encoding="utf-8")
            parsed = ResultParser().parse_xml(xml)
        failed = ResultParser().get_failed_tests(parsed)
        self.assertEqual([t.test_id for t in failed], ["armeabi-v7a CtsSample com.example.Foo#testA"])
        # Merging with itself must not let the arm64 PASS mask the armeabi FAIL
        merged = ResultAggregator().merge_results([parsed, parsed])
        self.assertEqual(merged.summary["fail"], 1)
        self.assertEqual(ResultComparator().compare(parsed, parsed).summary["persistent_failures"], 1)


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


class RetryManagerTests(unittest.TestCase):
    def _suite_result(self, status: str, session_id: int):
        return SuiteResult(
            name="cts", status=status, pass_count=0, fail_count=0, skip_count=0,
            duration=0.0, session_id=session_id, results_dir="", retry_count=0,
        )

    def _manager(self):
        from xts_agent.retry.retry_manager import RetryManager

        return RetryManager(MagicMock(post_execution=None))

    def test_incomplete_retries_not_executed_and_follows_latest_session(self):
        from xts_agent.execution.tradefed_runner import ExecutionResult

        suite = SuiteConfig(name="cts", plan="cts")
        suite.retry.max_retries = 2
        runner = TradefedRunner("/opt/xts/android-cts", "cts-tradefed")
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            first.mkdir()
            (first / "test_result.xml").write_text(INCOMPLETE_XML, encoding="utf-8")
            second = Path(tmp) / "second"
            second.mkdir()
            (second / "test_result.xml").write_text(PASSING_XML, encoding="utf-8")
            runs = [
                ExecutionResult(True, 5, 0, 1.0, str(first), "log1"),
                ExecutionResult(True, 6, 0, 1.0, str(second), "log2"),
            ]
            with patch("time.sleep"), patch.object(
                TradefedRunner, "execute", side_effect=runs
            ) as exec_mock:
                res = self._manager().retry_suite_until_done(
                    runner, self._suite_result("INCOMPLETE", 4), suite, ["s1"], tmp
                )
        first_cmd, second_cmd = (c[0][0] for c in exec_mock.call_args_list)
        self.assertEqual(first_cmd[first_cmd.index("--retry") + 1], "4")
        self.assertNotIn("--retry-type", first_cmd)
        self.assertEqual(second_cmd[second_cmd.index("--retry") + 1], "5")
        self.assertEqual(res.status, "PASSED")
        self.assertEqual(res.session_id, 6)

    def test_retry_without_results_stops_and_keeps_status(self):
        from xts_agent.execution.tradefed_runner import ExecutionResult

        suite = SuiteConfig(name="cts", plan="cts")
        suite.retry.max_retries = 3
        runner = TradefedRunner("/opt/xts/android-cts", "cts-tradefed")
        with patch("time.sleep"), patch.object(
            TradefedRunner, "execute", return_value=ExecutionResult(True, None, 0, 1.0, None, "log")
        ) as exec_mock:
            res = self._manager().retry_suite_until_done(
                runner, self._suite_result("FAILED", 4), suite, ["s1"], "/tmp"
            )
        self.assertEqual(exec_mock.call_count, 1)
        self.assertEqual(res.status, "FAILED")


class LoadLatestPlanResultTests(unittest.TestCase):
    def test_picks_newest_report_for_this_plan_from_results_dir(self):
        import json
        import os

        from xts_agent.orchestrator import Orchestrator

        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "custom_results" / "reports"
            reports.mkdir(parents=True)

            def write(name, plan, status, mtime):
                f = reports / name
                f.write_text(json.dumps({"plan_name": plan, "status": status, "suites": {}}))
                os.utime(f, (mtime, mtime))

            # "Z..." sorts last by name; it belongs to another plan
            write("xts_report_Zebra_dev_20261007_000000.json", "Zebra", "FAILED", 300)
            write("xts_report_Mine_dev_20261006_000000.json", "Mine", "FAILED", 100)
            write("xts_report_Mine_dev_20261007_000000.json", "Mine", "INCOMPLETE", 200)
            write("xts_report_Mine_dry_20261008_000000.json", "Mine", "DRY_RUN", 400)

            orch = Orchestrator.__new__(Orchestrator)
            orch._results_dir = Path(tmp) / "custom_results"
            orch.plan = MagicMock()
            orch.plan.name = "Mine"
            loaded = orch._load_latest_plan_result()
        self.assertEqual(loaded.plan_name, "Mine")
        self.assertEqual(loaded.overall_status, "INCOMPLETE")


class Ats2UploadTests(unittest.TestCase):
    def test_generate_reports_does_not_upload(self):
        from xts_agent.orchestrator import Orchestrator

        with tempfile.TemporaryDirectory() as tmp:
            orch = Orchestrator.__new__(Orchestrator)
            orch._results_dir = Path(tmp)
            orch.plan = ConfigLoader("config/test_plans/smoke_test.yaml").load_plan()
            orch.last_rca_report = None
            result = PlanResult("demo", {}, 0, 0, 0, 0.0, "FAILED")
            with patch.object(Orchestrator, "_upload_ats2") as upload:
                orch.generate_reports(result, formats=["json"])
            upload.assert_not_called()


class CliAnalyzeTests(unittest.TestCase):
    def _invoke(self, *args):
        from click.testing import CliRunner

        from xts_agent import cli

        report = MagicMock(failures=[], summary={})
        with patch.object(cli.Orchestrator, "analyze", return_value=report) as analyze:
            result = CliRunner().invoke(
                cli.main, ["analyze", "--plan", "config/test_plans/smoke_test.yaml", *args]
            )
        return result, analyze

    def test_classification_flag_is_honored(self):
        result, analyze = self._invoke("--rca", "--no-classify-failures")
        self.assertEqual(result.exit_code, 0, result.output)
        analyze.assert_called_once_with(enable_rca=True, classify_failures=False)

    def test_defaults_keep_ci_behaviour(self):
        result, analyze = self._invoke("--rca", "--classify-failures")
        analyze.assert_called_once_with(enable_rca=True, classify_failures=True)

    def test_no_rca_skips_analysis(self):
        result, analyze = self._invoke("--no-rca")
        self.assertEqual(result.exit_code, 0)
        analyze.assert_not_called()


class ReportGeneratorTests(unittest.TestCase):
    def test_multi_format_reports(self):
        from xts_agent.reporting.report_generator import ReportGenerator

        plan_result = PlanResult(
            plan_name="demo",
            suites_results={
                "cts": SuiteResult(
                    name="cts",
                    status="FAILED",
                    pass_count=1,
                    fail_count=1,
                    skip_count=0,
                    duration=1.5,
                    session_id=9,
                    results_dir="",
                    retry_count=0,
                    device_serials=["emu-1"],
                    error_message="boom",
                )
            },
            total_pass=1,
            total_fail=1,
            total_skip=0,
            duration=1.5,
            overall_status="FAILED",
            device_serials=["emu-1"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            written = ReportGenerator().generate_all(
                plan_result, None, None, tmp, formats=["html", "json", "junit"], basename="demo"
            )
            self.assertTrue(Path(written["html"]).exists())
            self.assertTrue(Path(written["json"]).exists())
            self.assertTrue(Path(written["junit"]).exists())
            xml_text = Path(written["junit"]).read_text(encoding="utf-8")
            self.assertIn("testcase", xml_text)


if __name__ == "__main__":
    unittest.main()
