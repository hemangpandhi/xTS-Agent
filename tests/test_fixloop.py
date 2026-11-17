"""Fix loop: catalog safety, loop state machine, OEM lookup, result reading."""

from __future__ import annotations

import json
import stat
from typing import Dict, List

import pytest

from xts_agent.fixloop import report
from xts_agent.fixloop.catalog import Remediation, RemediationCatalog, UnsafeCaptureError
from xts_agent.fixloop.device import DeviceSession
from xts_agent.fixloop.executor import FailureRecord, RunOutcome, TargetExecutor, TestTarget
from xts_agent.fixloop.loop import (
    FIXED,
    FLAKY,
    NEEDS_CODE_FIX,
    WORKAROUND_VERIFIED,
    FixLoop,
    run_queue,
)
from xts_agent.fixloop.oem_sources import OemCodeLocator, OemSourceConfig, search_terms
from xts_agent.fixloop.planner import Planner


# ---------------------------------------------------------------- fakes ---
class FakeDevice(DeviceSession):
    """Device whose shell answers come from a dict of substring -> output."""

    def __init__(self, answers: Dict[str, str]):
        self.commands: List[str] = []
        self.answers = answers
        super().__init__("emu-1", runner=self._fake, reconnect_wait_secs=0, boot_timeout_secs=0, poll_secs=0)

    def _fake(self, argv, timeout):
        cmd = " ".join(argv[3:]) if argv[1:3] == ["-s", "emu-1"] else " ".join(argv)
        self.commands.append(cmd)
        if "get-state" in cmd:
            return 0, "device"
        if "sys.boot_completed" in cmd:
            return 0, "1"
        for key, out in self.answers.items():
            if key in cmd:
                return 0, out
        return 0, ""


class ScriptedExecutor(TargetExecutor):
    def __init__(self, outcomes: List[RunOutcome], suite_path: str = "/nonexistent"):
        super().__init__(suite_path)
        self.outcomes = list(outcomes)
        self.calls = 0

    def run(self, target, serial):
        self.calls += 1
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]


def outcome(passed=0, failed=0, not_run=0, failures=(), setup=()):
    return RunOutcome(
        results_dir=f"/r/{passed}-{failed}-{not_run}", log_path="/l", return_code=0, duration_secs=1.0,
        passed=passed, failed=failed, not_run_modules=not_run, failures=list(failures), setup_errors=list(setup),
    )


def fail(msg, test="M c#t", kind="FAIL", stack=""):
    return FailureRecord(test, kind, msg, stack)


CATALOG_YAML = """
remediations:
  - id: host_aapt2_outdated
    title: aapt2
    category: INFRASTRUCTURE_FAILURE
    scope: host_tool
    match: ["AAPT_PARSER_FAILED"]
    tool: aapt2
    tool_candidates: ["{tooldir}"]
    verify_args: ["dump", "badging", "x.apk"]
  - id: device_connect_lab_wifi
    title: wifi
    category: ENVIRONMENT_ISSUE
    scope: device
    match: ["UnknownHostException"]
    detect: "cmd wifi status"
    broken_if: "Wifi is disabled"
    apply: ["cmd wifi connect-network {var.ssid} open"]
    revert: ["cmd wifi set-wifi-enabled disabled"]
  - id: dialer_role
    title: dialer role
    category: PRODUCT_BUG
    scope: device
    workaround: true
    permanent_fix: set config_defaultDialer
    match: ["getDefaultDialer"]
    apply: ["cmd role add-role-holder android.app.role.DIALER com.android.car.dialer"]
  - id: stop_app
    title: stop focus stealer
    category: ENVIRONMENT_ISSUE
    scope: device
    match: ["\\\\b([a-z][a-z0-9_]*(?:\\\\.[a-z0-9_]+){2,}) was focused instead"]
    apply: ["am force-stop {m1}"]
"""


