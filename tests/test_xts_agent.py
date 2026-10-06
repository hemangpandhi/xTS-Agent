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


class ProfileTests(unittest.TestCase):
    def _load(self, plan_yaml: str):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "plan.yaml"
            p.write_text(plan_yaml, encoding="utf-8")
            return ConfigLoader(p).load_plan()

    def test_certification_rejects_filters(self):
        from xts_agent.config_loader import ConfigError

        for key, value in (
            ("exclude_filters", "[CtsCarTestCases]"),
            ("include_filters", "[CtsBionicTestCases]"),
            ("modules", "[CtsOsTestCases]"),
        ):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                self._load(f"name: c\nprofile: certification\nsuites:\n- name: cts\n  {key}: {value}\n")

    def test_development_allows_filters_and_is_default(self):
        plan = self._load("name: d\nsuites:\n- name: cts\n  exclude_filters: [CtsCarTestCases]\n")
        self.assertEqual(plan.profile, "development")
        self.assertEqual(plan.suites[0].exclude_filters, ["CtsCarTestCases"])

    def test_unknown_profile_rejected(self):
        from xts_agent.config_loader import ConfigError

        with self.assertRaises(ConfigError):
            self._load("name: x\nprofile: certified\nsuites: []\n")

    def test_shipped_plans_load_with_expected_profiles(self):
        expected = {
            "full_certification.yaml": "certification",
            "full_cts_hardware.yaml": "certification",
            "cts_only.yaml": "certification",
            "vts_only.yaml": "certification",
            "catbox_functional.yaml": "certification",
            "dev_cts_hardware_triage.yaml": "development",
            "smoke_test.yaml": "development",
            "full_cts.yaml": "development",
        }
        for name, profile in expected.items():
            with self.subTest(plan=name):
                plan = ConfigLoader(f"config/test_plans/{name}").load_plan()
                self.assertEqual(plan.profile, profile)
        dev = ConfigLoader("config/test_plans/dev_cts_hardware_triage.yaml").load_plan()
        self.assertIn("CtsCarTestCases", dev.suites[0].exclude_filters)


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


class LlmProviderTests(unittest.TestCase):
    def _cfg(self, **kw):
        from xts_agent.config_loader import AiRcaConfig

        return AiRcaConfig(enabled=True, **kw)

    def test_defaults_are_on_prem(self):
        from xts_agent.rca.llm_provider import LlamaCppProvider, get_llm_provider

        cfg = self._cfg()
        self.assertEqual(cfg.provider, "llama_cpp")
        self.assertFalse(cfg.allow_external_providers)
        self.assertIsInstance(get_llm_provider(cfg), LlamaCppProvider)

    def test_external_provider_refused_without_opt_in(self):
        from xts_agent.rca.llm_provider import ExternalProviderNotAllowed, get_llm_provider

        with self.assertRaises(ExternalProviderNotAllowed):
            get_llm_provider(self._cfg(provider="gemini", gemini_api_key="k"))

    def test_analyzer_falls_back_when_external_refused(self):
        from xts_agent.rca.ai_analyzer import AIAnalyzer

        analyzer = AIAnalyzer(self._cfg(provider="gemini", gemini_api_key="k"))
        self.assertIsNone(analyzer.triage_engine)
        self.assertEqual(analyzer.analyze_failure("t", "stack", "").confidence, 0.4)

    def test_gemini_key_in_header_with_timeout_and_not_logged(self):
        import requests

        from xts_agent.rca.llm_provider import get_llm_provider

        provider = get_llm_provider(
            self._cfg(provider="gemini", gemini_api_key="SECRET-KEY", allow_external_providers=True,
                      request_timeout_secs=42)
        )
        self.assertNotIn("SECRET-KEY", provider.url)
        err = requests.ConnectionError(f"failed for {provider.url}?key=SECRET-KEY")
        with patch("requests.post", side_effect=err) as post, self.assertLogs(
            "xts_agent.rca.llm_provider", "ERROR"
        ) as logs:
            out = provider.generate("prompt")
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "SECRET-KEY")
        self.assertEqual(post.call_args.kwargs["timeout"], 42)
        self.assertNotIn("SECRET-KEY", " ".join(logs.output) + out)


