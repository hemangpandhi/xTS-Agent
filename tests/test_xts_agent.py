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


_LEASE_TMP = None
_PREP_PATCH = None
_SURVIVAL_PATCH = None


def setUpModule():
    # Keep device lease files out of the real host-wide lease dir
    import os

    global _LEASE_TMP
    _LEASE_TMP = tempfile.TemporaryDirectory()
    # Subdir, so the ledger (stored next to lease_dir) also stays in the temp dir
    os.environ["XTS_LEASE_DIR"] = str(Path(_LEASE_TMP.name) / "leases")
    # Executor tests use fake serials: never send prep commands to real adb or
    # probe real adb for post-suite device survival
    global _PREP_PATCH, _SURVIVAL_PATCH
    _PREP_PATCH = patch("xts_agent.execution.test_plan_executor.DevicePreparer")
    _PREP_PATCH.start()
    _SURVIVAL_PATCH = patch.object(TestPlanExecutor, "_record_device_survival")
    _SURVIVAL_PATCH.start()


def tearDownModule():
    import os

    os.environ.pop("XTS_LEASE_DIR", None)
    _LEASE_TMP.cleanup()
    _PREP_PATCH.stop()
    _SURVIVAL_PATCH.stop()


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

    def test_failed_but_incomplete_still_retries_not_executed(self):
        from xts_agent.execution.tradefed_runner import ExecutionResult

        # Shape of real session 7: 1303 failures and 524 of 1251 modules done
        failed_incomplete = INCOMPLETE_XML.replace('failed="0"', 'failed="1303"')
        suite = SuiteConfig(name="cts", plan="cts")
        suite.retry.max_retries = 1
        runner = TradefedRunner("/opt/xts/android-cts", "cts-tradefed")
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "test_result.xml"
            xml.write_text(failed_incomplete, encoding="utf-8")
            current = self._suite_result("FAILED", 7)
            current.details = ResultParser().parse_xml(xml)
            with patch("time.sleep"), patch.object(
                TradefedRunner, "execute", return_value=ExecutionResult(True, None, 0, 1.0, None, "log")
            ) as exec_mock:
                self._manager().retry_suite_until_done(runner, current, suite, ["s1"], tmp)
        self.assertNotIn("--retry-type", exec_mock.call_args[0][0])

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
                patch.object(DeviceManager, "_read_battery", return_value=(80, True, True)):
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


class ResumeTests(unittest.TestCase):
    FP = "oem/hu/hu:16/AB1/42:user/release-keys"

    def _setup(self, tmp: Path):
        from xts_agent.config_loader import DeviceRequirements, PathsConfig, TestPlanConfig
        from xts_agent.execution.run_state import RunState

        for suite in ("cts", "vts"):
            tools = tmp / f"android-{suite}" / "tools"
            tools.mkdir(parents=True)
            (tools / f"{suite}-tradefed").write_text("#!/bin/sh\n", encoding="utf-8")
        suites = [
            SuiteConfig(name="cts", plan="cts", priority=1, package_path=str(tmp / "android-cts")),
            SuiteConfig(name="vts", plan="vts", priority=2, package_path=str(tmp / "android-vts")),
        ]
        plan = TestPlanConfig(
            name="cert", suites=suites, devices=DeviceRequirements(min_devices=1), paths=PathsConfig()
        )
        device = MagicMock(serial="hu-1", device_type="aaos", build_fingerprint=self.FP)
        dm = MagicMock()
        dm.get_available_devices.return_value = [device]
        dm.select_shard_pool.return_value = [device]
        dm.allocate_devices.return_value = [device]
        state = RunState.for_plan(tmp / "results", "cert")
        executor = TestPlanExecutor(
            plan, dm, ShardManager(dm), None, MagicMock(get_suite=MagicMock(return_value=None)),
            results_dir=tmp / "results", run_state=state,
        )
        return plan, executor, dm, state

    @staticmethod
    def _result_dir(tmp: Path, suite: str, name: str, xml: str) -> Path:
        d = tmp / f"android-{suite}" / "results" / name
        d.mkdir(parents=True)
        (d / "test_result.xml").write_text(xml, encoding="utf-8")
        return d

    def test_resume_skips_passed_and_continues_interrupted_suite(self):
        from xts_agent.execution.tradefed_runner import ExecutionResult

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            plan, executor, dm, state = self._setup(tmp)
            cts_dir = self._result_dir(tmp, "cts", "2026.10.07_01.00.00.000_1", PASSING_XML)

            # --- first run: CTS passes, then the host dies inside VTS ---
            state.start("cert", "certification")
            state.suite_updated(SuiteResult("cts", "PASSED", 1, 0, 0, 1.0, 3, str(cts_dir), 0))
            vts_runner = TradefedRunner(tmp / "android-vts", "vts-tradefed")
            state.suite_started("vts", vts_runner.snapshot_result_dirs(), self.FP, ["hu-1"])
            # TradeFed had flushed a partial result before the crash
            partial = self._result_dir(tmp, "vts", "2026.10.07_02.00.00.000_2", INCOMPLETE_XML)

            # --- resume ---
            calls = []

            def fake_execute(self_runner, cmd, timeout_hours, log_dir, env=None):
                calls.append(cmd)
                # The retry session writes its own (cumulative) result dir
                done = self._result_dir(tmp, "vts", "2026.10.07_03.00.00.000_3", PASSING_XML)
                return ExecutionResult(True, 1, 0, 1.0, str(done), "log")

            with patch.object(TradefedRunner, "execute", fake_execute), patch.object(
                TradefedRunner, "resolve_session_id", return_value=0
            ):
                result = executor.execute_plan(plan, resume=True)

            self.assertEqual(len(calls), 1, calls)  # CTS skipped, VTS continued once
            cmd = calls[0]
            self.assertEqual(cmd[1:4], ["run", "retry", "--retry"])
            self.assertEqual(cmd[cmd.index("--retry") + 1], "0")
            self.assertNotIn("--retry-type", cmd)  # incomplete => FAILED + NOT_EXECUTED
            required = dm.select_shard_pool.call_args_list[-1][0][2]
            self.assertEqual(required["ro.build.fingerprint"], self.FP)
            self.assertEqual(result.suites_results["cts"].status, "PASSED")
            self.assertEqual(result.suites_results["vts"].status, "PASSED")
            self.assertEqual(result.overall_status, "PASSED")
            self.assertTrue(state.data["complete"])
            self.assertTrue(partial.exists())

    def test_resume_after_complete_run_starts_fresh(self):
        from xts_agent.execution.tradefed_runner import ExecutionResult

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            plan, executor, dm, state = self._setup(tmp)
            state.start("cert", "certification")
            state.mark_complete("PASSED")
            with patch.object(
                TradefedRunner, "execute", return_value=ExecutionResult(True, None, 0, 1.0, None, "log")
            ) as exe:
                executor.execute_plan(plan, resume=True)
            self.assertEqual(exe.call_count, 2)
            self.assertTrue(all(c[0][0][1:3] == ["run", "commandAndExit"] for c in exe.call_args_list))


