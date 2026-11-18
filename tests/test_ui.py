"""Execution UI: live state, history, result tables, server access controls, launch and stop."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.support import SAMPLE_XML


def _state(pid=None, phase="running", **extra):
    data = {
        "plan_name": "Cluster Smoke",
        "profile": "development",
        "started_at": 1000.0,
        "complete": phase == "done",
        "run_id": "ui-20261118-0001",
        "pid": pid,
        "phase": phase,
        "suites": {
            "cts": {
                "status": "RUNNING",
                "device_serials": ["0.0.0.0:6520", "0.0.0.0:6521"],
                "results_dir": "",
                "progress": {"modules_completed": 3, "modules_started": 5, "failures": 2,
                             "current": {"0.0.0.0:6520": "CtsBionicTestCases"}, "log": ""},
            }
        },
        "timeline": [{"at": 1000.0, "event": "started"}],
    }
    data.update(extra)
    return data


class Fixture:
    def __init__(self, tmp):
        self.root = Path(tmp)
        self.results = self.root / "results"
        self.state_dir = self.results / "run_state"
        (self.state_dir / "history").mkdir(parents=True)
        (self.results / "logs").mkdir()
        (self.results / "reports").mkdir()
        self.plans = self.root / "plans"
        self.plans.mkdir()
        (self.plans / "smoke.yaml").write_text(
            "name: Smoke\nprofile: certification\nsuites:\n  - name: cts\n    plan: cts\n    enabled: true\n",
            encoding="utf-8",
        )
        (self.plans / "two.yaml").write_text(
            "name: Two\nsuites:\n  - {name: cts, plan: cts}\n  - {name: vts, plan: vts}\n", encoding="utf-8"
        )

    def write_live(self, data, key="Cluster_Smoke"):
        (self.state_dir / f"{key}.json").write_text(json.dumps(data), encoding="utf-8")

    def config(self, **kw):
        from xts_agent.ui.server import UiConfig

        return UiConfig(root=self.root, results_dir=self.results, plans_dir=self.plans,
                        db_path=self.results / "none.db", lease_dir=str(self.root / "leases"), **kw)


class RunsViewTests(unittest.TestCase):
    def test_live_state_running_interrupted_and_done(self):
        from xts_agent.ui.data import RunsView

        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.write_live(_state(pid=4242))
            view = RunsView(fx.results)
            with patch("xts_agent.ui.data.agent_pid_alive", return_value=True):
                (run,) = view.live()
            self.assertEqual((run["state"], run["plan"], run["run_id"]), ("running", "Cluster Smoke", "ui-20261118-0001"))
            self.assertEqual(run["suites"][0]["progress"]["current"], {"0.0.0.0:6520": "CtsBionicTestCases"})
            with patch("xts_agent.ui.data.agent_pid_alive", return_value=False):
                self.assertEqual(view.live()[0]["state"], "interrupted")  # resumable, not silently "done"
            fx.write_live(_state(pid=4242, phase="done", overall_status="FAILED"))
            self.assertEqual(view.live()[0]["state"], "done")
            # addressable by run_id as well as by plan key
            with patch("xts_agent.ui.data.agent_pid_alive", return_value=True):
                self.assertEqual(view.get("ui-20261118-0001")["key"], "Cluster_Smoke")
            self.assertIsNone(view.get("../etc/passwd"))

    def test_history_rows_and_result_tables(self):
        from xts_agent.ui.data import RunsView

        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            rdir = fx.root / "tf" / "2026.11.18_00.00.00.000_1"
            rdir.mkdir(parents=True)
            (rdir / "test_result.xml").write_text(SAMPLE_XML, encoding="utf-8")
            done = _state(pid=1, phase="done", overall_status="FAILED",
                          totals={"pass": 2, "fail": 1, "skip": 2, "duration": 61},
                          reports=[str(fx.results / "reports" / "r.html")], triage_summary={"groups": 1})
            done["suites"]["cts"].update({"status": "FAILED", "results_dir": str(rdir)})
            (fx.state_dir / "history" / "ui-20261118-0001.json").write_text(json.dumps(done), encoding="utf-8")
            view = RunsView(fx.results)
            (row,) = view.history()
            self.assertEqual((row["status"], row["pass"], row["fail"], row["duration"], row["triage_groups"]),
                             ("FAILED", 2, 1, 61, 1))
            self.assertEqual(row["devices"], ["0.0.0.0:6520", "0.0.0.0:6521"])
            detail = view.detail("ui-20261118-0001")
            res = detail["suites"][0]["result"]
            (mod,) = res["modules"]
            self.assertEqual((mod["name"], mod["pass"], mod["fail"], mod["skip"], mod["total"], mod["done"]),
                             ("CtsSample", 2, 1, 2, 5, True))
            self.assertEqual(res["failed_tests"][0]["test"], "com.example.Foo#testC")
            self.assertIs(view.result_tables(str(rdir)), res)  # cached by mtime

    def test_tail_file_is_incremental(self):
        from xts_agent.ui.data import tail_file

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "tf.log"
            log.write_text("line1\nline2\n", encoding="utf-8")
            first = tail_file(log)
            self.assertEqual(first["text"], "line1\nline2\n")
            with open(log, "a", encoding="utf-8") as fh:
                fh.write("line3\n")
            self.assertEqual(tail_file(log, first["offset"])["text"], "line3\n")
            self.assertTrue(tail_file(Path(tmp) / "missing.log")["missing"])

    def test_list_plans(self):
        from xts_agent.ui.data import list_plans

        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            plans = {p["file"]: p for p in list_plans(fx.plans)}
            self.assertEqual(plans["smoke.yaml"]["profile"], "certification")
            self.assertEqual([s["name"] for s in plans["two.yaml"]["suites"]], ["cts", "vts"])


class UiAppTests(unittest.TestCase):
    def test_launch_validates_plan_and_modules_and_derives_a_plan(self):
        from xts_agent.ui.server import UiApp

        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            app = UiApp(fx.config())
            for bad in ({"plan": "../../etc/passwd"}, {"plan": "nope.yaml"}, {"plan": "smoke.yaml", "modules": ["Cts; rm -rf /"]},
                        {"plan": "two.yaml", "modules": ["CtsBionicTestCases"]}):
                with self.assertRaises(ValueError, msg=bad):
                    app.build_launch(bad)
            cmd, log, run_id = app.build_launch({"plan": "smoke.yaml", "auto_retry": True})
            self.assertEqual(cmd[1:5], ["-m", "xts_agent.cli", "run", "--plan"])
            self.assertEqual(Path(cmd[5]), (fx.plans / "smoke.yaml").resolve())
            self.assertIn("--auto-retry", cmd)
            self.assertTrue(run_id.startswith("ui-") and log.name == f"{run_id}.console.log")

            cmd, _, _ = app.build_launch({"plan": "smoke.yaml", "modules": ["CtsBionicTestCases",
                                                                            "CtsCarTestCases android.car.cts.CarTest#testX"],
                                          "ai_rca": False})
            import yaml

            derived = yaml.safe_load(Path(cmd[5]).read_text(encoding="utf-8"))
            self.assertTrue(str(Path(cmd[5])).startswith(str(fx.results / "ui_plans")))
            self.assertEqual(derived["suites"][0]["include_filters"],
                             ["CtsBionicTestCases", "CtsCarTestCases android.car.cts.CarTest#testX"])
            self.assertEqual(derived["profile"], "development")  # filtered certification is refused by design
            self.assertFalse(derived["ai_rca"]["enabled"])
            self.assertIn("[2 modules]", derived["name"])

    def test_launch_and_validate_shard_count_and_pinned_serials(self):
        import yaml

        from xts_agent.ui.data import list_plans
        from xts_agent.ui.server import UiApp

        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            (fx.plans / "cert6.yaml").write_text(
                "plan:\n  name: Cert6\nprofile: certification\ndevice_requirements:\n  min_devices: 6\n"
                "  reboot_between_suites: true\nsuites:\n  - name: cts\n    plan: cts\n    sharding:\n      shard_count: auto\n",
                encoding="utf-8",
            )
            app = UiApp(fx.config())
            for bad in (
                {"plan": "smoke.yaml", "shard_count": "banana"},
                {"plan": "smoke.yaml", "shard_count": 0},
                {"plan": "smoke.yaml", "shard_count": 99},
                {"plan": "smoke.yaml", "shard_count": True},
                {"plan": "smoke.yaml", "serials": ["0.0.0.0:6520; whoami"]},
                {"plan": "smoke.yaml", "serials": "0.0.0.0:6520"},
            ):
                with self.assertRaises(ValueError, msg=bad):
                    app.build_launch(bad)

            # Override shard_count to 1 on a min_devices=6 plan caps min_devices while keeping reboot_between_suites
            cmd, _, _ = app.build_launch({"plan": "cert6.yaml", "shard_count": 1})
            derived = yaml.safe_load(Path(cmd[5]).read_text(encoding="utf-8"))
            self.assertEqual(derived["devices"]["min_devices"], 1)
            self.assertTrue(derived["devices"]["reboot_between_suites"])
            self.assertEqual(derived["suites"][0]["sharding"]["shard_count"], 1)

            # Pinning specific serials without explicit shard_count sets serials and matches shard_count to len(serials)
            cmd2, _, _ = app.build_launch({"plan": "cert6.yaml", "serials": ["0.0.0.0:6523", "0.0.0.0:6521"]})
            derived2 = yaml.safe_load(Path(cmd2[5]).read_text(encoding="utf-8"))
            self.assertEqual(derived2["devices"]["serials"], ["0.0.0.0:6523", "0.0.0.0:6521"])
            self.assertEqual(derived2["devices"]["min_devices"], 2)
            self.assertEqual(derived2["suites"][0]["sharding"]["shard_count"], 2)

            # validate_plan accepts valid shard_count + devices.serials and rejects typos
            valid_yaml = (
                "name: pinned\ndevices:\n  min_devices: 1\n  serials: ['0.0.0.0:6520']\n"
                "suites:\n- name: cts\n  plan: cts\n  sharding:\n    shard_count: 1\n"
            )
            self.assertEqual(app.validate_plan(valid_yaml)["name"], "pinned")
            with self.assertRaises(ValueError):
                app.validate_plan("name: bad\nsuites:\n- name: cts\n  sharding: {shard_count: nope}\n")
            with self.assertRaises(ValueError):
                app.validate_plan("name: bad\ndevices: {serials: ['bad serial']}\nsuites:\n- name: cts\n")

            (fx.plans / "pinned.yaml").write_text(valid_yaml, encoding="utf-8")
            plans = {p["file"]: p for p in list_plans(fx.plans)}
            self.assertEqual(plans["pinned.yaml"]["serials"], ["0.0.0.0:6520"])
            self.assertEqual(plans["cert6.yaml"]["min_devices"], 6)

    def test_launch_spawns_detached_with_run_id_and_refuses_duplicates(self):
        from xts_agent.ui.server import UiApp

        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            app = UiApp(fx.config())
            with patch("xts_agent.ui.server.subprocess.Popen") as popen:
                popen.return_value = MagicMock(pid=999)
                out = app.launch({"plan": "smoke.yaml"})
            kwargs = popen.call_args.kwargs
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["env"]["XTS_RUN_ID"], out["run_id"])
            self.assertEqual(kwargs["cwd"], str(fx.root))
            fx.write_live(_state(pid=4242, plan_name="Smoke"), key="Smoke")
            with patch("xts_agent.ui.data.agent_pid_alive", return_value=True), self.assertRaises(RuntimeError):
                app.launch({"plan": "smoke.yaml"})

    def test_stop_only_signals_a_live_agent(self):
        from xts_agent.ui.server import UiApp

        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            app = UiApp(fx.config())
            with self.assertRaises(LookupError):
                app.stop("Nope")
            fx.write_live(_state(pid=4242))
            with patch("xts_agent.ui.server.agent_pid_alive", return_value=False), \
                    patch("xts_agent.ui.server.os.kill") as kill, self.assertRaises(RuntimeError):
                app.stop("Cluster_Smoke")
            kill.assert_not_called()
            with patch("xts_agent.ui.server.agent_pid_alive", return_value=True), \
                    patch("xts_agent.ui.server.os.kill") as kill:
                app.stop("Cluster_Smoke")
            kill.assert_called_once()
            self.assertEqual(kill.call_args[0][0], 4242)
            fx.write_live(_state(pid=4242, phase="done"))
            with patch("xts_agent.ui.server.agent_pid_alive", return_value=True), \
                    patch("xts_agent.ui.server.os.kill") as kill, self.assertRaises(RuntimeError):
                app.stop("Cluster_Smoke")


class ServerTests(unittest.TestCase):
    def _serve(self, fx, **kw):
        from xts_agent.ui.server import make_server

        cfg = fx.config(port=0, **kw)
        server = make_server(cfg)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def _req(self, url, method="GET", body=None, headers=None):
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def test_pages_api_files_and_csrf(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.write_live(_state(pid=4242))
            (fx.results / "reports" / "r.html").write_text("<p>report</p>", encoding="utf-8")
            (fx.results / "xts_agent.db").write_text("secret", encoding="utf-8")
            (fx.root / "outside.html").write_text("no", encoding="utf-8")
            base = self._serve(fx)
            status, body, headers = self._req(base + "/")
            self.assertEqual(status, 200)
            self.assertIn(b"xTS Agent", body)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            status, body, _ = self._req(base + "/api/live")
            self.assertEqual((status, json.loads(body)[0]["plan"]), (200, "Cluster Smoke"))
            status, _, headers = self._req(base + "/files/reports/r.html")
            self.assertEqual(status, 200)
            self.assertIn("sandbox", headers["Content-Security-Policy"])
            for path in ("/files/xts_agent.db", "/files/../outside.html", "/files/%2e%2e/outside.html",
                         "/files/run_state/../../outside.html"):
                self.assertEqual(self._req(base + path)[0], 404, path)
            # state-changing calls need the custom header (cross-site forms cannot send it)
            self.assertEqual(self._req(base + "/api/launch", "POST", {"plan": "smoke.yaml"})[0], 403)
            status, body, _ = self._req(base + "/api/launch", "POST", {"plan": "../x.yaml"}, {"X-XTS-UI": "1"})
            self.assertEqual(status, 400)
            # DNS rebinding: a foreign Host on a loopback-bound console is refused
            self.assertEqual(self._req(base + "/api/live", headers={"Host": "evil.example:80"})[0], 421)

    def test_token_mode(self):
        from xts_agent.ui.server import make_server

        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            with self.assertRaises(ValueError):
                make_server(fx.config(host="0.0.0.0", port=0))  # no token, not loopback
            base = self._serve(fx, token="s3cret")
            self.assertEqual(self._req(base + "/api/live")[0], 401)
            self.assertEqual(self._req(base + "/api/live", headers={"Authorization": "Bearer nope"})[0], 401)
            self.assertEqual(self._req(base + "/api/live", headers={"Authorization": "Bearer s3cret"})[0], 200)
            self.assertEqual(self._req(base + "/api/live?t=s3cret")[0], 200)
            self.assertEqual(self._req(base + "/")[0], 200)  # the page itself carries no data


class LiveStatusTests(unittest.TestCase):
    def test_run_state_process_phase_retry_and_archive(self):
        from xts_agent.execution.run_state import RunState

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XTS_RUN_ID": "ui-test-1"}):
            import xts_agent.utils.logger as lg

            with patch.object(lg, "_run_id", None):
                rs = RunState.for_plan(tmp, "Cluster Smoke")
                rs.start("Cluster Smoke", "development")
                rs.attach_process(auto_retry=True, resumed=False)
                rs.suite_started("cts", [], "fp", ["s1"])
                rs.suite_retrying("cts", 1)
                rs.set_phase("done", overall_status="PASSED")
                archived = rs.archive()
            data = json.loads(rs.path.read_text(encoding="utf-8"))
            self.assertEqual((data["pid"], data["run_id"], data["phase"]), (os.getpid(), "ui-test-1", "done"))
            self.assertEqual(data["suites"]["cts"]["status"], "RETRYING")
            self.assertEqual([t["event"] for t in data["timeline"]], ["started", "cts retry 1", "done"])
            self.assertEqual(archived, Path(tmp) / "run_state" / "history" / "ui-test-1.json")

    def test_status_heartbeat_is_faster_than_the_log_line(self):
        from xts_agent.execution.progress import ProgressMonitor

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "tf.log"
            log.write_text("10-04 16:20:03 I/ITestSuite: s1 running 1 modules: [x86_64 CtsFoo]\n", encoding="utf-8")
            updates = []
            mon = ProgressMonitor(log, interval_secs=600, stall_after_secs=0, on_update=lambda p: updates.append(p.as_dict()),
                                  status_interval_secs=0.05)
            with self.assertNoLogs("xts_agent.execution.progress", "INFO") if hasattr(self, "assertNoLogs") else _null():
                mon.start()
                time.sleep(0.4)
                mon.stop()
            self.assertGreaterEqual(len(updates), 3)
            self.assertEqual(updates[0]["log"], str(log))  # published before the first tick
            self.assertEqual(updates[-1]["current"], {"s1": "CtsFoo"})


class InteractiveTriageTests(unittest.TestCase):
    TWO_SAME_ROOT_CAUSE_XML = """<?xml version='1.0' encoding='UTF-8'?>
