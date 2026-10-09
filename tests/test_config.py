"""Plan/default config loading, profiles, unknown keys, AI RCA settings, secrets."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import support
from xts_agent.config_loader import ConfigLoader
from xts_agent.execution.tradefed_runner import TradefedRunner

setUpModule = support.setUpModule
tearDownModule = support.tearDownModule


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


class ConfigKeyTests(unittest.TestCase):
    def test_shipped_configs_have_no_unknown_keys(self):
        import glob

        import yaml

        from xts_agent.config_loader import _defaults_schema, _plan_schema, warn_unknown_keys

        defaults = yaml.safe_load(open("config/default_config.yaml"))
        self.assertEqual(warn_unknown_keys(defaults, _defaults_schema(), Path("d")), [])
        for plan in glob.glob("config/test_plans/*.yaml"):
            with self.subTest(plan=plan):
                data = yaml.safe_load(open(plan))
                self.assertEqual(warn_unknown_keys(data, _plan_schema(), Path(plan)), [])

    def test_typos_are_reported(self):
        from xts_agent.config_loader import _plan_schema, warn_unknown_keys

        data = {"name": "x", "suites": [{"name": "cts", "retry": {"max_retires": 2}}], "devcies": {}}
        with self.assertLogs("xts_agent.config_loader", "WARNING"):
            unknown = warn_unknown_keys(data, _plan_schema(), Path("p.yaml"))
        self.assertEqual(sorted(unknown), ["devcies", "suites[0].retry.max_retires"])

    def test_diagnostics_and_sharding_reach_tradefed(self):
        import dataclasses

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "d.yaml"
            d.write_text("diagnostics: {bugreport_on_failure: false, max_logcat_size_mb: 2}\n"
                         "sharding: {dynamic_sharding: false, token_sharding: true}\n")
            p = Path(tmp) / "p.yaml"
            p.write_text("name: t\nsuites:\n- name: cts\n  artifacts: {bugreport_on_failure: true}\n"
                         "  sharding: {intra_module_sharding: false}\n")
            suite = ConfigLoader(p, defaults_path=d).load_plan().suites[0]
        cmd = TradefedRunner("/tmp/android-cts", "cts-tradefed").build_run_command(
            "cts", shard_count=2, device_serials=["a", "b"],
            diagnostics=dataclasses.asdict(suite.diagnostics),
            sharding_options=dataclasses.asdict(suite.sharding),
        )
        for flag in ("--bugreport-on-failure", "--no-dynamic-sharding", "--no-intra-module-sharding",
                     "--enable-token-sharding", "--logcat-on-failure"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--logcat-on-failure-size") + 1], str(2 * 1024 * 1024))


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


if __name__ == "__main__":
    unittest.main()