class SecretsTests(unittest.TestCase):
    def _load(self, defaults: str, plan: str = "name: t\nsuites: []\n", env=None):
        import os

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env or {}, clear=False):
            d = Path(tmp) / "defaults.yaml"
            d.write_text(defaults, encoding="utf-8")
            p = Path(tmp) / "plan.yaml"
            p.write_text(plan, encoding="utf-8")
            return ConfigLoader(p, defaults_path=d).load_plan()

    def test_env_reference_expanded(self):
        plan = self._load(
            'ats2:\n  api_key: "${MY_ATS_KEY}"\n  base_url: "${ATS_URL:-http://ats.local}"\n',
            env={"MY_ATS_KEY": "from-env"},
        )
        self.assertEqual(plan.ats2.api_key, "from-env")
        self.assertEqual(plan.ats2.base_url, "http://ats.local")

    def test_fixed_env_overrides_win(self):
        plan = self._load(
            'ats2:\n  api_key: "${UNSET_ON_PURPOSE:-}"\nreporting:\n  notifications:\n    slack_webhook: ""\n',
            plan="name: t\nsuites: []\npost_execution:\n  reporting:\n    notifications:\n      slack_webhook: ''\n",
            env={"XTS_ATS2_API_KEY": "a", "XTS_SLACK_WEBHOOK": "https://hooks/x", "XTS_GEMINI_API_KEY": "g"},
        )
        self.assertEqual(plan.ats2.api_key, "a")
        self.assertEqual(plan.ai_rca.gemini_api_key, "g")
        self.assertEqual(plan.post_execution.reporting.notifications["slack_webhook"], "https://hooks/x")
        # raw defaults are not mutated by the override
        self.assertEqual(plan.raw_defaults["reporting"]["notifications"]["slack_webhook"], "")

    def test_plaintext_secret_warns(self):
        with self.assertLogs("xts_agent.config_loader", "WARNING") as logs:
            self._load("ai_rca:\n  gemini_api_key: AIzaPlainText\n")
        self.assertTrue(any("Plaintext secret at ai_rca.gemini_api_key" in m for m in logs.output))
        self.assertFalse(any("AIzaPlainText" in m for m in logs.output))


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

    def test_retry_command_shards_across_all_serials(self):
        runner = TradefedRunner("/tmp/android-cts", "cts-tradefed")
        cmd = runner.build_retry_command(7, "FAILED", device_serials=["a", "b", "c"])
        self.assertEqual(cmd[cmd.index("--shard-count") + 1], "3")
        self.assertEqual(cmd[cmd.index("--retry") + 1], "7")
        single = runner.build_retry_command(7, "FAILED", device_serials=["a"])
        self.assertNotIn("--shard-count", single)

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
        dm.select_shard_pool.return_value = [device]

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
        dm.select_shard_pool.return_value = [device]

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
        dm.select_shard_pool.return_value = [device]

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


class BaselineTests(unittest.TestCase):
    def test_json_round_trip_and_xml_and_no_pickle(self):
        from xts_agent.results.result_comparator import ResultComparator

        comp = ResultComparator()
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "test_result.xml"
            xml.write_text(MULTI_ABI_XML, encoding="utf-8")
            from_xml = comp.load_baseline(xml)
            snap = Path(tmp) / "baseline.json"
            comp.save_baseline(from_xml, snap)
            loaded = comp.load_baseline(snap)
            self.assertEqual(loaded, from_xml)
            self.assertEqual(comp.compare(loaded, from_xml).summary["new_failures"], 0)
            for bad in ("baseline.pkl", "baseline.pickle"):
                with self.assertRaises(ValueError):
                    comp.load_baseline(Path(tmp) / bad)


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


class IsolationTests(unittest.TestCase):
    def _handler(self, virtual: bool, command: str = "cvd powerwash --serial {serial}"):
        from xts_agent.retry.isolation import IsolationHandler

        dm = MagicMock()
        dm.is_virtual_device.return_value = virtual
        dm.reboot_device.return_value = True
        dm.wait_for_device.return_value = True
        return IsolationHandler(dm, virtual_reset_command=command), dm

    def test_physical_device_is_never_wiped(self):
        handler, dm = self._handler(virtual=False)
        with patch("subprocess.run") as run, patch(
            "xts_agent.retry.isolation.AdbWrapper"
        ) as adb:
            handler.apply_isolation("HU123", "FULLY_ISOLATED")
        run.assert_not_called()
        dm.reboot_device.assert_called_once_with("HU123")
        shell_cmds = " ".join(c[0][1] for c in adb.shell.call_args_list)
        self.assertNotIn("wipe", shell_cmds)
        self.assertNotIn("MASTER_CLEAR", shell_cmds)

    def test_virtual_device_uses_host_reset(self):
        handler, dm = self._handler(virtual=True)
        with patch("subprocess.run") as run, patch("xts_agent.retry.isolation.AdbWrapper"):
            handler.apply_isolation("0.0.0.0:6520", "FULLY_ISOLATED")
        self.assertEqual(run.call_args[0][0], ["cvd", "powerwash", "--serial", "0.0.0.0:6520"])
        dm.reboot_device.assert_not_called()

    def test_virtual_without_command_just_reboots(self):
        handler, dm = self._handler(virtual=True, command="")
        with patch("subprocess.run") as run, patch("xts_agent.retry.isolation.AdbWrapper"):
            handler.apply_isolation("0.0.0.0:6520", "FULLY_ISOLATED")
        run.assert_not_called()
        dm.reboot_device.assert_called_once()

    def test_is_virtual_device_reads_props(self):
        from xts_agent.device.device_manager import DeviceManager

        dm = DeviceManager()
        for props, expected in (
            ({"ro.hardware": "cutf_cvm"}, True),
            ({"ro.kernel.qemu": "1"}, True),
            ({"ro.hardware": "qcom"}, False),
            ({}, False),
        ):
            with self.subTest(props=props), patch.object(
                DeviceManager, "get_device_properties", return_value=props
            ):
                self.assertEqual(dm.is_virtual_device("x"), expected)