@pytest.fixture
def catalog(tmp_path):
    tooldir = tmp_path / "bt"
    tooldir.mkdir()
    exe = tooldir / "aapt2"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    path = tmp_path / "rem.yaml"
    path.write_text(CATALOG_YAML.replace("{tooldir}", str(tooldir)))
    return RemediationCatalog.load(path)


def make_loop(catalog, outcomes, answers=None, provider=None, tmp_path=None, **kw):
    dev = FakeDevice(answers or {})
    ex = ScriptedExecutor(outcomes)
    loop = FixLoop(ex, dev, Planner(catalog, None, provider), out_dir=tmp_path, variables={"ssid": "LabAP"}, **kw)
    return loop, dev, ex


# -------------------------------------------------------------- catalog ---
def test_capture_must_look_like_a_package(catalog):
    rem = catalog.by_id["stop_app"]
    m = rem.find("com.google.android.car.kitchensink was focused instead")
    assert rem.render("am force-stop {m1}", m) == "am force-stop com.google.android.car.kitchensink"
    evil = Remediation(id="x", title="x", category="E", scope="device", match=[r"pkg=(\S+)"])
    with pytest.raises(UnsafeCaptureError):
        evil.render("am force-stop {m1}", evil.find("pkg=a;reboot"))


def test_lab_variables_required(catalog):
    rem = catalog.by_id["device_connect_lab_wifi"]
    assert rem.render(rem.apply[0], None, {"ssid": "LabAP"}) == "cmd wifi connect-network LabAP open"
    with pytest.raises(UnsafeCaptureError):
        rem.render(rem.apply[0], None, {})


def test_duplicate_ids_rejected():
    r = dict(title="t", category="E", scope="device", match=["x"])
    with pytest.raises(ValueError):
        RemediationCatalog([Remediation(id="a", **r), Remediation(id="a", **r)])


# ----------------------------------------------------------------- loop ---
def test_layered_issues_fixed_in_sequence(catalog, tmp_path):
    outcomes = [
        outcome(not_run=1, failures=[fail("TargetSetupError[AAPT_PARSER_FAILED]", "M", "MODULE_NOT_RUN")]),
        outcome(failed=2, failures=[fail("java.net.UnknownHostException: www.google.com")]),
        outcome(passed=2),
        outcome(passed=2),
    ]
    loop, dev, ex = make_loop(catalog, outcomes, {"cmd wifi status": "Wifi is disabled"}, tmp_path=tmp_path)
    s = loop.run(TestTarget("M", ["c#t"]))
    assert s.status == FIXED
    assert [a["id"] for a in s.applied] == ["host_aapt2_outdated", "device_connect_lab_wifi"]
    assert [i.kind for i in s.iterations] == ["baseline", "remediate", "remediate", "confirm"]
    assert s.iterations[1].verdict == "improved"
    assert ex.path_prepend and ex.path_prepend[0].endswith("bt")
    assert any("cmd wifi connect-network LabAP open" in c for c in dev.commands)
    saved = json.loads((tmp_path / "sessions" / f"{s.id}.json").read_text())
    assert saved["status"] == FIXED


def test_rule_skipped_when_device_probe_shows_ok(catalog, tmp_path):
    outcomes = [outcome(failed=1, failures=[fail("UnknownHostException")])] * 2
    loop, dev, _ = make_loop(catalog, outcomes, {"cmd wifi status": "Wifi is connected"}, tmp_path=tmp_path)
    s = loop.run(TestTarget("M"))
    assert s.status == NEEDS_CODE_FIX  # nothing applicable; rule default PRODUCT_BUG
    assert not any("connect-network" in c for c in dev.commands)
    assert s.iterations[0].diagnosis["skipped"][0]["id"] == "device_connect_lab_wifi"


