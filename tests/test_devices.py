"""Device health gates, leases, preparation and quarantine."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests import support
from xts_agent.config_loader import ConfigLoader
from xts_agent.execution.test_plan_executor import TestPlanExecutor

setUpModule = support.setUpModule
tearDownModule = support.tearDownModule


class BatteryTests(unittest.TestCase):
    def _info(self, serial, level, present):
        from xts_agent.device.device_manager import DeviceInfo

        return DeviceInfo(serial, "", "", "fp", 34, level, "device", "aaos", battery_present=present)

    def test_reads_present_flag(self):
        from xts_agent.device.device_manager import DeviceManager

        dump = "  AC powered: true\n  present: false\n  level: 0\n"
        with patch("xts_agent.device.device_manager.AdbWrapper.shell", return_value=dump):
            self.assertEqual(DeviceManager()._read_battery("hu"), (0, True, False))

    def test_head_unit_without_battery_is_available_regardless_of_serial(self):
        from xts_agent.device.device_manager import DeviceManager

        devices = [
            self._info("192.168.1.10:5555", 0, present=False),  # head unit on TCP
            self._info("PHONE123", 5, present=True),  # drained phone
            self._info("PHONE456", 90, present=True),
        ]
        dm = DeviceManager()
        with patch.object(DeviceManager, "discover_devices", return_value=devices):
            serials = [d.serial for d in dm.get_available_devices(min_battery=20)]
        self.assertEqual(serials, ["192.168.1.10:5555", "PHONE456"])


class HealthGateTests(unittest.TestCase):
    def test_unhealthy_devices_excluded_and_no_internet_only_warns(self):
        from xts_agent.device.device_manager import DeviceInfo, DeviceManager, HealthReport

        devs = [DeviceInfo(s, "", "", "fp", 34, 100, "device", "aaos") for s in ("ok", "full", "offnet")]
        reports = {
            "ok": HealthReport(100, True, 9000, True, True, True),
            "full": HealthReport(100, True, 100, True, True, False, ["only 100 MB free on /data"]),
            "offnet": HealthReport(100, True, 9000, False, True, True),
        }
        with patch.object(DeviceManager, "check_device_health", side_effect=lambda s: reports[s]), \
                self.assertLogs("xts_agent.device.device_manager", "WARNING") as logs:
            kept = DeviceManager().filter_healthy(devs)
        self.assertEqual([d.serial for d in kept], ["ok", "offnet"])
        self.assertTrue(any("only 100 MB free" in m for m in logs.output))
        self.assertTrue(any("offnet has no validated internet" in m for m in logs.output))

    def test_health_report_lists_problems(self):
        from xts_agent.device.device_manager import DeviceManager

        def shell(serial, cmd, **kw):
            if cmd == "dumpsys battery":
                return "present: true\nlevel: 5\n"
            if cmd == "df /data":
                return "Filesystem 1K-blocks Used Available Use% Mounted\n/dev/x 100 90 102400 90% /data"
            return ""

        with patch("xts_agent.device.device_manager.AdbWrapper.shell", side_effect=shell):
            report = DeviceManager().check_device_health("s")
        self.assertFalse(report.healthy)
        self.assertEqual(report.problems, ["battery 5%", "only 100 MB free on /data"])


class LeaseTests(unittest.TestCase):
    HOLDER = """
import os, sys, time
from xts_agent.device.device_manager import DeviceInfo, DeviceManager
dm = DeviceManager(lease_dir=sys.argv[1])
dev = DeviceInfo("s1", "", "", "fp", 34, 100, "device", "aaos")
dm.allocate_devices(1, candidates=[dev])
if sys.argv[2] == "spawn-and-crash":
    import subprocess
    child = subprocess.Popen(["sleep", "30"], pass_fds=dm.lease_fds(["s1"]), start_new_session=True)
    print(child.pid, flush=True)
    os._exit(0)  # agent dies without releasing; TradeFed stand-in keeps running
