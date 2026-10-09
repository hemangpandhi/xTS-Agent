"""HTML/JSON/JUnit reports, dashboard and the analyze/report CLI paths."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests import support
from tests.support import SAMPLE_XML
from xts_agent.config_loader import ConfigLoader
from xts_agent.execution.test_plan_executor import PlanResult, SuiteResult
from xts_agent.results.result_parser import ResultParser

setUpModule = support.setUpModule
tearDownModule = support.tearDownModule


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


class DashboardTests(unittest.TestCase):
    def test_collect_and_render_offline(self):
        import datetime as dt
        import re

        from xts_agent.reporting.dashboard import collect, render
        from xts_agent.results.result_store import ResultStore
        from xts_agent.triage.known_issues import KnownIssue, KnownIssueDB, Waiver

        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp) / "db.sqlite")
            for fails, status in ((10, "FAILED"), (0, "PASSED")):
                store.save_suite_run("p", "certification", SuiteResult(
                    "cts", status, 90, fails, 0, 3600.0, 1, "", 0, device_serials=["a"]))
            soon = dt.date.today() + dt.timedelta(days=3)
            ki = KnownIssueDB([KnownIssue("KI-9", "x <script>", waiver=Waiver("r", soon))])
            ledger = MagicMock(quarantined=MagicMock(return_value={"hu-1": "3 consecutive failures"}))
            data = collect(store, known_issues=ki, ledger=ledger)
            html_text = render(data)
        self.assertEqual([r["fails"] for r in data["suites"]["CTS"]], [10, 0])  # oldest first
        self.assertEqual(data["waivers"][0]["days_left"], 3)
        self.assertEqual(re.findall(r"https?://", html_text), [])  # no CDN: works offline
        self.assertIn("<svg", html_text)
        self.assertIn("class='warn'", html_text)  # waiver expiring soon
        self.assertIn("hu-1", html_text)
        self.assertNotIn("<script>", html_text)  # escaped


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
            html = Path(written["html"]).read_text(encoding="utf-8")
        # Must render with no network: no external scripts, styles or fonts
        self.assertNotRegex(html, r"(?:src|href)=[\"']?(?:https?:)?//")
        self.assertNotIn("<script", html)
        self.assertIn("50.0%", html)  # pass rate = pass / (pass + fail)
        self.assertIn("boom", html)  # why the suite failed is visible, not only a tooltip

    def test_html_report_escapes_test_output(self):
        from xts_agent.reporting.html_report import HTMLReportGenerator

        evil = SuiteResult("cts", "FAILED", 0, 1, 0, 1.0, None, "", 0, error_message="<script>x()</script>")
        plan_result = PlanResult("p", {"cts": evil}, 0, 1, 0, 1.0, "FAILED")
        with tempfile.TemporaryDirectory() as tmp:
            html = HTMLReportGenerator().generate(plan_result, None, None, Path(tmp) / "r.html").read_text()
        self.assertNotIn("<script>x()", html)
        self.assertIn("&lt;script&gt;x()", html)

    def test_imported_results_take_duration_and_devices_from_xml(self):
        from xts_agent.orchestrator import Orchestrator

        xml_text = SAMPLE_XML.replace(
            'suite_name="CTS"', 'suite_name="CTS" start="1000000" end="4600000" devices="s1,s2"'
        )
        with tempfile.TemporaryDirectory() as tmp:
            rdir = Path(tmp) / "2026.10.05_21.11.12.345_5909"
            rdir.mkdir()
            (rdir / "test_result.xml").write_text(xml_text, encoding="utf-8")
            orch = Orchestrator("config/test_plans/smoke_test.yaml")
            orch.plan = ConfigLoader("config/test_plans/smoke_test.yaml").load_plan()
            result = orch.plan_result_from_results_dirs("CTS", [str(rdir)])
        self.assertEqual(result.duration, 3600.0)
        self.assertEqual(result.device_serials, ["s1", "s2"])
        self.assertEqual(result.suites_results["CTS"].device_serials, ["s1", "s2"])


if __name__ == "__main__":
    unittest.main()