def test_workaround_is_not_reported_as_fixed(catalog, tmp_path):
    outcomes = [outcome(failed=9, failures=[fail("IndexOutOfBounds", stack="at x.SelfManagedConnectionTest.getDefaultDialer(")]),
                outcome(passed=9), outcome(passed=9)]
    loop, _, _ = make_loop(catalog, outcomes, tmp_path=tmp_path)
    s = loop.run(TestTarget("CtsTelecomTestCases"))
    assert s.status == WORKAROUND_VERIFIED
    assert s.applied[0]["permanent_fix"] == "set config_defaultDialer"


def test_no_improvement_is_reverted(catalog, tmp_path):
    bad = outcome(failed=1, failures=[fail("UnknownHostException")])
    loop, dev, _ = make_loop(catalog, [bad, bad, bad], {"cmd wifi status": "Wifi is disabled"},
                             tmp_path=tmp_path, flaky_retries=0)
    s = loop.run(TestTarget("M"))
    assert s.iterations[1].verdict == "no_change"
    assert any("cmd wifi set-wifi-enabled disabled" in c for c in dev.commands)
    assert s.applied == []


def test_flaky_when_plain_rerun_passes(catalog, tmp_path):
    outcomes = [outcome(failed=1, failures=[fail("AssertionError: timing")]), outcome(passed=1), outcome(passed=1)]
    loop, _, _ = make_loop(catalog, outcomes, tmp_path=tmp_path)
    assert loop.run(TestTarget("M")).status == FLAKY


def test_pass_once_is_not_enough(catalog, tmp_path):
    outcomes = [outcome(failed=1, failures=[fail("UnknownHostException")]), outcome(passed=1),
                outcome(failed=1, failures=[fail("UnknownHostException")])]
    loop, _, _ = make_loop(catalog, outcomes, {"cmd wifi status": "Wifi is disabled"}, tmp_path=tmp_path,
                           max_iterations=4, flaky_retries=0)
    assert loop.run(TestTarget("M")).status != FIXED


class FakeLLM:
    model = "fake"

    def __init__(self, ids):
        self.ids = ids

    def generate(self, prompt, json_mode=False):
        if "List up to 4 identifiers" in prompt:
            return json.dumps({"terms": ["config_defaultDialer", "rm -rf /"]})
        return json.dumps({"classification": "PRODUCT_BUG", "confidence": 0.7, "root_cause": "no dialer role",
                           "remediation_ids": self.ids})


def test_llm_can_only_choose_catalog_ids(catalog, tmp_path):
    outcomes = [outcome(failed=1, failures=[fail("weird failure")])] * 3
    loop, dev, _ = make_loop(catalog, outcomes, provider=FakeLLM(["made_up", "dialer_role"]), tmp_path=tmp_path,
                             flaky_retries=0, max_iterations=2)
    s = loop.run(TestTarget("M"))
    rem = s.iterations[1].remediation
    assert rem["id"] == "dialer_role" and rem["source"] == "ai"
    assert all("made_up" not in c for c in dev.commands)


# ------------------------------------------------------------------ OEM ---
def test_local_oem_source_found_by_llm_term(tmp_path):
    overlay = tmp_path / "overlay" / "res" / "values"
    overlay.mkdir(parents=True)
    (overlay / "config.xml").write_text('<resources>\n  <string name="config_defaultDialer">com.oem.dialer</string>\n</resources>\n')
    loc = OemCodeLocator.from_config([{"name": "OemOverlay", "path": str(tmp_path / "overlay"), "packages": ["com.oem.dialer"]}])
    res = loc.locate("IndexOutOfBounds", "", extra_terms=["config_defaultDialer"])
    assert res["hits"][0]["file"].endswith("config.xml")
    assert res["hits"][0]["line"] == 2


def test_implicated_source_from_focused_package():
    loc = OemCodeLocator.from_config([
        {"name": "CarLauncher", "path": "/nonexistent", "packages": ["com.android.car.carlauncher"]},
        {"name": "CarSettings", "path": "/nonexistent", "packages": ["com.android.car.settings"]},
    ])
    assert loc.implicated("focus: com.android.car.carlauncher/.CarLauncher") == ["CarLauncher"]