class ParallelSuiteTests(unittest.TestCase):
    def test_split_is_proportional_with_min_one_and_caps(self):
        split = ShardManager.split_devices(list(range(11)), {"CTS": 40, "VTS": 20, "STS": 8})
        self.assertEqual({k: len(v) for k, v in split.items()}, {"CTS": 7, "VTS": 3, "STS": 1})
        capped = ShardManager.split_devices(list(range(11)), {"CTS": 40, "CATBOX": 12}, {"CATBOX": 2})
        self.assertEqual(len(capped["CATBOX"]), 2)
        self.assertEqual(len(capped["CTS"]), 9)
        flat = [d for devs in split.values() for d in devs]
        self.assertEqual(sorted(flat), list(range(11)))  # each device used once
        with self.assertRaises(ValueError):
            ShardManager.split_devices([1], {"CTS": 1, "VTS": 1})

    def test_history_overrides_static_weight(self):
        mgr = ShardManager(MagicMock())
        self.assertEqual(mgr.suite_weight("CTS", lambda n: 123.0), 123.0)
        self.assertEqual(mgr.suite_weight("CTS", lambda n: None), 40.0)

    def test_estimate_device_hours_from_store(self):
        from xts_agent.results.result_store import ResultStore

        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp) / "db.sqlite")
            for hours, status in ((10, "FAILED"), (12, "PASSED"), (100, "INCOMPLETE"), (1, "DRY_RUN")):
                store.save_suite_run("p", "development", SuiteResult(
                    "CTS", status, 1, 1, 0, hours * 3600, 1, "", 0, device_serials=["d"] * 4))
            self.assertEqual(store.estimate_device_hours("CTS"), 48.0)  # median of 40/48/400
            self.assertIsNone(store.estimate_device_hours("VTS"))
            runs = store.recent_runs("cts")
            self.assertEqual((len(runs), runs[0]["status"], runs[0]["devices"]), (4, "DRY_RUN", ["d"] * 4))

    def test_scheduler_runs_concurrently_without_sharing_devices(self):
        import threading
        import time as _time

        from xts_agent.config_loader import DeviceRequirements, PathsConfig, TestPlanConfig
        from xts_agent.execution.tradefed_runner import ExecutionResult

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            suites = []
            for prio, name in enumerate(("cts", "vts", "sts"), start=1):
                tools = tmp / f"android-{name}" / "tools"
                tools.mkdir(parents=True)
                (tools / f"{name}-tradefed").write_text("#!/bin/sh\n", encoding="utf-8")
                from xts_agent.config_loader import ShardingConfig

                suites.append(SuiteConfig(name=name, plan=name, priority=prio,
                                          package_path=str(tmp / f"android-{name}"),
                                          sharding=ShardingConfig(shard_count="auto")))
            plan = TestPlanConfig(name="p", suites=suites, devices=DeviceRequirements(min_devices=1),
                                  paths=PathsConfig(), max_concurrent_suites=2)
            from xts_agent.device.device_manager import DeviceInfo, DeviceManager

            pool = [DeviceInfo(f"d{i}", "", "", "fp:user/k", 34, 100, "device", "aaos") for i in range(6)]
            dm = DeviceManager()
            dm.get_available_devices = MagicMock(return_value=pool)
            dm.select_shard_pool = MagicMock(return_value=pool)

            lock = threading.Lock()
            in_use, spans, overlap = set(), [], []
            running, peak = set(), []

            def fake_execute(runner_self, cmd, timeout_hours, log_dir, env=None):
                serials = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-s"]
                with lock:
                    overlap.extend(set(serials) & in_use)
                    in_use.update(serials)
                    running.add(cmd[3])
                    peak.append(len(running))
                start = _time.time()
                _time.sleep(0.2 if cmd[3] == "cts" else 0.05)
                with lock:
                    in_use.difference_update(serials)
                    running.discard(cmd[3])
                spans.append((cmd[3], start, _time.time(), serials))
                return ExecutionResult(True, None, 0, 0.1, None, "log")

            executor = TestPlanExecutor(plan, dm, ShardManager(dm), None,
                                        MagicMock(get_suite=MagicMock(return_value=None)),
                                        results_dir=tmp / "results")
            with patch.object(TradefedRunner, "execute", fake_execute):
                result = executor.execute_plan(plan)

        self.assertEqual(overlap, [])
        self.assertLessEqual(max(peak), 2)  # never above max_concurrent_suites
        by = {name: (start, end, serials) for name, start, end, serials in spans}
        self.assertLess(by["vts"][0], by["cts"][1])  # cts and vts overlapped
        self.assertGreaterEqual(by["sts"][0], by["vts"][1])  # sts waited for freed devices
        self.assertGreater(len(by["cts"][2]), len(by["vts"][2]))  # cts weighted heavier
        self.assertEqual(list(result.suites_results), ["cts", "vts", "sts"])
        self.assertEqual(dm._allocated, set())

    def test_concurrency_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "plan.yaml"
            p.write_text("name: t\nmax_concurrent_suites: 3\nsuites: []\n", encoding="utf-8")
            self.assertEqual(ConfigLoader(p).load_plan().max_concurrent_suites, 3)
        self.assertEqual(ConfigLoader("config/test_plans/cts_only.yaml").load_plan().max_concurrent_suites, 1)