print("held", flush=True)
time.sleep(30)
"""

    def _devices(self):
        from xts_agent.device.device_manager import DeviceInfo

        return [DeviceInfo(s, "", "", "fp", 34, 100, "device", "aaos") for s in ("s1", "s2")]

    def _spawn(self, lease_dir, mode):
        import subprocess
        import sys

        proc = subprocess.Popen(
            [sys.executable, "-c", self.HOLDER, lease_dir, mode], stdout=subprocess.PIPE, text=True
        )
        return proc, proc.stdout.readline().strip()

    def test_lease_serials_is_all_or_nothing(self):
        from xts_agent.device.device_manager import DeviceManager

        with tempfile.TemporaryDirectory() as tmp:
            proc, _ = self._spawn(tmp, "hold")  # another job holds s1
            try:
                dm = DeviceManager(lease_dir=tmp)
                busy = dm.lease_serials(["s2", "s1"])
                self.assertEqual(list(busy), ["s1"])
                self.assertIn("pid=", busy["s1"])
                self.assertIsNone(dm.leased_elsewhere("s2"))  # s2 was given back
                self.assertEqual(dm.lease_fds(["s2"]), [])
            finally:
                proc.kill()
                proc.wait()
            self.assertEqual(dm.lease_serials(["s1", "s2"]), {})
            self.assertEqual(len(dm.lease_fds(["s1", "s2"])), 2)
            dm.release_devices(["s1", "s2"])

    def test_other_process_cannot_take_leased_device(self):
        from xts_agent.device.device_manager import DeviceManager

        with tempfile.TemporaryDirectory() as leases:
            holder, _ = self._spawn(leases, "hold")
            try:
                dm = DeviceManager(lease_dir=leases)
                self.assertIn("pid=", dm.leased_elsewhere("s1") or "")
                got = dm.allocate_devices(1, candidates=self._devices())
                self.assertEqual([d.serial for d in got], ["s2"])  # s1 skipped
                with self.assertRaises(ValueError) as ctx:
                    DeviceManager(lease_dir=leases).allocate_devices(2, candidates=self._devices())
                self.assertIn("leased by other processes", str(ctx.exception))
                dm.release_devices(["s2"])
            finally:
                holder.kill()
                holder.wait()
            # Holder died: the kernel dropped its lock
            self.assertIsNone(DeviceManager(lease_dir=leases).leased_elsewhere("s1"))

    def test_lease_survives_agent_crash_while_tradefed_runs(self):
        import os
        import signal
        import time as _time

        from xts_agent.device.device_manager import DeviceManager

        with tempfile.TemporaryDirectory() as leases:
            agent, child_pid = self._spawn(leases, "spawn-and-crash")
            agent.wait(timeout=10)
            try:
                self.assertIsNotNone(DeviceManager(lease_dir=leases).leased_elsewhere("s1"))
            finally:
                os.kill(int(child_pid), signal.SIGKILL)
            for _ in range(50):
                if DeviceManager(lease_dir=leases).leased_elsewhere("s1") is None:
                    break
                _time.sleep(0.1)
            self.assertIsNone(DeviceManager(lease_dir=leases).leased_elsewhere("s1"))


class DevicePrepTests(unittest.TestCase):
    def _cfg(self, **kw):
        from xts_agent.config_loader import DevicePrepConfig

        return DevicePrepConfig(**kw)

    def test_profile_steps(self):
        from xts_agent.device.device_prep import DevicePreparer

        cmds = [s["cmd"] for s in DevicePreparer(self._cfg(wifi_ssid="lab net", wifi_password="p w")).steps()]
        self.assertIn("locksettings set-disabled true", cmds)
        self.assertIn("cmd location set-location-enabled true", cmds)
        self.assertIn("cmd wifi connect-network 'lab net' wpa2 'p w'", cmds)
        no_wifi = [s["cmd"] for s in DevicePreparer(self._cfg()).steps()]
        self.assertFalse(any("wifi" in c for c in no_wifi))  # no SSID => skip wifi

    def test_wifi_password_never_logged(self):
        from xts_agent.device.adb_wrapper import AdbError
        from xts_agent.device.device_prep import DevicePreparer

        prep = DevicePreparer(self._cfg(wifi_ssid="lab", wifi_password="TopSecret!"))
        with patch("xts_agent.device.device_prep.AdbWrapper.shell",
                   side_effect=AdbError("Command failed: cmd wifi connect-network lab wpa2 TopSecret!")), \
                self.assertLogs("xts_agent.device", "INFO") as logs:
            failed = prep.prepare("hu")
        self.assertIn("join wifi lab", failed)
        self.assertNotIn("TopSecret!", "\n".join(logs.output))

    def test_wifi_password_from_env_and_defaults_section(self):
        import os

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XTS_WIFI_PASSWORD": "envpw"}):
            d = Path(tmp) / "d.yaml"
            d.write_text("device:\n  wifi_ssid: lab\n  extra_commands: ['cmd car_service x']\n")
            p = Path(tmp) / "p.yaml"
            p.write_text("name: t\nsuites: []\ndevices:\n  prepare: false\n")
            plan = ConfigLoader(p, defaults_path=d).load_plan()
        self.assertEqual(plan.device_prep.wifi_ssid, "lab")
        self.assertEqual(plan.device_prep.wifi_password, "envpw")
        self.assertEqual(plan.device_prep.extra_commands, ["cmd car_service x"])
        self.assertFalse(plan.devices.prepare)


class QuarantineTests(unittest.TestCase):
    def test_quarantine_after_consecutive_failures_and_reset(self):
        from xts_agent.device.device_ledger import DeviceLedger

        with tempfile.TemporaryDirectory() as tmp:
            ledger = DeviceLedger(Path(tmp) / "ledger.json", threshold=3, hours=1)
            self.assertFalse(ledger.record_failure("hu", "offline"))
            ledger.record_success("hu")  # success resets the streak
            self.assertFalse(ledger.record_failure("hu", "offline"))
            self.assertFalse(ledger.record_failure("hu", "reboot"))
            self.assertTrue(ledger.record_failure("hu", "reboot"))
            self.assertIn("3 consecutive failures", ledger.quarantine_reason("hu"))
            self.assertEqual(list(ledger.quarantined()), ["hu"])
            self.assertTrue(ledger.release("hu"))
            self.assertIsNone(ledger.quarantine_reason("hu"))

    def test_quarantined_device_not_available(self):
        from xts_agent.device.device_manager import DeviceInfo, DeviceManager

        with tempfile.TemporaryDirectory() as tmp:
            dm = DeviceManager(lease_dir=str(Path(tmp) / "leases"))
            dm.quarantine_threshold = 1
            dm.record_device_outcomes({"bad": False, "good": True}, "went offline during a suite")
            devs = [DeviceInfo(s, "", "", "fp", 34, 100, "device", "aaos") for s in ("bad", "good")]
            with patch.object(DeviceManager, "discover_devices", return_value=devs):
                self.assertEqual([d.serial for d in dm.get_available_devices()], ["good"])
            self.assertTrue((Path(tmp) / "device_ledger.json").exists())

    def test_device_lost_during_suite_is_recorded(self):
        executor = TestPlanExecutor.__new__(TestPlanExecutor)
        executor.device_manager = MagicMock()
        with patch("xts_agent.execution.test_plan_executor.AdbWrapper.devices", return_value=["a"]):
            support._SURVIVAL_PATCH.temp_original(executor, ["a", "b"])
        executor.device_manager.record_device_outcomes.assert_called_once_with(
            {"a": True, "b": False}, "went offline during a suite"
        )

    def test_ledger_is_safe_across_processes(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            code = (
                "import sys\nfrom xts_agent.device.device_ledger import DeviceLedger\n"
                "l = DeviceLedger(sys.argv[1], threshold=1000)\n"
                "[l.record_failure(sys.argv[2], 'x') for _ in range(50)]\n"
            )
            procs = [subprocess.Popen([sys.executable, "-c", code, str(path), "hu"]) for _ in range(4)]
            for p in procs:
                p.wait()
            import json

            self.assertEqual(json.loads(path.read_text())["hu"]["consecutive_failures"], 200)


if __name__ == "__main__":
    unittest.main()
