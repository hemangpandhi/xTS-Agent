"""Cancellation, logging, TradeFed heartbeat, Prometheus metrics, disk retention."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from tests import support
from tests.support import INCOMPLETE_XML, PASSING_XML
from xts_agent.config_loader import SuiteConfig
from xts_agent.execution.test_plan_executor import PlanResult, SuiteResult
from xts_agent.execution.tradefed_runner import TradefedRunner

CANCEL_DRIVER = r"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

from xts_agent.config_loader import DeviceRequirements, PathsConfig, SuiteConfig, TestPlanConfig
from xts_agent.execution import cancel
from xts_agent.execution.run_state import RunState
from xts_agent.execution.shard_manager import ShardManager
from xts_agent.execution.test_plan_executor import TestPlanExecutor

tmp = Path(sys.argv[1])
suites = [
    SuiteConfig(name=n, plan=n, priority=i, package_path=str(tmp / f"android-{n}"))
    for i, n in enumerate(("cts", "vts"))
]
plan = TestPlanConfig(name="cert", suites=suites, devices=DeviceRequirements(min_devices=1),
                      paths=PathsConfig())
device = MagicMock(serial="hu-1", device_type="aaos", build_fingerprint="fp")
dm = MagicMock()
dm.get_available_devices.return_value = [device]
dm.select_shard_pool.return_value = [device]
dm.allocate_devices.return_value = [device]
dm.lease_fds.return_value = []
state = RunState.for_plan(tmp / "results", "cert")
executor = TestPlanExecutor(plan, dm, ShardManager(dm), MagicMock(), MagicMock(),
                            results_dir=tmp / "results", run_state=state)
cancel.install_signal_handlers()
result = executor.execute_plan(plan, auto_retry=True)
print({n: s.status for n, s in result.suites_results.items()}, result.cancelled)
sys.exit(cancel.exit_code() if result.cancelled else 0)
"""


# Fake TradeFed: on SIGTERM flushes a partial result (as TradeFed does) and exits
CANCEL_TRADEFED = """#!/bin/sh
[ "$1" = list ] && exit 0
here=$(cd "$(dirname "$0")/.." && pwd)
flush() {
  mkdir -p "$here/results/2026.10.07_05.00.00.000_1"
  cp "$here/partial.xml" "$here/results/2026.10.07_05.00.00.000_1/test_result.xml"
  exit 143
}
trap flush TERM
echo $$ > "$here/started"
while true; do sleep 1; done
"""

setUpModule = support.setUpModule
tearDownModule = support.tearDownModule