class Aapt2Tests(unittest.TestCase):
    def test_check_never_modifies_launcher(self):
        from xts_agent.utils.env_validator import EnvironmentValidator

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "cts-tradefed"
            body = "--aapt='/home/someone/Android/Sdk/build-tools/34.0.0/aapt2' \\\n"
            script.write_text(body, encoding="utf-8")
            self.assertFalse(EnvironmentValidator.check_tradefed_script(script))
            self.assertEqual(script.read_text(encoding="utf-8"), body)
            self.assertEqual(list(Path(tmp).iterdir()), [script])

            stock = "--aapt='$(type -P aapt2 2>/dev/null)' \\\n"
            script.write_text(stock, encoding="utf-8")
            self.assertTrue(EnvironmentValidator.check_tradefed_script(script))

    def test_ensure_aapt2_prepends_build_tools_to_path(self):
        import os

        from xts_agent.utils.env_validator import EnvironmentValidator

        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "build-tools" / "34.0.0"
            tools.mkdir(parents=True)
            (tools / "aapt2").write_text("", encoding="utf-8")
            env = {"PATH": "/usr/bin", "ANDROID_HOME": tmp}
            # SDK build-tools must win over a distro /usr/bin/aapt2 on PATH
            with patch.dict(os.environ, env, clear=True), patch(
                "shutil.which", return_value="/usr/bin/aapt2"
            ):
                found = EnvironmentValidator.ensure_aapt2_on_path()
                self.assertEqual(found, tools / "aapt2")
                self.assertTrue(os.environ["PATH"].startswith(str(tools)))


class ScopedCleanupTests(unittest.TestCase):
    def test_kills_only_recorded_tradefed_groups(self):
        import subprocess

        from xts_agent.execution.tradefed_runner import kill_recorded_tradefed

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake-tradefed"
            script.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
            script.chmod(0o755)
            ours = subprocess.Popen([str(script)], start_new_session=True)
            other_job = subprocess.Popen([str(script)], start_new_session=True)
            try:
                logs = Path(tmp) / "logs"
                logs.mkdir()
                (logs / f"tradefed_{ours.pid}.pid").write_text(str(ours.pid))
                (logs / "tradefed_999999.pid").write_text("999999")  # stale

                killed = kill_recorded_tradefed(logs, grace_secs=5)

                self.assertEqual(killed, [ours.pid])
                self.assertIsNotNone(ours.wait(timeout=10))
                self.assertIsNone(other_job.poll())
                self.assertEqual(list(logs.glob("*.pid")), [])
            finally:
                for proc in (ours, other_job):
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()

    def test_execute_removes_pidfile_after_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "android-cts" / "tools"
            tools.mkdir(parents=True)
            script = tools / "cts-tradefed"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            runner = TradefedRunner(tools.parent, "cts-tradefed")
            logs = Path(tmp) / "logs"
            res = runner.execute([str(script)], timeout_hours=0.01, log_dir=logs)
            self.assertEqual(res.return_code, 0)
            self.assertEqual(list(logs.glob("*.pid")), [])


