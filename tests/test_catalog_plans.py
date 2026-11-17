"""Module catalog from the suite's own commands, and plans created from the console."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

LIST_MODULES = """==================
Android Compatibility Test Suite 17_r2 (16044444)
Using commandline arguments as starting command: [list, modules]
x86_64 AconfigPublicApiCtsTests[secondary_user]
x86_64 AconfigPublicApiCtsTests
x86 AconfigPublicApiCtsTests
x86_64 CtsBionicTestCases
x86 CtsBionicTestCases
x86_64 signed-CtsSecureElementAccessControlTestCases3
cts-console > Received EOF.
"""
LIST_PLANS = """Using commandline arguments as starting command: [list, plans]
Available plans include:
  cts: Runs CTS from a pre-existing CTS installation
  cts-dev: Runs CTS with common options set developer workflow: skips most checks
cts-console > Received EOF.
"""


class CatalogTests(unittest.TestCase):
    def test_parse_groups_abis_and_variants(self):
        from xts_agent.suites.catalog import parse_modules, parse_plans

        mods = {m["name"]: m for m in parse_modules(LIST_MODULES)}
        self.assertEqual(sorted(mods), ["AconfigPublicApiCtsTests", "CtsBionicTestCases",
                                        "signed-CtsSecureElementAccessControlTestCases3"])
        self.assertEqual(mods["AconfigPublicApiCtsTests"]["abis"], ["x86_64", "x86"])
        self.assertEqual(mods["AconfigPublicApiCtsTests"]["params"], ["secondary_user"])
        self.assertEqual([p["name"] for p in parse_plans(LIST_PLANS)], ["cts", "cts-dev"])

    def test_cached_per_build_and_falls_back_to_testcases(self):
        from xts_agent.suites import catalog

        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "android-cts"
            (pkg / "tools").mkdir(parents=True)
            (pkg / "tools" / "version.txt").write_text("111\n", encoding="utf-8")
            (pkg / "testcases" / "CtsFoo" / "x86_64").mkdir(parents=True)
            (pkg / "testcases" / "CtsFoo" / "CtsFoo.config").write_text("", encoding="utf-8")
            cache = Path(tmp) / "cache"
            with patch.object(catalog, "_run_tradefed", return_value=LIST_MODULES) as run:
                first = catalog.list_modules(str(pkg), "cts", cache)
                second = catalog.list_modules(str(pkg), "cts", cache)
                self.assertEqual(run.call_count, 1)
            self.assertEqual((first["source"], first["cached"], second["cached"]), ("tradefed", False, True))
            self.assertEqual(len(second["modules"]), 3)
            (pkg / "tools" / "version.txt").write_text("222\n", encoding="utf-8")  # new build: new list
            with patch.object(catalog, "_run_tradefed", side_effect=FileNotFoundError("no tool")):
                fb = catalog.list_modules(str(pkg), "cts", cache)
            self.assertEqual((fb["source"], [m["name"] for m in fb["modules"]]), ("testcases", ["CtsFoo"]))
            self.assertIn("no tool", fb["error"])


class PlanEditorTests(unittest.TestCase):
    def _app(self, tmp):
        from xts_agent.ui.server import UiApp, UiConfig

        root = Path(tmp)
        plans = root / "config" / "test_plans"
        plans.mkdir(parents=True)
        (root / "config" / "default_config.yaml").write_text("paths: {xts_packages_dir: /opt/xts}\n", encoding="utf-8")
        (plans / "builtin.yaml").write_text("name: Built in\nsuites:\n  - {name: cts, plan: cts}\n  - {name: vts, plan: vts}\n",
                                            encoding="utf-8")
        cfg = UiConfig(root=root, results_dir=root / "results", plans_dir=plans, db_path=None,
                       lease_dir=str(root / "leases"), defaults_path=str(root / "config" / "default_config.yaml"))
        return UiApp(cfg), plans

    def test_create_edit_protect_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, plans = self._app(tmp)
            data = {"name": "Car nightly", "profile": "development",
                    "suites": [{"name": "cts", "plan": "cts", "include_filters": ["CtsCarTestCases"]}]}
            out = app.save_plan({"file": "car_nightly", "data": data})
            self.assertEqual((out["file"], out["name"]), ("car_nightly.yaml", "Car nightly"))
            text = (plans / "car_nightly.yaml").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# created by xts-agent ui"))
            self.assertEqual(yaml.safe_load(text)["suites"][0]["include_filters"], ["CtsCarTestCases"])
            self.assertTrue(app.plan_source("car_nightly.yaml")["custom"])
            with self.assertRaises(RuntimeError):  # no silent overwrite
                app.save_plan({"file": "car_nightly", "data": data})
            data["description"] = "edited"
            app.save_plan({"file": "car_nightly", "data": data, "overwrite": True})
            self.assertEqual(yaml.safe_load((plans / "car_nightly.yaml").read_text(encoding="utf-8"))["description"], "edited")
            self.assertEqual((plans / "car_nightly.yaml").read_text(encoding="utf-8").count("created by xts-agent ui"), 1)
            # built-in plans cannot be overwritten or deleted from the console
            with self.assertRaises(RuntimeError):
                app.save_plan({"file": "builtin", "data": data, "overwrite": True})
            with self.assertRaises(RuntimeError):
                app.delete_plan("builtin.yaml")
            app.delete_plan("car_nightly.yaml")
            self.assertFalse((plans / "car_nightly.yaml").exists())

    def test_validation_rejects_bad_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = self._app(tmp)
            for bad_file in ("../escape", "Has Space", "UPPER", ""):
                with self.assertRaises(ValueError, msg=bad_file):
                    app.save_plan({"file": bad_file, "data": {"name": "x", "suites": [{"name": "cts"}]}})
            for text in ("just text", "name: x\nsuites: []\n", "name: x\nsuites:\n  - {name: nope}\n",
                         "name: x\nsuites:\n  - {name: cts, include_filters: ['a;rm -rf /']}\n", "name: [unclosed\n",
                         "name: x\nprofile: certification\nsuites:\n  - {name: cts, include_filters: [CtsFoo]}\n"):
                with self.assertRaises(ValueError, msg=text):
                    app.validate_plan(text)

    def test_launch_with_modules_for_one_suite_of_several(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = self._app(tmp)
            cmd, _, _ = app.build_launch({"plan": "builtin.yaml", "suite_modules": {"vts": ["VtsHalFooTargetTest"]}})
            derived = yaml.safe_load(Path(cmd[cmd.index("--plan") + 1]).read_text(encoding="utf-8"))
            cts, vts = derived["suites"]
            self.assertFalse(cts["enabled"])  # only suites with a selection run
            self.assertEqual(vts["include_filters"], ["VtsHalFooTargetTest"])
            self.assertIn("[1 module]", derived["name"])
            with self.assertRaises(ValueError):
                app.build_launch({"plan": "builtin.yaml", "suite_modules": {"sts": ["X"]}})


if __name__ == "__main__":
    unittest.main()


class FilterOrderTests(unittest.TestCase):
    def test_abi_is_moved_first(self):
        from xts_agent.ui.server import _clean_filters

        self.assertEqual(
            _clean_filters(["CtsBionicTestCases x86_64", "x86_64 CtsBionicTestCases", "CtsFoo a.B#c",
                            "CtsFoo arm64-v8a a.B#c", "CtsBionicTestCases"]),
            ["x86_64 CtsBionicTestCases", "x86_64 CtsBionicTestCases", "CtsFoo a.B#c",
             "arm64-v8a CtsFoo a.B#c", "CtsBionicTestCases"])
