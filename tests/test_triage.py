"""Failure signatures, history, known issues, ownership, Jira filing and group AI RCA."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests import support
from xts_agent.execution.test_plan_executor import PlanResult, SuiteResult
from xts_agent.results.result_parser import ResultParser

setUpModule = support.setUpModule
tearDownModule = support.tearDownModule


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


if __name__ == "__main__":
    unittest.main()