def test_search_terms_skip_jdk_frames():
    t = ("at java.util.ArrayList.get(ArrayList.java:435)\n"
         "at android.telecom.cts.SelfManagedConnectionTest.getDefaultDialer(SelfManagedConnectionTest.java:703)")
    assert search_terms(t) == ["SelfManagedConnectionTest"]


# ------------------------------------------------------------- executor ---
RESULT_XML = """<?xml version='1.0' encoding='UTF-8'?>
<Result start="1" end="2" devices="emu-1">
  <Summary pass="1" failed="1" modules_done="1" modules_total="2" />
  <Module name="CtsBroken" abi="x86_64" runtime="0" done="false" pass="0" total_tests="0">
    <Reason message="TargetSetupError[AAPT_PARSER_FAILED|520050|DEPENDENCY_ISSUE]: AaptParser failed&#10;at x" />
  </Module>
  <Module name="CtsOs" abi="x86_64" runtime="5" done="true" pass="1" total_tests="2">
    <TestCase name="a.B">
      <Test result="pass" name="ok" />
      <Test result="fail" name="bad"><Failure message="boom"><StackTrace>boom at a.B</StackTrace></Failure></Test>
      <Test result="fail" name="other"><Failure message="not targeted" /></Test>
    </TestCase>
  </Module>
</Result>
"""


def test_module_not_run_and_filtered_tests(tmp_path):
    (tmp_path / "test_result.xml").write_text(RESULT_XML)
    ex = TargetExecutor("/nonexistent")
    out = ex._read_results(RunOutcome(str(tmp_path), "", 0, 1.0), tmp_path / "test_result.xml",
                           TestTarget("CtsBroken"))
    assert out.not_run_modules == 1 and "AAPT_PARSER_FAILED" in out.failures[0].message
    out = ex._read_results(RunOutcome(str(tmp_path), "", 0, 1.0), tmp_path / "test_result.xml",
                           TestTarget("CtsOs", ["a.B#ok", "a.B#bad"]))
    assert (out.passed, out.failed, out.green) == (1, 1, False)
    assert "x86_64 CtsOs a.B#other" not in out.tests


def test_target_parse_and_filters():
    t = TestTarget.parse("x86_64 CtsOsTestCases a.B#c a.D")
    assert t.include_filters() == ["x86_64 CtsOsTestCases a.B#c", "x86_64 CtsOsTestCases a.D"]
    assert TestTarget.parse("CtsFoo").include_filters() == ["CtsFoo"]


# --------------------------------------------------------------- report ---
def test_report_renders_sessions(catalog, tmp_path):
    outcomes = [outcome(failed=1, failures=[fail("UnknownHostException")]), outcome(passed=1), outcome(passed=1)]
    loop, _, _ = make_loop(catalog, outcomes, {"cmd wifi status": "Wifi is disabled"}, tmp_path=tmp_path)
    s = loop.run(TestTarget("M", ["c#t"]))
    sessions = run_queue(loop, [], max_rounds=1) + [s]
    html = report.render([x.to_dict() for x in sessions], tmp_path / "r.html").read_text()
    assert "Fixed" in html and "device_connect_lab_wifi" in html and "Real TradeFed runs" in html


# ------------------------------------------------------------- failover ---
class DeadDevice(FakeDevice):
    """Goes offline after the first TradeFed run and never comes back."""

    def __init__(self, serial="dead-1"):
        super().__init__({})
        self.serial = serial
        self.runs = 0

    def _fake(self, argv, timeout):
        cmd = " ".join(argv)
        if self.runs and ("get-state" in cmd or "sys.boot_completed" in cmd):
            return 1, "error: device offline"
        return super()._fake(argv, timeout)


class CountingExecutor(ScriptedExecutor):
    def __init__(self, outcomes, dead):
        super().__init__(outcomes)
        self.dead = dead
        self.serials = []

    def run(self, target, serial):
        self.serials.append(serial)
        if serial == self.dead.serial:
            self.dead.runs += 1
        return super().run(target, serial)