<Result start_display="now" end_display="later" suite_name="CTS">
  <Build build_fingerprint="generic/cf_auto:16/B/1:userdebug/test-keys" device_serial="0.0.0.0:6520" />
  <Summary pass="10" failed="2" skipped="0" modules_done="1" modules_total="1" />
  <Module name="CtsCarTestCases" abi="x86_64" done="true" pass="10" fail="2" runtime="100">
    <TestCase name="com.example.car.FooTest">
      <Test result="fail" name="testOne">
        <Failure message="java.lang.IllegalStateException: Rotary IME missing 1">
          <StackTrace>java.lang.IllegalStateException: Rotary IME missing 1
\tat com.example.car.RotaryHelper.assertReady(FooTest.java:10)
\tat com.example.car.FooTest.testOne(FooTest.java:18)</StackTrace>
        </Failure>
      </Test>
      <Test result="fail" name="testTwo">
        <Failure message="java.lang.IllegalStateException: Rotary IME missing 2">
          <StackTrace>java.lang.IllegalStateException: Rotary IME missing 2
\tat com.example.car.RotaryHelper.assertReady(FooTest.java:10)
\tat com.example.car.FooTest.testTwo(FooTest.java:24)</StackTrace>
        </Failure>
      </Test>
    </TestCase>
  </Module>
