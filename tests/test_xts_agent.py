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
from xts_agent.results.result_parser import ResultParser


SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='no' ?>
<Result start_display="now" end_display="later" suite_name="CTS">
  <Build device_serial="emulator-5554" />
  <Summary pass="2" failed="1" skipped="1" modules_done="3" modules_total="3" />
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
    </TestCase>
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

    def test_find_results_dir_from_log(self):
        runner = TradefedRunner("/tmp/android-cts", "cts-tradefed")
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "2026.01.01_120000"
            results.mkdir()
            (results / "test_result.xml").write_text("<Result/>", encoding="utf-8")
            output = f"Saved log to {results}\nSession 42 completed\n"
            found = runner.find_results_dir(output)
            self.assertEqual(found, str(results))
            self.assertEqual(runner.get_session_id(output), 42)


class ResultParserTests(unittest.TestCase):
    def test_summary_uses_skipped_not_modules_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "test_result.xml"
            xml.write_text(SAMPLE_XML, encoding="utf-8")
            parsed = ResultParser().parse_xml(xml)
            self.assertEqual(parsed.summary["pass"], 2)
            self.assertEqual(parsed.summary["fail"], 1)
            self.assertEqual(parsed.summary["skip"], 1)
            failed = ResultParser().get_failed_tests(parsed)
            self.assertEqual(len(failed), 1)
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
        self.assertEqual(result.overall_status, "PASSED")
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
            with patch.object(
                TestPlanExecutor, "_resolve_package_path", return_value=suite_dir.parent
            ), patch.object(
                TradefedRunner,
                "execute",
                return_value=ExecutionResult(True, 1, 0, 1.0, None, "log"),
            ) as exec_mock:
                res = executor.execute_suite(suite, dry_run=False)

        self.assertEqual(res.status, "PASSED")
        cmd = exec_mock.call_args[0][0]
        self.assertIn("-s", cmd)
        self.assertIn("serial-1", cmd)
        dm.release_devices.assert_called_with(["serial-1"])


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