def test_lost_dut_moves_to_spare_and_rebaselines(catalog, tmp_path):
    from xts_agent.fixloop.loop import DevicePool

    dead = DeadDevice()
    spare = FakeDevice({"cmd wifi status": "Wifi is disabled"})
    junk = outcome(not_run=1, failures=[fail("device offline", "M", "MODULE_NOT_RUN")])
    ex = CountingExecutor([junk, outcome(failed=1, failures=[fail("UnknownHostException")]),
                           outcome(passed=1), outcome(passed=1)], dead)
    loop = FixLoop(ex, dead, Planner(catalog), out_dir=tmp_path, variables={"ssid": "LabAP"},
                   pool=DevicePool(["emu-1"], lambda s: spare))
    s = loop.run(TestTarget("M"))
    assert s.status == FIXED and s.serial == "emu-1"
    kinds = [(i.kind, i.verdict) for i in s.iterations]
    assert kinds[0] == ("baseline", "device_lost")  # junk result from the dying DUT is discarded
    assert ("failover", "failover") in kinds
    assert ex.serials == ["dead-1", "emu-1", "emu-1", "emu-1"]


def test_lost_dut_without_spare_ends_cleanly(catalog, tmp_path):
    from xts_agent.fixloop.loop import DEVICE_LOST

    dead = DeadDevice()
    ex = CountingExecutor([outcome(failed=1, failures=[fail("x")])], dead)
    loop = FixLoop(ex, dead, Planner(catalog), out_dir=tmp_path)
    s = loop.run(TestTarget("M"))
    assert s.status == DEVICE_LOST and "dead-1" in s.summary


def test_failed_confirmation_is_diagnosed_not_the_green_run(catalog, tmp_path):
    flaky_net = outcome(failed=1, failures=[fail("UnknownHostException")])
    outcomes = [flaky_net, outcome(passed=1), flaky_net, outcome(passed=1), outcome(passed=1)]
    loop, _, _ = make_loop(catalog, outcomes, {"cmd wifi status": "Wifi is disabled"}, tmp_path=tmp_path,
                           flaky_retries=1, max_iterations=6)
    s = loop.run(TestTarget("M"))
    kinds = [i.kind for i in s.iterations]
    assert kinds[:3] == ["baseline", "remediate", "confirm"]
    # The failed confirmation (not the green run) is what gets diagnosed next
    assert s.iterations[2].diagnosis is not None
    assert s.status == FLAKY


class CountingLLM(FakeLLM):
    def __init__(self):
        super().__init__(["dialer_role"])
        self.calls = 0

    def generate(self, prompt, json_mode=False):
        self.calls += 1
        return super().generate(prompt, json_mode)


def test_llm_skipped_for_confirmed_environment_fix_but_used_for_product_bug(catalog, tmp_path):
    llm = CountingLLM()
    loop, _, _ = make_loop(catalog, [outcome(failed=1, failures=[fail("UnknownHostException")]), outcome(passed=1)],
                           {"cmd wifi status": "Wifi is disabled"}, provider=llm, tmp_path=tmp_path)
    loop.run(TestTarget("M"))
    assert llm.calls == 0
    loop, _, _ = make_loop(catalog, [outcome(failed=1, failures=[fail("x", stack="t.getDefaultDialer(")]), outcome(passed=1)],
                           provider=llm, tmp_path=tmp_path)
    s = loop.run(TestTarget("M"))
    # one diagnosis call (no OEM sources configured here, so no search-term call)
    assert llm.calls == 1 and s.iterations[0].diagnosis["ai"]["root_cause"] == "no dialer role"