</Result>
"""

    def test_deterministic_grouping_discussion_ai_verdict_and_known_issue_promotion(self):
        from xts_agent.ui.server import UiApp

        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            cfg_dir = fx.root / "config"
            cfg_dir.mkdir()
            src_dir = fx.root / "src" / "com" / "example" / "car"
            src_dir.mkdir(parents=True)
            java_file = src_dir / "FooTest.java"
            java_file.write_text(
                "\n".join([f"// line {i}" for i in range(1, 9)] + [
                    "class RotaryHelper {",
                    "  static void assertReady() { throw new IllegalStateException(\"Rotary IME missing\"); }",
                    "}",
                ] + [f"// tail {i}" for i in range(12, 30)]),
                encoding="utf-8",
            )
            defaults_path = cfg_dir / "default_config.yaml"
            defaults_path.write_text(
                f"triage:\n  known_issues_file: {cfg_dir / 'known_issues.yaml'}\n"
                f"ai_rca:\n  provider: openai_compatible\n  openai_base_url: http://127.0.0.1:11434/v1\n"
                f"  openai_model: coder\n  source_code_paths:\n    - {fx.root / 'src'}\n",
                encoding="utf-8",
            )

            rdir = fx.root / "tf" / "2026.11.18_01.00.00.000_1"
            rdir.mkdir(parents=True)
            (rdir / "test_result.xml").write_text(self.TWO_SAME_ROOT_CAUSE_XML, encoding="utf-8")
            (rdir / "device_logcat_test_1.txt").write_text(
                "11-18 01:00:01 I/RotaryService: no touch IME configured\n"
                "11-18 01:00:02 I/TestRunner: failed: testOne(com.example.car.FooTest)\n"
                "11-18 01:00:02 E/RotaryHelper: missing IME\n"
                "11-18 01:00:03 I/TestRunner: finished: testOne(com.example.car.FooTest)\n",
                encoding="utf-8",
            )

            # Run 1 archived WITH NO triage_*.json: Level-1 deterministic grouping runs on the fly
            done1 = _state(pid=1, phase="done", overall_status="FAILED",
                           totals={"pass": 10, "fail": 2, "skip": 0, "duration": 30})
            done1["run_id"] = "ui-20261118-0100"
            done1["suites"]["cts"].update({"status": "FAILED", "results_dir": str(rdir)})
            (fx.state_dir / "history" / "ui-20261118-0100.json").write_text(json.dumps(done1), encoding="utf-8")

            # Clean passing run (newer) — default_key must still pick the failing run
            clean = _state(pid=2, phase="done", overall_status="PASSED", started_at=2000.0,
                           totals={"pass": 9, "fail": 0, "skip": 0, "duration": 10})
            clean["run_id"] = "ui-20261118-0200"
            (fx.state_dir / "history" / "ui-20261118-0200.json").write_text(json.dumps(clean), encoding="utf-8")

            app = UiApp(fx.config(defaults_path=str(defaults_path)))
            runs_meta = app.triage_runs()
            self.assertEqual(runs_meta["default_key"], "ui-20261118-0100")

            tr = app.triage_run("ui-20261118-0100")
            self.assertEqual(tr["triage_source"], "computed_deterministic")
            self.assertEqual(tr["summary"]["failures"], 2)
            self.assertEqual(tr["summary"]["groups"], 1)  # 2 tests collapsed into 1 root cause!
            sig = tr["groups"][0]["signature"]

            gd = app.triage_group("ui-20261118-0100", sig)
            self.assertIn("RotaryService: no touch IME", gd["logcat"])
            self.assertTrue(any("FooTest.java" in s["file"] for s in gd["snippets"]))

            # 1) Offline human note works without LLM
            note_res = app.triage_discuss("ui-20261118-0100", sig, {"message": "Reproduced on CVD #1", "mode": "note", "author": "alice"})
            self.assertEqual(len(note_res["messages"]), 1)
            self.assertEqual(note_res["messages"][0]["author"], "alice")

            # 2) Failed LLM raises RuntimeError without adding orphan question to DB
            fake_bad = MagicMock(model_id="openai:coder")
            fake_bad.generate.return_value = "Failed to generate RCA: ConnectionError"
            with patch("xts_agent.triage.discussion.get_llm_provider", return_value=fake_bad), \
                    self.assertRaises(RuntimeError):
                app.triage_discuss("ui-20261118-0100", sig, {"message": "Why did this fail?", "mode": "ask"})
            self.assertEqual(len(app.triage_group("ui-20261118-0100", sig)["messages"]), 1)

            # 3) On-demand structured AI RCA + interactive multi-turn Ask AI
            fake_llm = MagicMock(model_id="openai:coder")
            fake_llm.generate.side_effect = [
                json.dumps({
                    "root_cause": "Touch IME missing on Cuttlefish auto build.",
                    "classification": "ENVIRONMENT_ISSUE",
                    "confidence": 0.92,
                    "suggested_fix": "Add CarLatinIME to product packages.",
                    "evidence": "FooTest.java:10 & RotaryService logcat",
                }),
                "Check `device/google/cuttlefish/shared/auto/device_vendor.mk` for `CarLatinIME`.",
            ]
            with patch("xts_agent.triage.discussion.get_llm_provider", return_value=fake_llm):
                ai_res = app.triage_analyze("ui-20261118-0100", sig)
                self.assertEqual(ai_res["ai"]["classification"], "ENVIRONMENT_ISSUE")
                chat_res = app.triage_discuss("ui-20261118-0100", sig,
                                              {"message": "Which makefile enables it?", "mode": "ask", "author": "bob"})
            self.assertIn("CarLatinIME", chat_res["messages"][-1]["content"])
            # Prior note ("Reproduced on CVD #1") + source snippet ("FooTest.java") were grounded in the prompt
            sent_prompt = fake_llm.generate.call_args_list[-1][0][0]
            self.assertIn("Reproduced on CVD #1", sent_prompt)
            self.assertIn("FooTest.java", sent_prompt)

            # 4) Second run with same signature inherits discussion & cached AI, then promotes to known_issues.yaml
            done2 = _state(pid=3, phase="done", overall_status="FAILED", started_at=3000.0,
                           totals={"pass": 10, "fail": 2, "skip": 0, "duration": 25})
            done2["run_id"] = "ui-20261118-0300"
            done2["suites"]["cts"].update({"status": "FAILED", "results_dir": str(rdir)})
            (fx.state_dir / "history" / "ui-20261118-0300.json").write_text(json.dumps(done2), encoding="utf-8")

            tr2 = app.triage_run("ui-20261118-0300")
            self.assertEqual(tr2["groups"][0]["ai"]["classification"], "ENVIRONMENT_ISSUE")
            self.assertGreaterEqual(tr2["groups"][0]["discussion_count"], 4)

            ki_res = app.triage_known_issue({
                "signature": sig,
                "run_key": "ui-20261118-0300",
                "title": "Cuttlefish Auto missing default touch IME",
                "classification": "ENVIRONMENT_ISSUE",
                "jira": "AAOS-404",
                "waive": True,
                "waiver_expires": "2027-12-31",
                "waiver_reason": "AOSP Cuttlefish lab image",
                "author": "bob",
            })
            self.assertEqual(ki_res["issue_id"], "KI-0001")

            # Reloading triage now deterministically matches KI-0001 and marks it waived & non-actionable!
            tr3 = app.triage_run("ui-20261118-0300")
            g3 = tr3["groups"][0]
            self.assertEqual(g3["known_issue"]["id"], "KI-0001")
            self.assertTrue(g3["known_issue"]["waived"])
            self.assertFalse(g3["actionable"])
            self.assertEqual(g3["effective_classification"], "ENVIRONMENT_ISSUE")

            # 5) Official Google TradeFed report links (HTML, XML, submission .zip) & path resolution
            (rdir / "test_result_failures_suite.html").write_text("<html>Google CTS</html>", encoding="utf-8")
            rdir.with_name(rdir.name + ".zip").write_bytes(b"PK\x03\x04")
            detail = app.decorate(app.runs.detail("ui-20261118-0300"))
            (g_link,) = detail["links"]["google_reports"]
            self.assertEqual(g_link["html_url"], "/tf-results/ui-20261118-0300/cts/test_result_failures_suite.html")
            self.assertEqual(g_link["xml_url"], "/tf-results/ui-20261118-0300/cts/test_result.xml")
            self.assertEqual(g_link["zip_url"], "/tf-zip/ui-20261118-0300/cts")
            self.assertEqual(app.resolve_tf_suite_dir("ui-20261118-0300", "cts"), rdir.resolve())
            self.assertIsNone(app.resolve_tf_suite_dir("ui-20261118-0300", "../etc"))
            self.assertEqual(app.triage_run("ui-20261118-0300")["google_reports"][0]["primary_url"], g_link["html_url"])

            # 6) Optional AI Disabled mode ("provider: none") keeps deterministic triage & notes working
            app.save_settings({"ai_rca": {"provider": "none"}})
            tr_none = app.triage_run("ui-20261118-0300")
            self.assertEqual(tr_none["ai_configured"]["provider"], "none")
            self.assertFalse(tr_none["ai_configured"]["enabled"])
            note_res = app.triage_discuss("ui-20261118-0300", sig, {"message": "Offline note", "mode": "note", "author": "qa"})
            self.assertEqual(note_res["messages"][-1]["content"], "Offline note")
            with self.assertRaisesRegex(RuntimeError, "Disabled in Settings"):
                app.triage_analyze("ui-20261118-0300", sig, force=True)


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    unittest.main()