class ShardPoolTests(unittest.TestCase):
    def _dev(self, serial, fp, dtype="aaos"):
        from xts_agent.device.device_manager import DeviceInfo

        return DeviceInfo(serial, "m", "p", fp, 34, 100, "device", dtype)

    def test_shards_only_across_one_build(self):
        from xts_agent.device.device_manager import DeviceManager

        a = "google/car/cf:16/CP2A/1:userdebug/dev-keys"
        b = "google/car/cf:16/CP2A/2:userdebug/dev-keys"
        devices = [self._dev("s1", a), self._dev("s2", b), self._dev("s3", a), self._dev("s4", "")]
        pool = DeviceManager().select_shard_pool(devices)
        self.assertEqual([d.serial for d in pool], ["s1", "s3"])

    def test_device_type_filtered_before_counting(self):
        from xts_agent.device.device_manager import DeviceManager

        fp = "x:user/release-keys"
        devices = [self._dev(f"p{i}", fp, "phone") for i in range(6)] + [
            self._dev(f"a{i}", fp) for i in range(4)
        ]
        dm = DeviceManager()
        pool = dm.select_shard_pool(devices, "aaos")
        self.assertEqual(len(pool), 4)
        allocated = dm.allocate_devices(len(pool), "aaos", candidates=pool)
        self.assertEqual({d.serial for d in allocated}, {"a0", "a1", "a2", "a3"})

    def test_required_props_filter(self):
        from xts_agent.device.device_manager import DeviceManager

        fp = "x:user/release-keys"
        devices = [self._dev("s1", fp), self._dev("s2", fp)]
        props = {"s1": {"ro.product.model": "HU-A"}, "s2": {"ro.product.model": "HU-B"}}
        with patch.object(DeviceManager, "get_device_properties", side_effect=lambda s: props[s]):
            pool = DeviceManager().select_shard_pool(devices, required_props={"ro.product.model": "HU-B"})
        self.assertEqual([d.serial for d in pool], ["s2"])

    def test_user_build_warning(self):
        from xts_agent.execution.test_plan_executor import _warn_if_not_user_build

        with patch("xts_agent.execution.test_plan_executor.logger") as log:
            _warn_if_not_user_build("google/cf/cf:16/CP2A/1:userdebug/dev-keys")
            self.assertEqual(log.warning.call_count, 1)
            _warn_if_not_user_build("oem/hu/hu:16/AB1/42:user/release-keys")
            self.assertEqual(log.warning.call_count, 1)


class DiscoveryTests(unittest.TestCase):
    DUMPSYS = (
        "Active default network: 100\n"
        "  NetworkAgentInfo{network{100}  ni{WIFI CONNECTED}  nc{[ Transports: WIFI "
        "Capabilities: INTERNET&NOT_RESTRICTED&TRUSTED&VALIDATED&NOT_VPN ]}}\n"
        "  NetworkAgentInfo{network{101}  ni{MOBILE} nc{[ Capabilities: INTERNET ]}}\n"
    )

    def test_validated_network_reads_active_network_only(self):
        from xts_agent.device.device_manager import DeviceManager

        dm = DeviceManager()
        with patch("xts_agent.device.device_manager.AdbWrapper.shell", return_value=self.DUMPSYS):
            self.assertTrue(dm.has_validated_network("s"))
        unvalidated = self.DUMPSYS.replace("Active default network: 100", "Active default network: 101")
        with patch("xts_agent.device.device_manager.AdbWrapper.shell", return_value=unvalidated):
            self.assertFalse(dm.has_validated_network("s"))
        # The real Cuttlefish case: INTERNET without VALIDATED
        no_val = self.DUMPSYS.replace("&VALIDATED", "")
        with patch("xts_agent.device.device_manager.AdbWrapper.shell", return_value=no_val):
            self.assertFalse(dm.has_validated_network("s"))

    def test_discovery_probes_in_parallel_and_caches_type(self):
        import threading
        import time as _time

        from xts_agent.device.device_manager import DeviceManager

        serials = [f"s{i}" for i in range(8)]
        listing = "List of devices attached\n" + "".join(f"{s} device\n" for s in serials) + "x offline\n"
        active = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def slow_props(serial):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            _time.sleep(0.05)
            with lock:
                active["now"] -= 1
            return {"ro.build.fingerprint": "fp:user/k"}

        dm = DeviceManager()
        with patch("xts_agent.device.device_manager.AdbWrapper._run_cmd", return_value=listing), \
                patch.object(DeviceManager, "get_device_properties", side_effect=slow_props), \
                patch.object(DeviceManager, "is_aaos_device", return_value=True) as aaos, \
                patch.object(DeviceManager, "_read_battery", return_value=(80, True)):
            first = dm.discover_devices()
            second = dm.discover_devices()
        self.assertEqual([d.serial for d in first], serials + ["x"])
        self.assertEqual(first[-1].state, "offline")
        self.assertGreater(active["peak"], 1)
        self.assertEqual(aaos.call_count, len(serials))  # cached on 2nd pass
        self.assertEqual([d.device_type for d in second[:-1]], ["aaos"] * len(serials))

    def test_reboot_and_wait_all_reports_failures(self):
        from xts_agent.device.device_manager import DeviceManager

        dm = DeviceManager()
        with patch.object(DeviceManager, "reboot_device", return_value=True), \
                patch.object(DeviceManager, "wait_for_device", side_effect=lambda s, t: s != "bad"):
            self.assertEqual(dm.reboot_and_wait_all(["a", "bad"]), {"a": True, "bad": False})


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