def test_ambient_launcher_does_not_crowd_out_overlay(tmp_path):
    launcher = tmp_path / "Launcher"
    (launcher / "res").mkdir(parents=True)
    (launcher / "Android.bp").write_text('android_app { name: "CarLauncher" }\n')
    (launcher / "res" / "dock.xml").write_text("<item>com.oem.dialer/.Main</item>\n")
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "config.xml").write_text('<string name="config_defaultDialer">com.oem.dialer</string>\n')
    loc = OemCodeLocator.from_config([
        {"name": "CarLauncher", "path": str(launcher), "packages": ["com.android.car.carlauncher"]},
        {"name": "Overlay", "path": str(overlay), "packages": []},
    ])
    res = loc.locate("IndexOutOfBounds at x.SelfManagedConnectionTest.getDefaultDialer(",
                     "top_activity: com.android.car.carlauncher/.CarLauncher", extra_terms=["com.oem.dialer"])
    assert res["implicated"] == []
    files = [h["file"] for h in res["hits"]]
    assert any(f.endswith("overlay/config.xml") for f in files)
    assert not any(f.endswith("Android.bp") for f in files)


def test_fallback_provider_skips_dead_server():
    from xts_agent.rca.llm_provider import FallbackProvider

    class Dead:
        model_id, model = "openai:coder", "coder"

        def generate(self, prompt, json_mode=False):
            return "Failed to generate RCA using coder: ConnectionError"

    class Alive:
        model_id, model = "openai:qwen", "qwen"

        def generate(self, prompt, json_mode=False):
            return '{"ok": true}'

    fb = FallbackProvider([Dead(), Alive()])
    assert fb.generate("x") == '{"ok": true}' and fb.model == "qwen"
    assert FallbackProvider([Dead()]).generate("x").startswith("Failed to generate")


def test_generic_terms_do_not_crowd_out_config(tmp_path):
    src = tmp_path / "oem"
    src.mkdir()
    (src / "A.java").write_text("void setUp() {}\n" * 3 + "String d = getDefaultDialer();\n" * 3)
    (src / "config.xml").write_text('<string name="config_defaultDialer">com.oem.dialer</string>\n')
    loc = OemCodeLocator.from_config([{"name": "Oem", "path": str(src)}])
    res = loc.locate("boom", extra_terms=["getDefaultDialer", "setUp", "config_defaultDialer"], max_hits=4)
    assert "setUp" not in res["terms"]
    assert any(h["file"].endswith("config.xml") for h in res["hits"])


def test_results_from_another_dut_are_rejected(tmp_path):
    (tmp_path / "test_result.xml").write_text(RESULT_XML)  # devices="emu-1"
    ex = TargetExecutor("/nonexistent")
    out = ex._read_results(RunOutcome(str(tmp_path), "", 0, 1.0), tmp_path / "test_result.xml",
                           TestTarget("CtsOs", ["a.B#ok"]), serial="emu-2")
    assert out.not_run_modules == 1 and "belongs to emu-1" in out.failures[0].message
    out = ex._read_results(RunOutcome(str(tmp_path), "", 0, 1.0), tmp_path / "test_result.xml",
                           TestTarget("CtsOs", ["a.B#ok"]), serial="emu-1")
    assert out.green


def test_ui_text_family_reaches_the_changed_sibling(tmp_path):
    res = tmp_path / "app/res"
    (res / "values").mkdir(parents=True)
    (res / "xml").mkdir()
    (res / "values/strings.xml").write_text(
        '<resources>\n<string name="mic_recently_accessed">Recently accessed</string>\n'
        '<string name="camera_recently_accessed">Recent camera activity</string>\n</resources>\n')
    (res / "xml/camera_fragment.xml").write_text('<Category android:title="@string/camera_recently_accessed"/>\n')
    loc = OemCodeLocator([OemSourceConfig(name="Settings", path=str(tmp_path / "app"))])
    got = loc.locate("AssertionError: Failed to find 'Recently accessed'.")
    assert "recently_accessed" in got["terms"]
    assert any(h["file"].endswith("camera_fragment.xml") for h in got["hits"])
