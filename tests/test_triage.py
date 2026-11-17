"""Failure signatures, history, known issues, ownership, Jira filing and group AI RCA."""

from __future__ import annotations

import logging
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

    def test_openai_compatible_on_prem_endpoint_needs_no_opt_in(self):
        from xts_agent.rca.llm_provider import OpenAICompatibleProvider, get_llm_provider

        for url in ("http://127.0.0.1:11434/v1", "http://localhost:8000/v1/", "http://10.1.2.3:8080/v1"):
            provider = get_llm_provider(self._cfg(
                provider="openai_compatible", openai_base_url=url, openai_model="qwen2.5-coder:32b"))
            self.assertIsInstance(provider, OpenAICompatibleProvider)
            self.assertEqual(provider.url, url.rstrip("/") + "/chat/completions")
            self.assertEqual(provider.model_id, "openai:qwen2.5-coder:32b")

    def test_openai_compatible_hosted_endpoint_refused_without_opt_in(self):
        from xts_agent.rca.llm_provider import ExternalProviderNotAllowed, get_llm_provider

        with self.assertRaises(ExternalProviderNotAllowed):
            get_llm_provider(self._cfg(
                provider="openai_compatible", openai_base_url="https://api.openai.com/v1",
                openai_model="gpt-4o-mini", openai_api_key="k"))

    def test_openai_compatible_bearer_key_in_header_json_mode_and_not_logged(self):
        import requests

        from xts_agent.rca.llm_provider import get_llm_provider

        provider = get_llm_provider(self._cfg(
            provider="openai_compatible", openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-4o-mini", openai_api_key="SECRET-KEY",
            allow_external_providers=True, request_timeout_secs=7))
        calls = []

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": '{"root_cause": "x"}'}}]}

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append((url, json, headers, timeout))
            return Resp()

        with patch.object(requests, "post", fake_post):
            with self.assertLogs("xts_agent.rca.llm_provider", level="DEBUG") as logs:
                logging.getLogger("xts_agent.rca.llm_provider").debug("probe")
                out = provider.generate("prompt", json_mode=True)
        self.assertEqual(out, '{"root_cause": "x"}')
        url, payload, headers, timeout = calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer SECRET-KEY")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["model"], "gpt-4o-mini")
        self.assertEqual(timeout, 7)
        self.assertNotIn("SECRET-KEY", "\n".join(logs.output))

    def test_openai_compatible_http_error_is_reported_not_raised(self):
        import requests

        from xts_agent.rca.llm_provider import get_llm_provider

        provider = get_llm_provider(self._cfg(
            provider="openai_compatible", openai_base_url="http://127.0.0.1:11434/v1", openai_model="m"))

        def fake_post(*a, **k):
            resp = MagicMock(status_code=503)
            raise requests.HTTPError(response=resp)

        with patch.object(requests, "post", fake_post):
            self.assertIn("HTTP 503", provider.generate("p"))

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

    def test_dead_llm_endpoint_trips_circuit_breaker(self):
        from xts_agent.triage.ai_rca import GroupAIAnalyzer

        names = [chr(ord("a") + i) for i in range(8)]
        report = JiraFilerTests._report(None, [(n, "NEW", False, "") for n in names])
        provider = MagicMock(model_id="openai:coder")
        provider.generate.return_value = "Failed to generate RCA using coder: ConnectionError"
        stats = GroupAIAnalyzer(provider, max_groups=8).analyze(report)
        # 3 consecutive failures, then the remaining 5 groups are skipped, not timed out one by one
        self.assertEqual(provider.generate.call_count, GroupAIAnalyzer.MAX_CONSECUTIVE_ERRORS)
        self.assertEqual((stats["provider_errors"], stats["skipped"], stats["unparseable"]), (3, 5, 0))
        self.assertTrue(all(g.ai is None for g in report.groups))
        provider.unload.assert_called_once()  # VRAM released even on abort

    def test_transient_llm_errors_do_not_trip_breaker(self):
        from xts_agent.triage.ai_rca import GroupAIAnalyzer

        report = JiraFilerTests._report(None, [(n, "NEW", False, "") for n in "abcde"])
        provider = MagicMock(model_id="openai:coder")
        ok = '{"root_cause": "r", "classification": "PRODUCT_BUG", "confidence": 0.8}'
        provider.generate.side_effect = ["Failed to generate: Timeout", ok, "Failed to generate: Timeout",
                                         "Failed to generate: Timeout", ok]
        stats = GroupAIAnalyzer(provider, max_groups=5).analyze(report)
        self.assertEqual((stats["analyzed"], stats["provider_errors"], stats["skipped"]), (2, 3, 0))

    def test_openai_fallbacks_build_a_provider_chain(self):
        from xts_agent.config_loader import AiRcaConfig
        from xts_agent.rca.llm_provider import (
            ExternalProviderNotAllowed,
            FallbackProvider,
            OpenAICompatibleProvider,
            get_llm_provider,
        )

        cfg = AiRcaConfig(
            provider="openai_compatible", openai_base_url="http://127.0.0.1:8011/v1", openai_model="coder",
            openai_extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            openai_fallbacks=[{"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5-coder:32b"}],
        )
        prov = get_llm_provider(cfg)
        self.assertIsInstance(prov, FallbackProvider)
        self.assertEqual([p.model for p in prov.providers], ["coder", "qwen2.5-coder:32b"])
        self.assertTrue(all(isinstance(p, OpenAICompatibleProvider) for p in prov.providers))
        self.assertEqual(prov.providers[0].extra_body, {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(prov.providers[1].extra_body, {})
        self.assertIsInstance(get_llm_provider(AiRcaConfig(provider="openai_compatible", openai_model="m")),
                              OpenAICompatibleProvider)
        # a hosted fallback is still refused without the opt-in
        cfg.openai_fallbacks = [{"base_url": "https://api.openai.com/v1", "model": "gpt"}]
        with self.assertRaises(ExternalProviderNotAllowed):
            get_llm_provider(cfg)

    def test_retrieved_source_is_in_prompt_and_recorded_as_code_refs(self):
        from xts_agent.triage.ai_rca import GroupAIAnalyzer

        report = JiraFilerTests._report(None, [("a", "NEW", False, "")])
        provider = MagicMock(model_id="m")
        provider.generate.return_value = '{"root_cause": "r", "classification": "PRODUCT_BUG", "confidence": 0.5}'
        report.groups[0].group.frames = ["util.PollingCheck.run", "util.WindowUtil.waitForFocus"]
        foo = {"file": "/aosp/frameworks/base/Foo.java", "start_line": 120, "text": "void waitForFocus() {}"}
        bar = {"file": "/aosp/cts/Bar.java", "start_line": 1, "text": "class Bar {}"}
        indexer = MagicMock()
        indexer.search_chunks.side_effect = lambda q, top_k=3, contains="": [foo] if contains else [foo, bar]
        GroupAIAnalyzer(provider, indexer=indexer).analyze(report)
        prompt = provider.generate.call_args_list[0][0][0]
        self.assertIn("File: /aosp/frameworks/base/Foo.java (line 120)", prompt)
        self.assertIn("void waitForFocus() {}", prompt)
        # frame method looked up first (not the generic "run"), then semantic matches, de-duplicated
        contains = [c.kwargs.get("contains", "") for c in indexer.search_chunks.call_args_list]
        self.assertEqual(contains, ["waitForFocus(", ""])
        self.assertEqual(report.groups[0].ai["code_refs"],
                         ["/aosp/frameworks/base/Foo.java:120", "/aosp/cts/Bar.java:1"])
        self.assertIn(report.groups[0].group.exception, indexer.search_chunks.call_args[0][0])

    def test_known_classified_groups_keep_eval_slots_when_cap_is_full(self):
        from xts_agent.triage.ai_rca import GroupAIAnalyzer

        report = JiraFilerTests._report(None, [
            ("a", "NEW", False, ""), ("b", "NEW", False, ""), ("c", "NEW", False, ""),
            ("k1", "NEW", True, ""), ("k2", "NEW", True, ""), ("k3", "NEW", True, ""),
        ])
        for tg in report.groups[3:]:
            tg.known.issue.classification = "ENVIRONMENT_ISSUE"
        provider = MagicMock(model_id="m")
        provider.generate.return_value = '{"root_cause": "r", "classification": "ENVIRONMENT_ISSUE", "confidence": 0.9}'
        GroupAIAnalyzer(provider, max_groups=2).analyze(report)
        analysed = [tg.group.signature for tg in report.groups if tg.ai]
        self.assertEqual(analysed, ["a", "b", "k1", "k2"])  # cap on actionable + 2 reserved known
        self.assertEqual(report.ai_agreement["evaluated"], 2)

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
        provider.generate.return_value = "Sure, here is my analysis: it is probably flaky."
        stats = GroupAIAnalyzer(provider).analyze(report)
        self.assertEqual((stats["unparseable"], stats["provider_errors"]), (1, 0))
        self.assertIsNone(report.groups[0].ai)
        # a provider error string is counted separately from a bad answer
        provider.generate.return_value = "Failed to generate RCA using llama.cpp: boom"
        stats = GroupAIAnalyzer(provider).analyze(JiraFilerTests._report(None, [("b", "NEW", False, "")]))
        self.assertEqual((stats["unparseable"], stats["provider_errors"]), (0, 1))

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

    def test_cross_group_contamination_correlates_ui_victims_but_preserves_product_bugs(self):
        from xts_agent.rca.failure_classifier import FailureClassifier
        from xts_agent.rca.rca_engine import FailureType, RCAEngine
        from xts_agent.results.result_parser import ModuleResult, TestCaseResult, TestResults

        hijacks = [
            TestCaseResult(
                class_name="android.app.cts.DialogTest",
                test_name=f"testFocus{i}",
                result="FAIL",
                message="Timed out waiting for activity; com.oem.overlay was focused instead",
                stack_trace="junit.framework.AssertionFailedError: com.oem.overlay was focused instead",
                module="CtsAppTestCases",
            )
            for i in range(12)
        ]
        ui_victim = TestCaseResult(
            class_name="android.view.inputmethod.cts.FocusHandlingTest",
            test_name="testKeyboardState",
            result="FAIL",
            message="java.lang.AssertionError",
            stack_trace="java.lang.AssertionError\n\tat android.view.inputmethod.cts.FocusHandlingTest.testKeyboardState(FocusHandlingTest.java:100)",
            module="CtsInputMethodTestCases",
        )
        real_bug = TestCaseResult(
            class_name="android.app.cts.SystemFeaturesTest",
            test_name="testCameraFeatures",
            result="FAIL",
            message="AssertionError: PackageManager#hasSystemFeature should NOT return true",
            stack_trace="java.lang.AssertionError\n\tat android.app.cts.SystemFeaturesTest.testCameraFeatures(SystemFeaturesTest.java:160)",
            module="CtsAppTestCases",
        )
        tr = TestResults(
            suite_name="CTS",
            device_info={},
            start_time="",
            end_time="",
            modules=[
                ModuleResult(
                    name="m",
                    done=True,
                    pass_count=0,
                    fail_count=14,
                    runtime=0.0,
                    test_cases=hijacks + [ui_victim, real_bug],
                )
            ],
        )
        report = RCAEngine(config=None, failure_classifier=FailureClassifier()).analyze_failures(tr)
        by_id = {f.test_id: f for f in report.failures}
        self.assertEqual(by_id[ui_victim.test_id].classification, FailureType.ENVIRONMENT_ISSUE)
        self.assertEqual(by_id[real_bug.test_id].classification, FailureType.PRODUCT_BUG)


if __name__ == "__main__":
    unittest.main()


class TriageEvalTests(unittest.TestCase):
    def _labels(self, tmp, entries):
        import yaml

        p = Path(tmp) / "labels.yaml"
        p.write_text(yaml.safe_dump({"issues": entries}), encoding="utf-8")
        return p

    def test_scores_rule_and_ai_against_held_out_labels(self):
        from xts_agent.triage.evaluation import evaluate
        from xts_agent.triage.known_issues import KnownIssueDB

        report = JiraFilerTests._report(None, [
            ("a", "NEW", False, ""), ("b", "NEW", False, ""), ("c", "NEW", False, ""), ("d", "NEW", False, ""),
        ])
        for tg, cls, ai in zip(report.groups, ["PRODUCT_BUG", "FLAKY_TEST", "FLAKY_TEST", "ENVIRONMENT_ISSUE"],
                               ["PRODUCT_BUG", "PRODUCT_BUG", None, "ENVIRONMENT_ISSUE"]):
            tg.classification = cls
            tg.ai = {"classification": ai, "confidence": 0.9} if ai else None
        with tempfile.TemporaryDirectory() as tmp:
            labels = KnownIssueDB.load(self._labels(tmp, [
                {"id": "LBL-1", "classification": "PRODUCT_BUG", "match": {"signature": "a"}},
                {"id": "LBL-2", "classification": "ENVIRONMENT_ISSUE", "match": {"signature": "b"}},
                {"id": "LBL-3", "classification": "FLAKY_TEST", "match": {"signature": "c"}},
                {"id": "LBL-9", "classification": "TEST_BUG", "match": {"signature": "nothing"}},
            ]))
        ev = evaluate(report, labels, labels_file="labels.yaml", known_issues=KnownIssueDB([]))
        self.assertTrue(ev.held_out)
        self.assertEqual((ev.groups_labelled, ev.groups_total, ev.unmatched_labels), (3, 4, ["LBL-9"]))
        self.assertEqual((ev.rule.scored, ev.rule.correct), (3, 2))        # b: FLAKY vs ENV wrong
        self.assertEqual((ev.ai.scored, ev.ai.correct), (2, 1))            # c not analysed: not scored
        self.assertEqual(ev.rule.per_class()["FLAKY_TEST"], {"support": 1, "precision": 0.5, "recall": 1.0, "f1": 0.667})
        self.assertEqual(ev.ai.confusion, {"PRODUCT_BUG": {"PRODUCT_BUG": 1}, "ENVIRONMENT_ISSUE": {"PRODUCT_BUG": 1}})
        text = ev.text()
        self.assertIn("rule  accuracy 67% (2/3", text)
        self.assertIn("ai    accuracy 50% (1/2", text)
        self.assertIn("disagreements", text)
        d = ev.to_dict()
        self.assertEqual(d["coverage"]["groups"], "3/4")

    def test_labels_that_are_also_known_issues_are_flagged_as_leaked(self):
        from xts_agent.triage.evaluation import evaluate
        from xts_agent.triage.known_issues import KnownIssueDB

        report = JiraFilerTests._report(None, [("a", "NEW", False, "")])
        report.groups[0].classification = "PRODUCT_BUG"
        with tempfile.TemporaryDirectory() as tmp:
            labels = KnownIssueDB.load(self._labels(tmp, [
                {"id": "KI-0001", "classification": "PRODUCT_BUG", "match": {"signature": "a"}},
            ]))
        ev = evaluate(report, labels, known_issues=labels)
        self.assertFalse(ev.held_out)
        self.assertEqual(ev.leaked_labels, ["KI-0001"])
        self.assertIn("not held out", ev.text())
