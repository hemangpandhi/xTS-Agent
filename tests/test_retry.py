"""Suite-level retry and device isolation between attempts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests import support
from tests.support import INCOMPLETE_XML, PASSING_XML
from xts_agent.config_loader import SuiteConfig
from xts_agent.execution.test_plan_executor import SuiteResult
from xts_agent.execution.tradefed_runner import TradefedRunner
from xts_agent.results.result_parser import ResultParser

setUpModule = support.setUpModule
tearDownModule = support.tearDownModule


class RetryManagerTests(unittest.TestCase):
    def _suite_result(self, status: str, session_id: int):
        return SuiteResult(
            name="cts", status=status, pass_count=0, fail_count=0, skip_count=0,
            duration=0.0, session_id=session_id, results_dir="", retry_count=0,
        )

    def _manager(self):
        from xts_agent.retry.retry_manager import RetryManager

        return RetryManager(MagicMock(post_execution=None))

    def test_incomplete_retries_not_executed_and_follows_latest_session(self):
        from xts_agent.execution.tradefed_runner import ExecutionResult

        suite = SuiteConfig(name="cts", plan="cts")
        suite.retry.max_retries = 2
        runner = TradefedRunner("/opt/xts/android-cts", "cts-tradefed")
        with tempfile.TemporaryDirectory() as tmp:
            # TradeFed numbers sessions by result-dir order: 0-4 exist already
            for i in range(5):
                old = Path(tmp) / f"2026.10.0{i + 1}_00.00.00.000_1"
                old.mkdir()
                (old / "test_result.xml").write_text(PASSING_XML, encoding="utf-8")
            first = Path(tmp) / "2026.10.07_01.00.00.000_1"
            first.mkdir()
            (first / "test_result.xml").write_text(INCOMPLETE_XML, encoding="utf-8")
            second = Path(tmp) / "2026.10.07_02.00.00.000_1"
            second.mkdir()
            (second / "test_result.xml").write_text(PASSING_XML, encoding="utf-8")
            runs = [
                ExecutionResult(True, 5, 0, 1.0, str(first), "log1"),
                ExecutionResult(True, 6, 0, 1.0, str(second), "log2"),
            ]
            with patch("xts_agent.execution.cancel.wait", return_value=False), patch.object(
                TradefedRunner, "execute", side_effect=runs
            ) as exec_mock:
                res = self._manager().retry_suite_until_done(
                    runner, self._suite_result("INCOMPLETE", 4), suite, ["s1"], tmp
                )
        first_cmd, second_cmd = (c[0][0] for c in exec_mock.call_args_list)
        self.assertEqual(first_cmd[first_cmd.index("--retry") + 1], "4")
        self.assertNotIn("--retry-type", first_cmd)
        self.assertEqual(second_cmd[second_cmd.index("--retry") + 1], "5")
        self.assertEqual(res.status, "PASSED")
        self.assertEqual(res.session_id, 6)

    def test_retry_follows_session_renumbered_by_pruning(self):
        from xts_agent.execution.tradefed_runner import ExecutionResult

        suite = SuiteConfig(name="cts", plan="cts")
        suite.retry.max_retries = 1
        runner = TradefedRunner("/opt/xts/android-cts", "cts-tradefed")
        with tempfile.TemporaryDirectory() as tmp:
            # Recorded as session 7, but older sessions were pruned since
            mine = Path(tmp) / "2026.10.07_01.00.00.000_1"
            mine.mkdir()
            (mine / "test_result.xml").write_text(INCOMPLETE_XML, encoding="utf-8")
            current = self._suite_result("INCOMPLETE", 7)
            current.results_dir = str(mine)
            with patch("xts_agent.execution.cancel.wait", return_value=False), patch.object(
                TradefedRunner, "execute", return_value=ExecutionResult(True, None, 0, 1.0, None, "log")
            ) as exec_mock:
                self._manager().retry_suite_until_done(runner, current, suite, ["s1"], tmp)
        cmd = exec_mock.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--retry") + 1], "0")

    def test_failed_but_incomplete_still_retries_not_executed(self):
        from xts_agent.execution.tradefed_runner import ExecutionResult

        # Shape of real session 7: 1303 failures and 524 of 1251 modules done
        failed_incomplete = INCOMPLETE_XML.replace('failed="0"', 'failed="1303"')
        suite = SuiteConfig(name="cts", plan="cts")
        suite.retry.max_retries = 1
        runner = TradefedRunner("/opt/xts/android-cts", "cts-tradefed")
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "test_result.xml"
            xml.write_text(failed_incomplete, encoding="utf-8")
            current = self._suite_result("FAILED", 7)
            current.details = ResultParser().parse_xml(xml)
            with patch("xts_agent.execution.cancel.wait", return_value=False), patch.object(
                TradefedRunner, "execute", return_value=ExecutionResult(True, None, 0, 1.0, None, "log")
            ) as exec_mock:
                self._manager().retry_suite_until_done(runner, current, suite, ["s1"], tmp)
        self.assertNotIn("--retry-type", exec_mock.call_args[0][0])

    def test_session_zero_is_retryable(self):
        # First run on a fresh TradeFed install is session 0
        manager = self._manager()
        self.assertTrue(manager.should_retry(self._suite_result("FAILED", 0), 0, 1))
        self.assertFalse(manager.should_retry(self._suite_result("FAILED", None), 0, 1))

    def test_retry_without_results_stops_and_keeps_status(self):
        from xts_agent.execution.tradefed_runner import ExecutionResult

        suite = SuiteConfig(name="cts", plan="cts")
        suite.retry.max_retries = 3
        runner = TradefedRunner("/opt/xts/android-cts", "cts-tradefed")
        with patch("xts_agent.execution.cancel.wait", return_value=False), patch.object(
            TradefedRunner, "execute", return_value=ExecutionResult(True, None, 0, 1.0, None, "log")
        ) as exec_mock:
            res = self._manager().retry_suite_until_done(
                runner, self._suite_result("FAILED", 4), suite, ["s1"], "/tmp"
            )
        self.assertEqual(exec_mock.call_count, 1)
        self.assertEqual(res.status, "FAILED")


class IsolationTests(unittest.TestCase):
    def _handler(self, virtual: bool, command: str = "cvd powerwash --serial {serial}"):
        from xts_agent.retry.isolation import IsolationHandler

        dm = MagicMock()
        dm.is_virtual_device.return_value = virtual
        dm.reboot_device.return_value = True
        dm.wait_for_device.return_value = True
        return IsolationHandler(dm, virtual_reset_command=command), dm

    def test_physical_device_is_never_wiped(self):
        handler, dm = self._handler(virtual=False)
        with patch("subprocess.run") as run, patch(
            "xts_agent.retry.isolation.AdbWrapper"
        ) as adb:
            handler.apply_isolation("HU123", "FULLY_ISOLATED")
        run.assert_not_called()
        dm.reboot_device.assert_called_once_with("HU123")
        shell_cmds = " ".join(c[0][1] for c in adb.shell.call_args_list)
        self.assertNotIn("wipe", shell_cmds)
        self.assertNotIn("MASTER_CLEAR", shell_cmds)

    def test_virtual_device_uses_host_reset(self):
        handler, dm = self._handler(virtual=True)
        with patch("subprocess.run") as run, patch("xts_agent.retry.isolation.AdbWrapper"):
            handler.apply_isolation("0.0.0.0:6520", "FULLY_ISOLATED")
        self.assertEqual(run.call_args[0][0], ["cvd", "powerwash", "--serial", "0.0.0.0:6520"])
        dm.reboot_device.assert_not_called()

    def test_virtual_without_command_just_reboots(self):
        handler, dm = self._handler(virtual=True, command="")
        with patch("subprocess.run") as run, patch("xts_agent.retry.isolation.AdbWrapper"):
            handler.apply_isolation("0.0.0.0:6520", "FULLY_ISOLATED")
        run.assert_not_called()
        dm.reboot_device.assert_called_once()

    def test_is_virtual_device_reads_props(self):
        from xts_agent.device.device_manager import DeviceManager

        dm = DeviceManager()
        for props, expected in (
            ({"ro.hardware": "cutf_cvm"}, True),
            ({"ro.kernel.qemu": "1"}, True),
            ({"ro.hardware": "qcom"}, False),
            ({}, False),
        ):
            with self.subTest(props=props), patch.object(
                DeviceManager, "get_device_properties", return_value=props
            ):
                self.assertEqual(dm.is_virtual_device("x"), expected)


if __name__ == "__main__":
    unittest.main()
