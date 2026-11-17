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


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    unittest.main()