class BatteryTests(unittest.TestCase):
    def _info(self, serial, level, present):
        from xts_agent.device.device_manager import DeviceInfo

        return DeviceInfo(serial, "", "", "fp", 34, level, "device", "aaos", battery_present=present)

    def test_reads_present_flag(self):
        from xts_agent.device.device_manager import DeviceManager

        dump = "  AC powered: true\n  present: false\n  level: 0\n"
        with patch("xts_agent.device.device_manager.AdbWrapper.shell", return_value=dump):
            self.assertEqual(DeviceManager()._read_battery("hu"), (0, True, False))

    def test_head_unit_without_battery_is_available_regardless_of_serial(self):
        from xts_agent.device.device_manager import DeviceManager

        devices = [
            self._info("192.168.1.10:5555", 0, present=False),  # head unit on TCP
            self._info("PHONE123", 5, present=True),  # drained phone
            self._info("PHONE456", 90, present=True),
        ]
        dm = DeviceManager()
        with patch.object(DeviceManager, "discover_devices", return_value=devices):
            serials = [d.serial for d in dm.get_available_devices(min_battery=20)]
        self.assertEqual(serials, ["192.168.1.10:5555", "PHONE456"])


class HealthGateTests(unittest.TestCase):
    def test_unhealthy_devices_excluded_and_no_internet_only_warns(self):
        from xts_agent.device.device_manager import DeviceInfo, DeviceManager, HealthReport

        devs = [DeviceInfo(s, "", "", "fp", 34, 100, "device", "aaos") for s in ("ok", "full", "offnet")]
        reports = {
            "ok": HealthReport(100, True, 9000, True, True, True),
            "full": HealthReport(100, True, 100, True, True, False, ["only 100 MB free on /data"]),
            "offnet": HealthReport(100, True, 9000, False, True, True),
        }
        with patch.object(DeviceManager, "check_device_health", side_effect=lambda s: reports[s]), \
                self.assertLogs("xts_agent.device.device_manager", "WARNING") as logs:
            kept = DeviceManager().filter_healthy(devs)
        self.assertEqual([d.serial for d in kept], ["ok", "offnet"])
        self.assertTrue(any("only 100 MB free" in m for m in logs.output))
        self.assertTrue(any("offnet has no validated internet" in m for m in logs.output))

    def test_health_report_lists_problems(self):
        from xts_agent.device.device_manager import DeviceManager

        def shell(serial, cmd, **kw):
            if cmd == "dumpsys battery":
                return "present: true\nlevel: 5\n"
            if cmd == "df /data":
                return "Filesystem 1K-blocks Used Available Use% Mounted\n/dev/x 100 90 102400 90% /data"
            return ""

        with patch("xts_agent.device.device_manager.AdbWrapper.shell", side_effect=shell):
            report = DeviceManager().check_device_health("s")
        self.assertFalse(report.healthy)
        self.assertEqual(report.problems, ["battery 5%", "only 100 MB free on /data"])


class LeaseTests(unittest.TestCase):
    HOLDER = """
import os, sys, time
from xts_agent.device.device_manager import DeviceInfo, DeviceManager
dm = DeviceManager(lease_dir=sys.argv[1])
dev = DeviceInfo("s1", "", "", "fp", 34, 100, "device", "aaos")
dm.allocate_devices(1, candidates=[dev])
if sys.argv[2] == "spawn-and-crash":
    import subprocess
    child = subprocess.Popen(["sleep", "30"], pass_fds=dm.lease_fds(["s1"]), start_new_session=True)
    print(child.pid, flush=True)
    os._exit(0)  # agent dies without releasing; TradeFed stand-in keeps running
print("held", flush=True)
time.sleep(30)
"""

    def _devices(self):
        from xts_agent.device.device_manager import DeviceInfo

        return [DeviceInfo(s, "", "", "fp", 34, 100, "device", "aaos") for s in ("s1", "s2")]

    def _spawn(self, lease_dir, mode):
        import subprocess
        import sys

        proc = subprocess.Popen(
            [sys.executable, "-c", self.HOLDER, lease_dir, mode], stdout=subprocess.PIPE, text=True
        )
        return proc, proc.stdout.readline().strip()

    def test_other_process_cannot_take_leased_device(self):
        from xts_agent.device.device_manager import DeviceManager

        with tempfile.TemporaryDirectory() as leases:
            holder, _ = self._spawn(leases, "hold")
            try:
                dm = DeviceManager(lease_dir=leases)
                self.assertIn("pid=", dm.leased_elsewhere("s1") or "")
                got = dm.allocate_devices(1, candidates=self._devices())
                self.assertEqual([d.serial for d in got], ["s2"])  # s1 skipped
                with self.assertRaises(ValueError) as ctx:
                    DeviceManager(lease_dir=leases).allocate_devices(2, candidates=self._devices())
                self.assertIn("leased by other processes", str(ctx.exception))
                dm.release_devices(["s2"])
            finally:
                holder.kill()
                holder.wait()
            # Holder died: the kernel dropped its lock
            self.assertIsNone(DeviceManager(lease_dir=leases).leased_elsewhere("s1"))

    def test_lease_survives_agent_crash_while_tradefed_runs(self):
        import os
        import signal
        import time as _time

        from xts_agent.device.device_manager import DeviceManager

        with tempfile.TemporaryDirectory() as leases:
            agent, child_pid = self._spawn(leases, "spawn-and-crash")
            agent.wait(timeout=10)
            try:
                self.assertIsNotNone(DeviceManager(lease_dir=leases).leased_elsewhere("s1"))
            finally:
                os.kill(int(child_pid), signal.SIGKILL)
            for _ in range(50):
                if DeviceManager(lease_dir=leases).leased_elsewhere("s1") is None:
                    break
                _time.sleep(0.1)
            self.assertIsNone(DeviceManager(lease_dir=leases).leased_elsewhere("s1"))