class CancelTests(unittest.TestCase):
    def tearDown(self):
        from xts_agent.execution import cancel

        cancel.reset()

    def test_sigterm_stops_tradefed_and_keeps_run_resumable(self):
        import json
        import os
        import signal
        import subprocess
        import sys
        import time

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name in ("cts", "vts"):
                tools = tmp / f"android-{name}" / "tools"
                tools.mkdir(parents=True)
                script = tools / f"{name}-tradefed"
                script.write_text(CANCEL_TRADEFED, encoding="utf-8")
                script.chmod(0o755)
                (tmp / f"android-{name}" / "partial.xml").write_text(INCOMPLETE_XML, encoding="utf-8")
            driver = tmp / "driver.py"
            driver.write_text(CANCEL_DRIVER, encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
            agent = subprocess.Popen(
                [sys.executable, str(driver), str(tmp)], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            started = tmp / "android-cts" / "started"
            try:
                deadline = time.time() + 30
                while not started.exists() and time.time() < deadline:
                    time.sleep(0.1)
                self.assertTrue(started.exists(), "fake TradeFed never started")
                tf_pid = int(started.read_text())

                agent.send_signal(signal.SIGTERM)
                out, _ = agent.communicate(timeout=60)
            finally:
                if agent.poll() is None:
                    agent.kill()

            self.assertEqual(agent.returncode, 143, out)
            self.assertIn("{'cts': 'INCOMPLETE', 'vts': 'INCOMPLETE'} True", out)
            with self.assertRaises(ProcessLookupError):
                os.kill(tf_pid, 0)  # TradeFed did not outlive the agent
            self.assertFalse((tmp / "android-vts" / "started").exists())  # VTS never started
            state = json.loads((tmp / "results" / "run_state" / "cert.json").read_text())
            self.assertFalse(state["complete"])
            cts = state["suites"]["cts"]
            self.assertEqual(cts["status"], "INCOMPLETE")
            self.assertTrue(cts["results_dir"].endswith("2026.10.07_05.00.00.000_1"))
            self.assertEqual(cts["session_id"], 0)

    def test_cancelled_retry_loop_returns_last_result(self):
        from xts_agent.execution import cancel
        from xts_agent.retry.retry_manager import RetryManager

        cfg = MagicMock(post_execution=None)
        suite_cfg = SuiteConfig(name="cts", plan="cts")
        suite_cfg.retry.max_retries = 3
        current = SuiteResult("cts", "FAILED", 1, 1, 0, 1.0, 4, "/r", 0)
        runner = MagicMock()
        cancel.request_cancel()
        result = RetryManager(cfg).retry_suite_until_done(runner, current, suite_cfg, ["hu-1"], "/tmp")
        self.assertIs(result, current)
        runner.execute.assert_not_called()


class LoggingTests(unittest.TestCase):
    def tearDown(self):
        import logging

        logger = logging.getLogger("xts_agent")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = True

    def test_single_console_handler_with_run_id_and_suite_in_json_file(self):
        import json
        import logging

        from xts_agent.utils.logger import run_id, setup_logging, suite_context

        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "agent.log"
            setup_logging(logging.INFO)
            logger = setup_logging(logging.INFO, log_file=str(log_file))  # re-init is idempotent
            self.assertEqual(len(logger.handlers), 2)
            self.assertFalse(logger.propagate)
            with suite_context("cts"):
                logging.getLogger("xts_agent.x").info("inside")
            logging.getLogger("xts_agent.x").info("outside")
            for handler in logger.handlers:
                handler.flush()
            inside, outside = (json.loads(line) for line in log_file.read_text().splitlines())
        self.assertEqual(inside["run_id"], run_id())
        self.assertEqual(inside["suite"], "cts")
        self.assertNotIn("suite", outside)


TF_LOG_LINES = [
    "10-04 16:20:03 I/ITestSuite: 0.0.0.0:6527 running 1 modules: [x86_64 CtsFooTestCases]",
    "10-04 16:20:04 I/ITestSuite: 0.0.0.0:6524 running 1 modules: [x86_64 CtsBarTestCases[instant]]",
    "10-04 16:20:17 I/ModuleListener: [1/1] 0.0.0.0:6529 android.sig.blocklist android.sig.DebugTest#testA FAILURE: j",
    "10-04 16:20:18 I/ModuleListener: [1/6] 0.0.0.0:6533 android.cc.Test#testB ASSUMPTION_FAILURE: org.junit.Assume",
    "10-04 16:20:19 I/ModuleListener: [2/6] 0.0.0.0:6533 android.cc.Test#testC FAILURE: java.lang.AssertionError",
    "10-04 16:20:03 D/ModuleDefinition: Running module x86_64 CtsFooTestCases",  # same start, not a new module
    # Printed per test run (dEQP: thousands) and never unsharded: must not count
    "10-05 01:26:27 I/ShardListener: Sharded test completed: x86_64 CtsFooTestCases",
    "10-05 01:26:27 I/ShardListener: Sharded test completed: x86_64 CtsFooTestCases",
    "10-05 01:26:28 I/ITestSuite: 0.0.0.0:6527 running 1 modules: [x86_64 CtsBazTestCases]",
]


class ProgressTests(unittest.TestCase):
    def test_counts_modules_failures_and_handles_partial_lines(self):
        from xts_agent.execution.progress import ProgressMonitor

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "tf.log"
            text = "\n".join(TF_LOG_LINES) + "\n"
            cut = text.index("FAILURE: java")  # split the last failure line mid-way
            log.write_text(text[:cut], encoding="utf-8")
            monitor = ProgressMonitor(log, interval_secs=60, stall_after_secs=0)
            p = monitor.poll()
            self.assertEqual((p.modules_started, p.failures), (2, 1))
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(text[cut:])
            p = monitor.poll()
        self.assertEqual(p.modules_started, 3)
        self.assertEqual(p.modules_completed, 1)  # Foo: the next module started on its device
        self.assertEqual(p.failures, 2)  # ASSUMPTION_FAILURE is not a failure
        self.assertEqual(p.as_dict()["current"], {"0.0.0.0:6527": "CtsBazTestCases", "0.0.0.0:6524": "CtsBarTestCases[instant]"})

    def test_retried_module_is_running_not_finished(self):
        from xts_agent.execution.progress import TradefedProgress

        p = TradefedProgress()
        p.feed("I/ITestSuite: s1 running 1 modules: [x86_64 A]\nI/ITestSuite: s1 running 1 modules: [x86_64 B]\n"
               "I/ITestSuite: s2 running 1 modules: [x86_64 A]\n")
        self.assertEqual((p.modules_started, p.modules_completed), (2, 0))

    def test_unsharded_multi_module_invocation(self):
        from xts_agent.execution.progress import TradefedProgress

        p = TradefedProgress()
        p.feed("I/ITestSuite: s1 running 3 modules: [x86_64 A, x86_64 B, x86_64 C]\n"
               "D/ModuleDefinition: Running module x86_64 A\nD/ModuleDefinition: Running module x86_64 B\n")
        self.assertEqual((p.modules_started, p.modules_completed), (2, 1))
        self.assertEqual(p.as_dict()["current"], {"s1": "B"})

    def test_warns_once_when_log_goes_quiet(self):
        import time as _time

        from xts_agent.execution.progress import ProgressMonitor

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "tf.log"
            log.write_text(TF_LOG_LINES[0] + "\n", encoding="utf-8")
            monitor = ProgressMonitor(log, interval_secs=60, stall_after_secs=0.2)
            monitor.report()
            monitor.progress.last_output_at = _time.time() - 1
            with self.assertLogs("xts_agent.execution.progress", "WARNING") as logs:
                monitor.report()
                monitor.report()
        self.assertEqual(len([m for m in logs.output if "silent" in m]), 1)

    def test_runner_reports_progress_while_tradefed_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "android-cts" / "tools"
            tools.mkdir(parents=True)
            script = tools / "cts-tradefed"
            body = "".join(f"echo '{line}'\nsleep 0.15\n" for line in TF_LOG_LINES)
            script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
            script.chmod(0o755)
            runner = TradefedRunner(tools.parent, "cts-tradefed")
            runner.progress_interval_secs = 0.2
            updates = []
            runner.on_progress = lambda p: updates.append(p.as_dict())
            res = runner.execute([str(script)], timeout_hours=0.01, log_dir=Path(tmp) / "logs")
        self.assertEqual(res.return_code, 0)
        self.assertGreaterEqual(len(updates), 2)
        self.assertLessEqual(updates[0]["modules_started"], updates[-1]["modules_started"])
        self.assertGreater(updates[-1]["modules_started"], 0)


class MetricsTests(unittest.TestCase):
    def _plan_result(self):
        cts = SuiteResult("CTS", "FAILED", 90, 7, 3, 3600.0, 2, "/r", 1)
        return PlanResult("Full Cert", {"CTS": cts}, 90, 7, 3, 3700.0, "FAILED")

    def test_textfile_written_atomically_with_expected_samples(self):
        from xts_agent.reporting.metrics import MetricsPublisher

        with tempfile.TemporaryDirectory() as tmp:
            pub = MetricsPublisher("Full Cert", textfile_dir=tmp)
            pub.suite_progress("CTS", {"modules_completed": 5, "failures": 2, "quiet_secs": 30})
            prom = Path(tmp) / "xts_full_cert.prom"
            running = prom.read_text()
            triage = MagicMock(summary={"by_label": {"NEW": 2, "PERSISTENT": 5}, "actionable_groups": 2})
            pub.run_finished(self._plan_result(), triage, quarantined=1)
            final = prom.read_text()
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name != prom.name]
        self.assertIn('xts_suite_modules_completed{plan="Full Cert",suite="CTS"} 5', running)
        self.assertIn('xts_run_in_progress{plan="Full Cert"} 1', running)
        self.assertIn('xts_run_in_progress{plan="Full Cert"} 0', final)
        self.assertIn('xts_suite_tests{plan="Full Cert",result="fail",suite="CTS"} 7', final)
        self.assertIn('xts_run_status{plan="Full Cert",status="FAILED"} 1', final)
        self.assertIn('xts_triage_groups{label="NEW",plan="Full Cert"} 2', final)
        self.assertIn('xts_devices_quarantined{plan="Full Cert"} 1', final)
        self.assertIn("# TYPE xts_suite_tests gauge", final)
        self.assertEqual(leftovers, [])

    def test_pushgateway_put(self):
        import http.server
        import threading

        received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                received["path"] = self.path
                received["body"] = self.rfile.read(int(self.headers["Content-Length"])).decode()
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        from xts_agent.reporting.metrics import MetricsPublisher

        pub = MetricsPublisher("Full Cert", pushgateway_url=f"http://127.0.0.1:{server.server_port}/")
        pub.run_finished(self._plan_result())
        thread.join(5)
        server.server_close()
        self.assertEqual(received["path"], "/metrics/job/xts_agent/plan/full_cert")
        self.assertIn('xts_suite_retries{plan="Full Cert",suite="CTS"} 1', received["body"])


class RetentionTests(unittest.TestCase):
    def _session(self, root: Path, name: str, results: bool = True) -> None:
        (root / "logs" / name).mkdir(parents=True)
        (root / "logs" / name / "host_log.txt").write_text("x" * 100)
        if results:
            (root / "results" / name).mkdir(parents=True)
            (root / "results" / name / "test_result.xml").write_text(PASSING_XML)
            (root / "results" / f"{name}.zip").write_text("zip")

    def test_prunes_old_sessions_but_keeps_newest_and_resumable(self):
        import json
        import time as _time

        from xts_agent.utils.retention import plan_prune, protected_sessions, prune

        with tempfile.TemporaryDirectory() as tmp:
            cts = Path(tmp) / "android-cts"
            old, resumable = "2026.09.01_10.00.00.000_1", "2026.09.02_10.00.00.000_2"
            aborted = "2026.09.03_10.00.00.000_3"
            newest = ["2026.09.04_10.00.00.000_4", "2026.09.05_10.00.00.000_5"]
            for name in (old, resumable, *newest):
                self._session(cts, name)
            self._session(cts, aborted, results=False)  # logs-only session
            (cts / "results" / "latest").symlink_to(cts / "results" / newest[-1])
            state_dir = Path(tmp) / "run_state"
            state_dir.mkdir()
            (state_dir / "cert.json").write_text(json.dumps({
                "complete": False,
                "suites": {"cts": {"results_dir": str(cts / "results" / resumable)}},
            }))
            now = _time.mktime((2026, 10, 7, 0, 0, 0, 0, 0, -1))
            plan = plan_prune([cts], keep_days=7, keep_latest=2,
                              protect=protected_sessions(state_dir), now=now)
            names = sorted(p.name for p in plan.paths)
            self.assertEqual(names, sorted([old, old, f"{old}.zip", aborted]))
            self.assertEqual(plan.protected, [resumable])
            self.assertTrue(cts.joinpath("results", old).exists())  # planning deletes nothing
            freed = prune(plan)
            self.assertGreater(freed, 0)
            self.assertFalse(cts.joinpath("results", old).exists())
            self.assertFalse(cts.joinpath("logs", aborted).exists())
            for name in (resumable, *newest):
                self.assertTrue(cts.joinpath("results", name, "test_result.xml").exists())
            self.assertTrue((cts / "results" / "latest").is_symlink())

    def test_free_space_check(self):
        from xts_agent.utils.retention import check_free_space

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(check_free_space([Path(tmp)], 0), [])
            problems = check_free_space([Path(tmp) / "not" / "created"], 10**9)
        self.assertTrue(problems)
        self.assertIn("GB free", problems[0])


if __name__ == "__main__":
    unittest.main()
