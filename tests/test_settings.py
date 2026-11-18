"""Host settings from the console: suite installations per architecture, tool paths, LLM endpoint."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


def make_package(root: Path, suite: str, abis=("x86_64", "x86"), build="16044444") -> Path:
    pkg = root / f"android-{suite}"
    (pkg / "tools").mkdir(parents=True)
    (pkg / "tools" / "tradefed.jar").write_text("", encoding="utf-8")
    (pkg / "tools" / f"{suite}-tradefed").write_text("#!/bin/sh\n", encoding="utf-8")
    (pkg / "tools" / "version.txt").write_text(build + "\n", encoding="utf-8")
    for mod in ("CtsFooTestCases", "CtsBarTestCases"):
        for abi in abis:
            (pkg / "testcases" / mod / abi).mkdir(parents=True)
    return pkg


class InspectAndScanTests(unittest.TestCase):
    def test_inspect_detects_suite_build_and_arch(self):
        from xts_agent.settings import inspect_package

        with tempfile.TemporaryDirectory() as tmp:
            x86 = make_package(Path(tmp) / "x86_64", "cts")
            arm = make_package(Path(tmp) / "arm64", "vts", abis=("arm64", "arm"))
            i = inspect_package(str(x86))
            self.assertEqual((i["ok"], i["suite"], i["build"], i["abis"], i["arch"], i["modules"]),
                             (True, "cts", "16044444", ["x86", "x86_64"], "x86_64", 2))
            self.assertEqual(inspect_package(str(arm))["arch"], "arm64")
            bad = inspect_package(str(Path(tmp) / "nope"), "cts")
            self.assertFalse(bad["ok"])
            self.assertIn("does not exist", bad["problems"][0])
            self.assertIn("absolute", inspect_package("relative/path")["problems"][0])
            # a VTS folder checked as CTS is flagged
            self.assertIn("tools/cts-tradefed missing", inspect_package(str(arm), "cts")["problems"])

    def test_scan_finds_packages_per_arch(self):
        from xts_agent.settings import scan

        with tempfile.TemporaryDirectory() as tmp:
            make_package(Path(tmp) / "x86_64", "cts")
            make_package(Path(tmp) / "arm64", "cts", abis=("arm64",))
            found = sorted((f["suite"], f["arch"]) for f in scan(tmp))
            self.assertEqual(found, [("cts", "arm64"), ("cts", "x86_64")])
            self.assertEqual(scan(str(Path(tmp) / "missing")), [])


class ResolveAndValidateTests(unittest.TestCase):
    def test_resolution_order(self):
        from xts_agent.settings import resolve_package_path

        suites = {"default_arch": "x86_64", "installations": [
            {"suite": "cts", "arch": "x86_64", "path": "/x/cts"},
            {"suite": "cts", "arch": "arm64", "path": "/a/cts"},
            {"suite": "vts", "arch": "arm64", "path": "/a/vts"},
        ]}
        self.assertEqual(resolve_package_path("cts", "arm64", suites, "/opt/xts"), "/a/cts")
        self.assertEqual(resolve_package_path("CTS", "", suites, "/opt/xts"), "/x/cts")  # default_arch
        self.assertEqual(resolve_package_path("vts", "x86_64", suites, "/opt/xts"), "/opt/xts/android-vts")
        self.assertEqual(resolve_package_path("sts", "arm64", {}, "/opt/xts"), "/opt/xts/android-sts")

    def test_validate_rejects_bad_input(self):
        from xts_agent.settings import validate

        ok = validate({"paths": {"java_home": "/jdk", "adb_path": ""},
                       "suites": {"default_arch": "arm64", "installations": [{"suite": "CTS", "arch": "arm64", "path": "/a/cts"}]},
                       "ai_rca": {"openai_base_url": "http://127.0.0.1:8011/v1", "openai_model": "coder"}})
        self.assertEqual(ok["paths"], {"java_home": "/jdk"})
        self.assertEqual(ok["suites"]["installations"], [{"suite": "cts", "arch": "arm64", "path": "/a/cts"}])
        self.assertEqual(ok["ai_rca"]["provider"], "openai_compatible")
        for bad in (
            {"paths": {"java_home": "jdk"}},
            {"suites": {"installations": [{"suite": "xyz", "arch": "arm64", "path": "/p"}]}},
            {"suites": {"installations": [{"suite": "cts", "arch": "mips", "path": "/p"}]}},
            {"suites": {"installations": [{"suite": "cts", "arch": "arm64", "path": "/p"},
                                          {"suite": "cts", "arch": "arm64", "path": "/q"}]}},
            {"ai_rca": {"openai_base_url": "file:///etc/passwd"}},
        ):
            with self.assertRaises(ValueError, msg=bad):
                validate(bad)


class LoaderIntegrationTests(unittest.TestCase):
    def _write(self, tmp, local, plan):
        cfg = Path(tmp) / "config"
        cfg.mkdir()
        (cfg / "default_config.yaml").write_text(yaml.safe_dump({"paths": {"xts_packages_dir": "/opt/xts", "java_home": "/old"}}),
                                                 encoding="utf-8")
        (cfg / "local_settings.yaml").write_text(yaml.safe_dump(local), encoding="utf-8")
        plan_path = cfg / "plan.yaml"
        plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")
        return cfg / "default_config.yaml", plan_path

    def test_local_settings_drive_suite_paths_and_tools(self):
        from xts_agent.config_loader import ConfigLoader

        local = {"paths": {"java_home": "/jdk21"},
                 "suites": {"default_arch": "x86_64", "installations": [
                     {"suite": "cts", "arch": "x86_64", "path": "/x/android-cts"},
                     {"suite": "cts", "arch": "arm64", "path": "/a/android-cts"}]},
                 "ai_rca": {"openai_base_url": "http://127.0.0.1:9/v1", "openai_model": "m"}}
        plan = {"name": "P", "suites": [{"name": "cts", "plan": "cts"},
                                         {"name": "vts", "plan": "vts", "package_path": "/pinned/vts"}]}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XTS_LOCAL_SETTINGS", None)
            defaults, plan_path = self._write(tmp, local, plan)
            cfg = ConfigLoader(plan_path, defaults).load_plan()
            cts, vts = cfg.suites
            self.assertEqual((cts.package_path, cts.arch), ("/x/android-cts", "x86_64"))
            self.assertEqual(vts.package_path, "/pinned/vts")  # explicit plan path still wins
            self.assertEqual(cfg.paths.java_home, "/jdk21")
            self.assertEqual(cfg.ai_rca.openai_model, "m")

            plan["arch"] = "arm64"
            plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")
            self.assertEqual(ConfigLoader(plan_path, defaults).load_plan().suites[0].package_path, "/a/android-cts")


class SettingsApiTests(unittest.TestCase):
    def _app(self, tmp):
        from xts_agent.ui.server import UiApp, UiConfig

        root = Path(tmp)
        (root / "config" / "test_plans").mkdir(parents=True)
        (root / "config" / "default_config.yaml").write_text(
            yaml.safe_dump({"paths": {"xts_packages_dir": str(root / "xts")}}), encoding="utf-8")
        (root / "config" / "test_plans" / "smoke.yaml").write_text(
            "name: Smoke\nsuites:\n  - {name: cts, plan: cts, include_filters: [CtsFooTestCases]}\n", encoding="utf-8")
        cfg = UiConfig(root=root, results_dir=root / "results", plans_dir=root / "config" / "test_plans",
                       db_path=None, lease_dir=str(root / "leases"), defaults_path=str(root / "config" / "default_config.yaml"))
        return UiApp(cfg), root

    def test_save_load_scan_and_plan_resolution(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XTS_LOCAL_SETTINGS", None)
            app, root = self._app(tmp)
            x86 = make_package(root / "xts", "cts")
            arm = make_package(root / "arm", "android-cts".split("-")[1], abis=("arm64",))
            st = app.settings()
            self.assertEqual([d["suite"] for d in st["discovered"]], ["cts"])  # under xts_packages_dir
            self.assertFalse(st["file_exists"])
            saved = app.save_settings({"suites": {"default_arch": "x86_64", "installations": [
                {"suite": "cts", "arch": "x86_64", "path": str(x86)}, {"suite": "cts", "arch": "arm64", "path": str(arm)}]},
                "paths": {"java_home": "/jdk"}, "ai_rca": {}})
            self.assertTrue(Path(saved["file"]).exists())
            self.assertEqual({(i["arch"], i["inspect"]["arch"]) for i in saved["installations"]},
                             {("x86_64", "x86_64"), ("arm64", "arm64")})
            self.assertEqual(saved["paths"]["java_home"], "/jdk")
            (plan,) = app.plans()
            res = plan["suites"][0]["resolved"]
            self.assertEqual((res["x86_64"]["path"], res["x86_64"]["ok"]), (str(x86), True))
            self.assertEqual((res["arm64"]["path"], res["arm64"]["ok"]), (str(arm), True))
            # clearing a section in the form removes it from the file
            app.save_settings({"paths": {}})
            self.assertNotIn("paths", yaml.safe_load(Path(saved["file"]).read_text(encoding="utf-8")))

    def test_launch_with_arch_and_llm_test_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = self._app(tmp)
            cmd, _, _ = app.build_launch({"plan": "smoke.yaml", "arch": "arm64"})
            derived = yaml.safe_load(Path(cmd[cmd.index("--plan") + 1]).read_text(encoding="utf-8"))
            self.assertEqual(derived["arch"], "arm64")
            with self.assertRaises(ValueError):
                app.build_launch({"plan": "smoke.yaml", "arch": "mips"})
            with self.assertRaises(ValueError):  # no requests to arbitrary hosts from the console
                app.test_llm({"base_url": "http://example.com/v1"})
            out = app.test_llm({"base_url": "http://127.0.0.1:9/v1", "model": "m"})  # nothing listens on :9
            self.assertFalse(out["ok"])

    def test_multi_provider_ai_settings_llama_cpp_online_and_disabled(self):
        from xts_agent.settings import check_tool_path, merge_into_defaults, validate

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XTS_LOCAL_SETTINGS", None)
            os.environ.pop("XTS_LLM_API_KEY", None)
            os.environ.pop("XTS_GEMINI_API_KEY", None)
            app, root = self._app(tmp)
            gguf = root / "model.gguf"
            gguf.write_bytes(b"GGUF" * 512)

            # 1) llama_cpp GGUF validation & tool check
            v_llama = validate({
                "ai_rca": {
                    "provider": "llama_cpp",
                    "llama_model_path": str(gguf),
                    "llama_n_ctx": 16384,
                    "llama_n_gpu_layers": -1,
                    "enabled": True,
                }
            })
            self.assertEqual(v_llama["ai_rca"]["provider"], "llama_cpp")
            self.assertEqual(v_llama["ai_rca"]["llama_model_path"], str(gguf))
            with self.assertRaises(ValueError):
                validate({"ai_rca": {"provider": "llama_cpp"}})  # missing path
            with self.assertRaises(ValueError):
                validate({"ai_rca": {"provider": "llama_cpp", "llama_model_path": "rel/model.gguf"}})
            chk_missing = check_tool_path("llama_model_path", str(root / "nope.gguf"))
            self.assertFalse(chk_missing["ok"])
            with patch("xts_agent.settings.has_llama_cpp_pkg", return_value=True):
                t_llama = app.test_llm({"provider": "llama_cpp", "llama_model_path": str(gguf)})
                self.assertTrue(t_llama["ok"])

            # 2) Online API (ChatGPT / Gemini) requires explicit allow_external_providers + https + env key
            v_online = validate({
                "ai_rca": {
                    "provider": "openai_compatible",
                    "openai_base_url": "https://api.openai.com/v1",
                    "openai_model": "gpt-4.1-mini",
                    "allow_external_providers": True,
                }
            })
            self.assertTrue(v_online["ai_rca"]["allow_external_providers"])
            with self.assertRaises(ValueError):
                app.test_llm({"provider": "openai_compatible", "base_url": "https://api.openai.com/v1", "allow_external_providers": False})
            no_key = app.test_llm({"provider": "openai_compatible", "base_url": "https://api.openai.com/v1", "allow_external_providers": True})
            self.assertFalse(no_key["ok"])
            self.assertIn("XTS_LLM_API_KEY", no_key["error"])

            with self.assertRaises(ValueError):
                app.test_llm({"provider": "gemini", "allow_external_providers": False})
            no_gkey = app.test_llm({"provider": "gemini", "allow_external_providers": True})
            self.assertFalse(no_gkey["ok"])
            self.assertIn("XTS_GEMINI_API_KEY", no_gkey["error"])

            # 3) Disabled mode ("none") forces enabled: False and succeeds cleanly on test_llm
            v_none = validate({"ai_rca": {"provider": "none", "enabled": True}})
            self.assertEqual(v_none["ai_rca"]["provider"], "none")
            self.assertFalse(v_none["ai_rca"]["enabled"])
            self.assertTrue(app.test_llm({"provider": "none"})["ok"])

            # 4) merge_into_defaults clears default vLLM openai_extra_body when URL changes (e.g. to api.openai.com)
            merged = merge_into_defaults(
                {"ai_rca": {"openai_base_url": "http://127.0.0.1:8011/v1", "openai_extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}},
                v_online,
            )
            self.assertEqual(merged["ai_rca"]["openai_extra_body"], {})


if __name__ == "__main__":
    unittest.main()