class DevicePrepTests(unittest.TestCase):
    def _cfg(self, **kw):
        from xts_agent.config_loader import DevicePrepConfig

        return DevicePrepConfig(**kw)

    def test_profile_steps(self):
        from xts_agent.device.device_prep import DevicePreparer

        cmds = [s["cmd"] for s in DevicePreparer(self._cfg(wifi_ssid="lab net", wifi_password="p w")).steps()]
        self.assertIn("locksettings set-disabled true", cmds)
        self.assertIn("cmd location set-location-enabled true", cmds)
        self.assertIn("cmd wifi connect-network 'lab net' wpa2 'p w'", cmds)
        no_wifi = [s["cmd"] for s in DevicePreparer(self._cfg()).steps()]
        self.assertFalse(any("wifi" in c for c in no_wifi))  # no SSID => skip wifi

    def test_wifi_password_never_logged(self):
        from xts_agent.device.adb_wrapper import AdbError
        from xts_agent.device.device_prep import DevicePreparer

        prep = DevicePreparer(self._cfg(wifi_ssid="lab", wifi_password="TopSecret!"))
        with patch("xts_agent.device.device_prep.AdbWrapper.shell",
                   side_effect=AdbError("Command failed: cmd wifi connect-network lab wpa2 TopSecret!")), \
                self.assertLogs("xts_agent.device", "INFO") as logs:
            failed = prep.prepare("hu")
        self.assertIn("join wifi lab", failed)
        self.assertNotIn("TopSecret!", "\n".join(logs.output))

    def test_wifi_password_from_env_and_defaults_section(self):
        import os

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XTS_WIFI_PASSWORD": "envpw"}):
            d = Path(tmp) / "d.yaml"
            d.write_text("device:\n  wifi_ssid: lab\n  extra_commands: ['cmd car_service x']\n")
            p = Path(tmp) / "p.yaml"
            p.write_text("name: t\nsuites: []\ndevices:\n  prepare: false\n")
            plan = ConfigLoader(p, defaults_path=d).load_plan()
        self.assertEqual(plan.device_prep.wifi_ssid, "lab")
        self.assertEqual(plan.device_prep.wifi_password, "envpw")
        self.assertEqual(plan.device_prep.extra_commands, ["cmd car_service x"])
        self.assertFalse(plan.devices.prepare)


class QuarantineTests(unittest.TestCase):
    def test_quarantine_after_consecutive_failures_and_reset(self):
        from xts_agent.device.device_ledger import DeviceLedger

        with tempfile.TemporaryDirectory() as tmp:
            ledger = DeviceLedger(Path(tmp) / "ledger.json", threshold=3, hours=1)
            self.assertFalse(ledger.record_failure("hu", "offline"))
            ledger.record_success("hu")  # success resets the streak
            self.assertFalse(ledger.record_failure("hu", "offline"))
            self.assertFalse(ledger.record_failure("hu", "reboot"))
            self.assertTrue(ledger.record_failure("hu", "reboot"))
            self.assertIn("3 consecutive failures", ledger.quarantine_reason("hu"))
            self.assertEqual(list(ledger.quarantined()), ["hu"])
            self.assertTrue(ledger.release("hu"))
            self.assertIsNone(ledger.quarantine_reason("hu"))

    def test_quarantined_device_not_available(self):
        from xts_agent.device.device_manager import DeviceInfo, DeviceManager

        with tempfile.TemporaryDirectory() as tmp:
            dm = DeviceManager(lease_dir=str(Path(tmp) / "leases"))
            dm.quarantine_threshold = 1
            dm.record_device_outcomes({"bad": False, "good": True}, "went offline during a suite")
            devs = [DeviceInfo(s, "", "", "fp", 34, 100, "device", "aaos") for s in ("bad", "good")]
            with patch.object(DeviceManager, "discover_devices", return_value=devs):
                self.assertEqual([d.serial for d in dm.get_available_devices()], ["good"])
            self.assertTrue((Path(tmp) / "device_ledger.json").exists())

    def test_device_lost_during_suite_is_recorded(self):
        executor = TestPlanExecutor.__new__(TestPlanExecutor)
        executor.device_manager = MagicMock()
        with patch("xts_agent.execution.test_plan_executor.AdbWrapper.devices", return_value=["a"]):
            _SURVIVAL_PATCH.temp_original(executor, ["a", "b"])
        executor.device_manager.record_device_outcomes.assert_called_once_with(
            {"a": True, "b": False}, "went offline during a suite"
        )

    def test_ledger_is_safe_across_processes(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            code = (
                "import sys\nfrom xts_agent.device.device_ledger import DeviceLedger\n"
                "l = DeviceLedger(sys.argv[1], threshold=1000)\n"
                "[l.record_failure(sys.argv[2], 'x') for _ in range(50)]\n"
            )
            procs = [subprocess.Popen([sys.executable, "-c", code, str(path), "hu"]) for _ in range(4)]
            for p in procs:
                p.wait()
            import json

            self.assertEqual(json.loads(path.read_text())["hu"]["consecutive_failures"], 200)


def _tc(cls, test, stack, module="x86_64 CtsTextTestCases"):
    from xts_agent.results.result_parser import TestCaseResult

    return TestCaseResult(cls, test, "FAIL", stack.split("\n")[0], stack, module=module)


FOCUS_STACK = (
    "junit.framework.AssertionFailedError: Timed out waiting for activity "
    "ComponentInfo{{android.text.cts/{act}}} to gain focus; {h} com.google.android.car.kitchensink "
    "was focused in 5003ms\n"
    "\tat junit.framework.Assert.fail(Assert.java:57)\n"
    "\tat android.server.wm.WindowManagerStateHelper.waitForFocus(WindowManagerStateHelper.java:{line})\n"
    "\tat {cls}.{test}({file}.java:{line2})\n"
)


class SignatureTests(unittest.TestCase):
    def _focus(self, cls, test, act, h, line):
        stack = FOCUS_STACK.format(act=act, h=h, line=line, cls=cls, test=test,
                                   file=cls.rsplit(".", 1)[-1], line2=line + 7)
        return _tc(cls, test, stack)

    def test_same_root_cause_across_tests_and_classes_groups_together(self):
        from xts_agent.triage.signature import compute_signature, group_failures

        a = self._focus("android.text.method.cts.KeyListenerTest", "testA", "A.KeyListenerCtsActivity", "5a3b2", 101)
        b = self._focus("android.widget.cts.ListViewTest", "testB", "B.ListViewCtsActivity", "9f0e1c", 202)
        self.assertEqual(compute_signature(a), compute_signature(b))
        groups = group_failures([("CTS", a), ("CTS", b)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].count, 2)
        self.assertIn("<component>", groups[0].message)

    def test_direct_assertions_in_different_test_classes_stay_separate(self):
        from xts_agent.triage.signature import compute_signature

        stack = "java.lang.AssertionError: expected:<1> but was:<2>\n\tat org.junit.Assert.fail(Assert.java:89)\n\tat {c}.t({c}.java:{n})"
        a = _tc("android.a.cts.ATest", "t", stack.format(c="android.a.cts.ATest", n=10))
        b = _tc("android.a.cts.ATest", "t2", stack.format(c="android.a.cts.ATest", n=99))
        c = _tc("android.b.cts.BTest", "t", stack.format(c="android.b.cts.BTest", n=10))
        self.assertEqual(compute_signature(a), compute_signature(b))  # line numbers ignored
        self.assertNotEqual(compute_signature(a), compute_signature(c))

    def test_different_exceptions_differ(self):
        from xts_agent.triage.signature import compute_signature

        npe = _tc("X", "t", "java.lang.NullPointerException: boom\n\tat X.t(X.java:1)")
        ise = _tc("X", "t", "java.lang.IllegalStateException: boom\n\tat X.t(X.java:1)")
        self.assertNotEqual(compute_signature(npe), compute_signature(ise))

    def test_normalize_message(self):
        from xts_agent.triage.signature import normalize_message

        self.assertEqual(
            normalize_message("event bindInput(pid=25752) not found within 5000ms: uid 0x1f"),
            "event bindInput(pid=<n>) not found within <n>ms: uid <hex>",
        )


class FailureHistoryTests(unittest.TestCase):
    def _run(self, start_ms, fp, outcomes, done=True):
        """outcomes: {test_name: "PASS"|"FAIL"} in module CtsM (x86_64)."""
        from xts_agent.results.result_parser import ModuleResult, TestCaseResult, TestResults

        cases = [
            TestCaseResult("c.T", name, res, "boom" if res == "FAIL" else None,
                           "java.lang.AssertionError: boom" if res == "FAIL" else None,
                           module="x86_64 CtsM")
            for name, res in outcomes.items()
        ]
        mod = ModuleResult("CtsM", done, 0, 0, 0, cases, abi="x86_64")
        res = TestResults("CTS", {"build_fingerprint": fp}, "", "", modules=[mod])
        res.start_ms = start_ms
        return res

    def _classify(self, history, current):
        from xts_agent.results.result_parser import ResultParser

        failed = ResultParser().get_failed_tests(current)
        labels = history.classify("CTS", current, [(t.module, t.test_id) for t in failed])
        return {k.split("#")[-1]: v for k, v in labels.items()}

    def test_labels_new_persistent_flaky_and_no_history(self):
        from xts_agent.triage.history import FailureHistory

        with tempfile.TemporaryDirectory() as tmp:
            h = FailureHistory(Path(tmp) / "h.db")
            h.record_run("CTS", self._run(1, "b1", {"new": "PASS", "pers": "FAIL", "flaky": "FAIL"}), results_dir="r1")
            h.record_run("CTS", self._run(2, "b2", {"new": "PASS", "pers": "FAIL", "flaky": "PASS"}), results_dir="r2")
            cur = self._run(3, "b3", {"new": "FAIL", "pers": "FAIL", "flaky": "FAIL", "fresh": "FAIL"})
            cur.modules.append(self._run(3, "b3", {"x": "FAIL"}).modules[0])
            cur.modules[-1].name = "CtsOther"
            for tc in cur.modules[-1].test_cases:
                tc.module = "x86_64 CtsOther"
            labels = self._classify(h, cur)
        self.assertEqual(labels["new"].label, "NEW")
        self.assertEqual(labels["new"].last_pass_build, "b2")
        self.assertEqual(labels["pers"].label, "PERSISTENT")
        self.assertEqual(labels["pers"].first_fail_build, "b1")
        self.assertEqual(labels["flaky"].label, "FLAKY")
        self.assertEqual(labels["x"].label, "NO_HISTORY")  # module never ran before

    def test_partial_module_is_not_evidence_of_pass(self):
        from xts_agent.triage.history import FailureHistory

        with tempfile.TemporaryDirectory() as tmp:
            h = FailureHistory(Path(tmp) / "h.db")
            # interrupted before reaching the test: module not done, no failure
            h.record_run("CTS", self._run(1, "b1", {"other": "PASS"}, done=False), results_dir="r1")
            labels = self._classify(h, self._run(2, "b2", {"t": "FAIL"}))
        self.assertEqual(labels["t"].label, "NO_HISTORY")

    def test_retry_session_replaces_same_invocation(self):
        import sqlite3

        from xts_agent.triage.history import FailureHistory

        with tempfile.TemporaryDirectory() as tmp:
            h = FailureHistory(Path(tmp) / "h.db")
            self.assertIsNotNone(h.record_run("CTS", self._run(1, "b", {"t": "FAIL"}), results_dir="2026.01.01_a"))
            # retry of the same invocation (same start) fixed it
            self.assertIsNotNone(h.record_run("CTS", self._run(1, "b", {"t": "PASS"}), results_dir="2026.01.02_b"))
            self.assertIsNone(h.record_run("CTS", self._run(1, "b", {"t": "FAIL"}), results_dir="2026.01.01_a"))
            conn = sqlite3.connect(Path(tmp) / "h.db")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM triage_runs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM triage_failures").fetchone()[0], 0)


class KnownIssueTests(unittest.TestCase):
    YAML = """
issues:
  - id: KI-1
    title: focus
    jira: AAOS-1
    classification: environment_issue
    match:
      message_regex: "kitchensink was focused"
    waiver:
      reason: cuttlefish only
      expires: 2026-12-31
      builds_regex: "aosp_cf"
  - id: KI-2
    title: bad waiver
    match: {module_regex: "CtsX"}
    waiver: {reason: "no expiry"}
  - id: KI-3
    title: cert waiver
    match: {test_regex: "CtsCar"}
    waiver: {reason: ok, expires: 2026-12-31, profiles: [certification, development]}
"""

    def _db(self):
        from xts_agent.triage.known_issues import KnownIssueDB

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ki.yaml"
            p.write_text(self.YAML, encoding="utf-8")
            with self.assertLogs("xts_agent.triage.known_issues", "ERROR"):
                return KnownIssueDB.load(p)

    def _group(self, msg, module="x86_64 CtsTextTestCases"):
        from xts_agent.triage.signature import group_failures

        return group_failures([("CTS", _tc("c.T", "t", f"junit.framework.AssertionFailedError: {msg}", module))])[0]

    def test_waiver_rules(self):
        import datetime as dt

        db = self._db()
        self.assertEqual([i.id for i in db.issues], ["KI-1", "KI-3"])  # no-expiry waiver rejected
        g = self._group("x com.google.android.car.kitchensink was focused in 5s")
        day = dt.date(2026, 10, 7)
        m = db.match(g, "development", "generic/aosp_cf_x86_64_auto/x:userdebug", day)
        self.assertEqual((m.issue.id, m.issue.jira, m.waived), ("KI-1", "AAOS-1", True))
        self.assertEqual(m.issue.classification, "ENVIRONMENT_ISSUE")
        self.assertFalse(db.match(g, "certification", "aosp_cf", day).waived)  # dev-only waiver
        self.assertFalse(db.match(g, "development", "oem/hu/hu:user", day).waived)  # other build
        expired = db.match(g, "development", "aosp_cf", dt.date(2027, 1, 1))
        self.assertFalse(expired.waived)
        self.assertTrue(expired.waiver_expired)
        self.assertIsNone(db.match(self._group("unrelated"), "development", "aosp_cf", day))

    def test_certification_waiver_must_be_explicit(self):
        import datetime as dt

        g = self._group("boom", module="x86_64 CtsCarTestCases")
        g.tests[0].module = "x86_64 CtsCarTestCases"
        m = self._db().match(g, "certification", "fp", dt.date(2026, 10, 7))
        self.assertTrue(m.waived)


class OwnershipTests(unittest.TestCase):
    def test_routing_first_match_default_and_group_majority(self):
        from xts_agent.triage.ownership import OwnershipMap
        from xts_agent.triage.signature import FailureGroup

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "own.yaml"
            p.write_text(
                "default: {team: triage}\nrules:\n"
                "  - {module_regex: '^CtsCar', team: car, jira_component: Car, assignee: alice}\n"
                "  - {module_regex: '^Cts', team: catch-all}\n"
                "  - {module_regex: '[', team: broken}\n"
            )
            with self.assertLogs("xts_agent.triage.ownership", "ERROR"):
                om = OwnershipMap.load(p)
        self.assertEqual(om.owner_for_module("x86_64 CtsCarTestCases[instant]").assignee, "alice")
        self.assertEqual(om.owner_for_module("arm64-v8a CtsMediaTestCases").team, "catch-all")
        self.assertEqual(om.owner_for_module("VtsHal").team, "triage")
        g = FailureGroup("s", "E", "m", [])
        g.tests = [_tc("c", "t", "E: m", module=m) for m in
                   ("x86_64 CtsCarA", "x86_64 CtsMedia", "x86_64 CtsCarA")]
        self.assertEqual(om.owner_for_group(g).team, "car")

    def test_shipped_starter_map_loads(self):
        from xts_agent.triage.ownership import OwnershipMap

        om = OwnershipMap.load("config/ownership.yaml")
        self.assertGreater(len(om.rules), 10)
        self.assertEqual(om.owner_for_module("x86_64 CtsCarTestCases").team, "aaos-car-framework")


class TriageEngineTests(unittest.TestCase):
    def _plan_result(self, tests, fp="aosp_cf/x:userdebug", start=10):
        from xts_agent.results.result_parser import ModuleResult, TestResults

        mods = {}
        for tc in tests:
            abi, name = tc.module.split(" ", 1)
            mods.setdefault(tc.module, ModuleResult(name, True, 0, 0, 0, [], abi=abi)).test_cases.append(tc)
        details = TestResults("CTS", {"build_fingerprint": fp}, "", "", modules=list(mods.values()))
        details.start_ms = start
        suite = SuiteResult("CTS", "FAILED", 0, len(tests), 0, 1.0, 1, "", 0, details=details)
        return PlanResult("cert", {"CTS": suite}, 0, len(tests), 0, 1.0, "FAILED", profile="development")

    def test_end_to_end_grouping_known_owner_history(self):
        import datetime as dt

        from xts_agent.triage.engine import TriageEngine
        from xts_agent.triage.history import FailureHistory
        from xts_agent.triage.known_issues import KnownIssue, KnownIssueDB, Waiver
        from xts_agent.triage.ownership import OwnershipMap

        focus = [
            _tc("a.T", f"t{i}", "junit.framework.AssertionFailedError: kitchensink was focused 5003ms", "x86_64 CtsTextTestCases")
            for i in range(3)
        ]
        npe = [_tc("b.T", "t", "java.lang.NullPointerException: x\n\tat b.T.t(T.java:1)", "x86_64 CtsCarTestCases")]
        tracked = [_tc("c.T", "t", "java.lang.IllegalStateException: tracked\n\tat c.T.t(T.java:1)", "x86_64 CtsCarTestCases")]
        db = KnownIssueDB([
            KnownIssue("KI-1", "focus", classification="ENVIRONMENT_ISSUE", message_regex="kitchensink",
                       waiver=Waiver("cf only", dt.date(2099, 1, 1))),
            KnownIssue("KI-2", "tracked", jira="AAOS-9", message_regex="tracked"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own.yaml"
            own.write_text("rules:\n  - {module_regex: '^CtsCar', team: car}\n")
            history = FailureHistory(Path(tmp) / "h.db")
            engine = TriageEngine(history, db, OwnershipMap.load(own))
            # earlier run: the NPE test passed (module completed without it failing)
            first = self._plan_result(focus + tracked + [_tc("b.T", "t", "", "x86_64 CtsCarTestCases")], start=1)
            first.suites_results["CTS"].details.modules[1].test_cases[-1].result = "PASS"
            engine.triage(first)
            report = engine.triage(self._plan_result(focus + npe + tracked, start=2))

        by_title = {g.group.title.split(":")[0]: g for g in report.groups}
        focus_g = by_title["AssertionFailedError"]
        self.assertEqual(focus_g.group.count, 3)
        self.assertTrue(focus_g.waived)
        self.assertEqual(focus_g.classification, "ENVIRONMENT_ISSUE")
        self.assertEqual(focus_g.label, "PERSISTENT")
        npe_g = by_title["NullPointerException"]
        self.assertEqual((npe_g.label, npe_g.owner.team, npe_g.actionable), ("NEW", "car", True))
        self.assertEqual(npe_g.last_pass_build, "aosp_cf/x:userdebug")
        tracked_g = by_title["IllegalStateException"]
        self.assertEqual((tracked_g.jira_key, tracked_g.actionable), ("AAOS-9", False))
        s = report.summary
        self.assertEqual((s["failures"], s["groups"], s["actionable_groups"], s["waived_groups"]), (5, 3, 1, 1))
        self.assertEqual(s["by_team"], {"car": 1})


class JiraFilerTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, existing=None, reject_components=False):
            self.existing = existing or {}
            self.reject_components = reject_components
            self.created, self.comments = [], []

        def find_open_by_label(self, project, label):
            return self.existing.get(label)

        def create(self, fields):
            from xts_agent.triage.jira_filer import JiraError

            if self.reject_components and "components" in fields:
                raise JiraError(400, '{"errors":{"components":"Component name not valid"}}')
            self.created.append(fields)
            return f"AAOS-{100 + len(self.created)}"

        def comment(self, key, body):
            self.comments.append((key, body))

    def _report(self, specs):
        """specs: list of (signature, label, waived, jira_key)."""
        from xts_agent.triage.engine import TriagedGroup, TriageReport
        from xts_agent.triage.known_issues import KnownIssue, KnownIssueMatch
        from xts_agent.triage.ownership import Owner
        from xts_agent.triage.signature import FailureGroup

        report = TriageReport("cert", "development", "fp:user/k")
        for sig, label, waived, jira in specs:
            g = FailureGroup(sig, "java.lang.NullPointerException", "boom", [])
            g.tests = [_tc("c.T", "t", "java.lang.NullPointerException: boom", "x86_64 CtsCarTestCases")]
            g.suites = ["CTS"]
            known = KnownIssueMatch(KnownIssue("KI", "k", jira=jira), waived) if (waived or jira) else None
            report.groups.append(TriagedGroup(g, label, {}, Owner("car", "Car Framework"), "PRODUCT_BUG",
                                              known=known, jira_key=jira))
        return report

    def _cfg(self, **kw):
        from xts_agent.triage.jira_filer import JiraConfig

        return JiraConfig(enabled=True, project="AAOS", **kw)

    def test_live_dedupes_creates_and_skips(self):
        from xts_agent.triage.jira_filer import JiraFiler

        client = self.FakeClient(existing={"xts-sig-old": "AAOS-7"}, reject_components=True)
        report = self._report([
            ("old", "NEW", False, ""),          # open ticket exists -> comment
            ("new1", "NEW", False, ""),         # create (component rejected -> retry)
            ("pers", "PERSISTENT", False, ""),  # no ticket for PERSISTENT by default
            ("waived", "NEW", True, ""),        # waived -> skip
            ("tracked", "NEW", False, "AAOS-1"),  # known issue with ticket -> skip
        ])
        stats = JiraFiler(self._cfg(mode="live"), client).file(report)
        self.assertEqual((stats["created"], stats["commented"]), (1, 1))
        self.assertEqual(client.comments[0][0], "AAOS-7")
        created = client.created[0]
        self.assertNotIn("components", created)  # retried without unknown component
        self.assertIn("xts-sig-new1", created["labels"])
        self.assertTrue(created["summary"].startswith("[xTS][CTS] NullPointerException: boom"))
        by_sig = {g.group.signature: g for g in report.groups}
        self.assertEqual((by_sig["new1"].jira_key, by_sig["new1"].jira_action), ("AAOS-101", "created"))
        self.assertEqual(by_sig["pers"].jira_key, "")

    def test_dry_run_previews_and_caps(self):
        import json

        from xts_agent.triage.jira_filer import JiraFiler

        report = self._report([(f"s{i}", "NEW", False, "") for i in range(5)])
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp) / "preview.json"
            stats = JiraFiler.from_config(self._cfg(max_new_issues_per_run=3)).file(report, preview)
            self.assertEqual((stats["would_create"], stats["skipped_cap"]), (3, 2))
            self.assertEqual(len(json.loads(preview.read_text())), 3)
        self.assertEqual(report.groups[0].jira_action, "would create")

    def test_live_requires_token_and_errors_hide_it(self):
        import os

        from xts_agent.triage.jira_filer import JiraClient, JiraError, JiraFiler

        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            JiraFiler.from_config(self._cfg(mode="live", base_url="https://jira"))
        client = JiraClient(self._cfg(base_url="https://jira"), "S3CRET")
        self.assertEqual(client.session.headers["Authorization"], "Bearer S3CRET")
        resp = MagicMock(status_code=401, text="Unauthorized", content=b"x")
        with patch.object(client.session, "request", return_value=resp), self.assertRaises(JiraError) as ctx:
            client.create({})
        self.assertNotIn("S3CRET", str(ctx.exception))


class GroupAiTests(unittest.TestCase):
    def test_parse_validates_and_clamps(self):
        from xts_agent.triage.ai_rca import parse_ai_json

        ok = parse_ai_json('Sure! {"root_cause": "focus stolen", "classification": "environment_issue", '
                           '"confidence": 1.7, "suggested_fix": "disable kitchensink"} done')
        self.assertEqual((ok["classification"], ok["confidence"]), ("ENVIRONMENT_ISSUE", 1.0))
        self.assertEqual(parse_ai_json('{"root_cause": "x", "classification": "ALIENS"}')["classification"], "UNKNOWN")
        self.assertIsNone(parse_ai_json("I think it is a product bug"))
        self.assertIsNone(parse_ai_json('{"classification": "PRODUCT_BUG"}'))  # no root cause

    def test_one_call_per_actionable_group_capped_and_cached(self):
        from xts_agent.triage.ai_rca import GroupAIAnalyzer

        report = JiraFilerTests._report(None, [
            ("a", "NEW", False, ""), ("b", "NEW", False, ""), ("c", "NEW", False, ""),
            ("waived", "NEW", True, ""),
        ])
        provider = MagicMock(model_id="llama_cpp:test.gguf")
        provider.generate.return_value = '{"root_cause": "r", "classification": "PRODUCT_BUG", "confidence": 0.8}'
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "c.db")
            stats = GroupAIAnalyzer(provider, cache_db=db, max_groups=2).analyze(report)
            self.assertEqual(stats["analyzed"], 2)
            self.assertEqual(provider.generate.call_count, 2)
            self.assertTrue(all(c.kwargs.get("json_mode") for c in provider.generate.call_args_list))
            self.assertIn("1 failing tests", provider.generate.call_args_list[0][0][0])
            self.assertIsNone(report.groups[3].ai)  # cap reached: waived group not analysed

            again = JiraFilerTests._report(None, [("a", "NEW", False, "")])
            stats = GroupAIAnalyzer(provider, cache_db=db).analyze(again)
            self.assertEqual((stats["cached"], provider.generate.call_count), (1, 2))
            self.assertTrue(again.groups[0].ai["cached"])

    def test_agreement_with_human_classified_known_issues(self):
        from xts_agent.triage.ai_rca import GroupAIAnalyzer

        report = JiraFilerTests._report(None, [("w1", "NEW", True, ""), ("w2", "NEW", True, "")])
        report.groups[0].known.issue.classification = "ENVIRONMENT_ISSUE"
        report.groups[1].known.issue.classification = "PRODUCT_BUG"
        provider = MagicMock(model_id="m")
        provider.generate.return_value = '{"root_cause": "r", "classification": "ENVIRONMENT_ISSUE", "confidence": 0.9}'
        GroupAIAnalyzer(provider, max_groups=5).analyze(report)  # spare slots go to eval
        self.assertEqual(report.summary["ai_agreement"], {"evaluated": 2, "agreed": 1, "rate": 0.5})

    def test_unparseable_output_is_ignored(self):
        from xts_agent.triage.ai_rca import GroupAIAnalyzer

        report = JiraFilerTests._report(None, [("a", "NEW", False, "")])
        provider = MagicMock(model_id="m")
        provider.generate.return_value = "Failed to generate RCA using llama.cpp: boom"
        stats = GroupAIAnalyzer(provider).analyze(report)
        self.assertEqual(stats["unparseable"], 1)
        self.assertIsNone(report.groups[0].ai)

    def test_chunk_ids_are_stable(self):
        from xts_agent.rca.code_indexer import CHUNK_LINES, OEMCodeIndexer

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "Foo.java"
            f.write_text("\n".join(f"int line{i} = {i}; // padding padding padding" for i in range(130)))
            first = OEMCodeIndexer.chunk_file(f)
            second = OEMCodeIndexer.chunk_file(f)
        self.assertEqual([c[0] for c in first], [c[0] for c in second])
        self.assertTrue(all(len(c[1].split("\n")) <= CHUNK_LINES for c in first))
        self.assertEqual(first[1][2], CHUNK_LINES - 10 + 1)  # overlapping windows


class CompactReportTests(unittest.TestCase):
    def _plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "test_result.xml"
            xml.write_text(SAMPLE_XML, encoding="utf-8")
            details = ResultParser().parse_xml(xml)
        suite = SuiteResult("cts", "FAILED", 2, 1, 2, 1.0, 1, "", 0, details=details)
        return PlanResult("p", {"cts": suite}, 2, 1, 2, 1.0, "FAILED")

    def test_json_keeps_failures_only(self):
        import json

        from xts_agent.reporting.json_report import JSONReportGenerator

        with tempfile.TemporaryDirectory() as tmp:
            out = JSONReportGenerator().generate(self._plan(), None, None, Path(tmp) / "r.json")
            data = json.loads(out.read_text())
        mod = data["suites"]["cts"]["details"]["modules"][0]
        self.assertEqual((mod["pass"], mod["fail"]), (2, 1))
        self.assertEqual([f["test_name"] for f in mod["failures"]], ["testC"])
        self.assertNotIn("test_cases", mod)

    def test_junit_failures_mode_vs_all(self):
        import xml.etree.ElementTree as ET

        from xts_agent.reporting.gitlab_report import GitLabReportGenerator

        with tempfile.TemporaryDirectory() as tmp:
            small = ET.parse(GitLabReportGenerator().generate(self._plan(), Path(tmp) / "s.xml")).getroot()
            full = ET.parse(GitLabReportGenerator("all").generate(self._plan(), Path(tmp) / "a.xml")).getroot()
        names = [c.get("name") for c in small.iter("testcase")]
        self.assertEqual(names, ["module summary (2 passed)", "testC"])
        self.assertEqual(len(list(full.iter("testcase"))), 5)
        self.assertEqual(len(list(small.iter("failure"))), len(list(full.iter("failure"))))


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

        run = FailureHistoryTests._run
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
                labels = FailureHistoryTests._classify(None, h, cur)
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
            orch.last_triage = None
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
