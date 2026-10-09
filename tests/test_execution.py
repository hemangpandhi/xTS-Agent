"""TradeFed command building, sharding, suite scheduling, discovery and resume."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests import support
from tests.support import INCOMPLETE_XML, PASSING_XML
from xts_agent.config_loader import ConfigLoader, SuiteConfig
from xts_agent.execution.shard_manager import ShardManager
from xts_agent.execution.test_plan_executor import SuiteResult, TestPlanExecutor
from xts_agent.execution.tradefed_runner import TradefedRunner

setUpModule = support.setUpModule
tearDownModule = support.tearDownModule


class ShardManagerTests(unittest.TestCase):
    def test_auto_shard_count(self):
        mgr = ShardManager(device_manager=MagicMock())
        suite = SuiteConfig(name="cts", sharding=__import__(
            "xts_agent.config_loader", fromlist=["ShardingConfig"]
        ).ShardingConfig(shard_count="auto", max_shards=16))
        self.assertEqual(mgr.calculate_shard_count(10, suite), 10)
        self.assertEqual(mgr.calculate_shard_count(20, suite), 16)

    def test_fixed_shard_capped_by_devices(self):
        mgr = ShardManager(device_manager=MagicMock())
        suite = SuiteConfig(
            name="cts",
            sharding=__import__(
                "xts_agent.config_loader", fromlist=["ShardingConfig"]
            ).ShardingConfig(shard_count=15, max_shards=16),
        )
        self.assertEqual(mgr.calculate_shard_count(4, suite), 4)


class TradefedRunnerTests(unittest.TestCase):
    def test_build_run_command_includes_serials_and_retry(self):
        runner = TradefedRunner("/tmp/android-cts", "cts-tradefed")
        cmd = runner.build_run_command(
            plan="cts",
            shard_count=2,
            retry_config={
                "max_testcase_run_count": 3,
                "retry_strategy": "RETRY_ANY_FAILURE",
                "isolation_grade": "REBOOT_ISOLATED",
                "reboot_at_last_retry": True,
            },
            exclude_filters=["BadModule"],
            include_filters=["GoodModule"],
            device_serials=["0.0.0.0:6520", "0.0.0.0:6524"],
            extra_args=["--skip-system-status-check"],
        )
        self.assertIn("-s", cmd)
        self.assertIn("0.0.0.0:6520", cmd)
        self.assertIn("0.0.0.0:6524", cmd)
        self.assertIn("--shard-count", cmd)
        self.assertIn("--max-testcase-run-count", cmd)
        self.assertIn("--retry-strategy", cmd)
        self.assertIn("--retry-isolation-grade", cmd)
        self.assertIn("--reboot-at-last-retry", cmd)
        self.assertIn("--exclude-filter", cmd)
        self.assertIn("--include-filter", cmd)
        self.assertIn("--skip-system-status-check", cmd)

    def test_retry_command_shards_across_all_serials(self):
        runner = TradefedRunner("/tmp/android-cts", "cts-tradefed")
        cmd = runner.build_retry_command(7, "FAILED", device_serials=["a", "b", "c"])
        self.assertEqual(cmd[cmd.index("--shard-count") + 1], "3")
        self.assertEqual(cmd[cmd.index("--retry") + 1], "7")
        single = runner.build_retry_command(7, "FAILED", device_serials=["a"])
        self.assertNotIn("--shard-count", single)

    @staticmethod
    def _make_result_dir(root: Path, name: str) -> Path:
        d = root / name
        d.mkdir(parents=True)
        (d / "test_result.xml").write_text("<Result/>", encoding="utf-8")
        return d

    def test_find_results_dir_from_result_directory_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = TradefedRunner(Path(tmp) / "android-cts", "cts-tradefed")
            results = self._make_result_dir(
                Path(tmp) / "android-cts" / "results", "2026.10.06_01.02.22.903_1303"
            )
            # Real TradeFed tail: the global log line must not be mistaken for results
            output = (
                "Saved log to /tmp/tradefed_global_log_17628663554220524335.txt\n"
                f"10-06 01:04:10 I/ResultReporter: RESULT DIRECTORY            : {results}\n"
            )
            self.assertEqual(runner.find_results_dir(output), str(results))

    def test_find_results_dir_ignores_saved_log_and_missing_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = TradefedRunner(Path(tmp) / "android-cts", "cts-tradefed")
            empty = Path(tmp) / "android-cts" / "results" / "2026.01.01_00.00.00.000_1"
            empty.mkdir(parents=True)
            output = (
                "Saved log to /tmp/tradefed_global_log_1.txt\n"
                f"RESULT DIRECTORY : {empty}\n"
            )
            self.assertIsNone(runner.find_results_dir(output))

    def test_find_results_dir_prefers_new_dir_since_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "android-cts" / "results"
            runner = TradefedRunner(Path(tmp) / "android-cts", "cts-tradefed")
            self._make_result_dir(root, "2026.10.03_13.21.28.012_2796")
            (root / "latest").symlink_to(root / "2026.10.03_13.21.28.012_2796")
            before = runner.snapshot_result_dirs()
            new = self._make_result_dir(root, "2026.10.04_16.16.31.269_4285")
            # Interrupted runs print no RESULT DIRECTORY line
            self.assertEqual(runner.find_results_dir("", before=before), str(new))

    def test_parse_session_table_from_list_results(self):
        table = (
            "Session  Pass    Fail  Warning  Modules Complete  Result Directory              "
            "Test Plan  Device serial(s)  Build ID         Product\n"
            "0        3275    0     0        1 of 1            2026.10.03_13.21.28.012_2796  "
            "cts        0.0.0.0:6520      CP2A.260605.016  aosp_cf_x86_64_auto\n"
            "4        161222  2067  0        421 of 1251       2026.10.04_16.16.31.269_4285  "
            "cts        0.0.0.0:6522, 0.0.0.0:6533  CP2A.260605.016  aosp_cf_x86_64_auto\n"
        )
        parse = TradefedRunner.parse_session_table
        self.assertEqual(parse(table, "2026.10.04_16.16.31.269_4285"), 4)
        self.assertEqual(parse(table, "2026.10.03_13.21.28.012_2796"), 0)
        self.assertIsNone(parse(table, "2026.10.05_00.00.00.000_1"))
        # The ATS console id must never be read as a session index
        self.assertIsNone(parse("{olc_session_id=93400c03}", "93400c03"))

    def test_resolve_session_id_falls_back_to_dir_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "android-cts" / "results"
            runner = TradefedRunner(Path(tmp) / "android-cts", "cts-tradefed")
            self._make_result_dir(root, "2026.10.03_13.21.28.012_2796")
            target = self._make_result_dir(root, "2026.10.04_16.16.31.269_4285")
            (root / "latest").symlink_to(target)
            (root / "2026.10.04_16.16.31.269_4285.zip").write_text("", encoding="utf-8")
            # No tradefed script in the temp layout => 'list results' cannot run
            self.assertEqual(runner.resolve_session_id(target), 1)


class ExecutorPriorityAndDevicesTests(unittest.TestCase):
    def test_priority_ascending_and_serials_passed(self):
        from xts_agent.config_loader import (
            DeviceRequirements,
            PathsConfig,
            ShardingConfig,
            TestPlanConfig,
        )

        low = SuiteConfig(name="cts", priority=1, plan="cts", sharding=ShardingConfig(shard_count=1))
        high = SuiteConfig(name="ats", priority=5, plan="ats", sharding=ShardingConfig(shard_count=1))
        plan = TestPlanConfig(
            name="t",
            suites=[high, low],
            devices=DeviceRequirements(min_devices=1),
            paths=PathsConfig(xts_packages_dir="/opt/xts"),
        )

        device = MagicMock()
        device.serial = "serial-1"
        device.device_type = "aaos"

        dm = MagicMock()
        dm.get_available_devices.return_value = [device]
        dm.allocate_devices.return_value = [device]
        dm.select_shard_pool.return_value = [device]

        shard = ShardManager(dm)
        registry = MagicMock()
        registry.get_suite.return_value = None

        executor = TestPlanExecutor(plan, dm, shard, None, registry, results_dir="/tmp/xts-results")
        result = executor.execute_plan(plan, dry_run=True)
        self.assertEqual(list(result.suites_results.keys()), ["cts", "ats"])
        self.assertTrue(result.device_serials[0].startswith("dry-run-device-"))
        self.assertEqual(result.overall_status, "DRY_RUN")
        # dry-run does not allocate live devices
        dm.allocate_devices.assert_not_called()

    def test_release_on_failure(self):
        from xts_agent.config_loader import DeviceRequirements, PathsConfig, TestPlanConfig

        suite = SuiteConfig(name="cts", plan="cts")
        plan = TestPlanConfig(
            name="t",
            suites=[suite],
            devices=DeviceRequirements(min_devices=1),
            paths=PathsConfig(),
        )
        device = MagicMock(serial="s1", device_type="phone")
        dm = MagicMock()
        dm.get_available_devices.return_value = [device]
        dm.allocate_devices.return_value = [device]
        dm.select_shard_pool.return_value = [device]

        executor = TestPlanExecutor(
            plan, dm, ShardManager(dm), None, MagicMock(get_suite=MagicMock(return_value=None)),
            results_dir="/tmp/xts-results",
        )
        # Force missing tradefed script => failure path after allocate
        with patch.object(TestPlanExecutor, "_resolve_package_path", return_value=Path("/no/such/suite")):
            res = executor.execute_suite(suite, dry_run=False)
        self.assertEqual(res.status, "FAILED")
        dm.release_devices.assert_called()

    def test_live_allocate_passes_serials_into_command(self):
        from xts_agent.config_loader import DeviceRequirements, PathsConfig, TestPlanConfig
        from xts_agent.execution.tradefed_runner import ExecutionResult

        suite = SuiteConfig(name="cts", plan="cts")
        plan = TestPlanConfig(
            name="t",
            suites=[suite],
            devices=DeviceRequirements(min_devices=1),
            paths=PathsConfig(),
        )
        device = MagicMock(serial="serial-1", device_type="aaos")
        dm = MagicMock()
        dm.get_available_devices.return_value = [device]
        dm.allocate_devices.return_value = [device]
        dm.select_shard_pool.return_value = [device]

        executor = TestPlanExecutor(
            plan,
            dm,
            ShardManager(dm),
            None,
            MagicMock(get_suite=MagicMock(return_value=None)),
            results_dir="/tmp/xts-results",
        )

        with tempfile.TemporaryDirectory() as tmp:
            suite_dir = Path(tmp) / "android-cts" / "tools"
            suite_dir.mkdir(parents=True)
            (suite_dir / "cts-tradefed").write_text("#!/bin/sh\n", encoding="utf-8")
            results_dir = Path(tmp) / "android-cts" / "results" / "2026.10.06_01.02.22.903_1303"
            results_dir.mkdir(parents=True)
            (results_dir / "test_result.xml").write_text(PASSING_XML, encoding="utf-8")
            with patch.object(
                TestPlanExecutor, "_resolve_package_path", return_value=suite_dir.parent
            ), patch.object(
                TradefedRunner,
                "execute",
                return_value=ExecutionResult(True, 1, 0, 1.0, str(results_dir), "log"),
            ) as exec_mock:
                res = executor.execute_suite(suite, dry_run=False)

        self.assertEqual(res.status, "PASSED")
        cmd = exec_mock.call_args[0][0]
        self.assertIn("-s", cmd)
        self.assertIn("serial-1", cmd)
        dm.release_devices.assert_called_with(["serial-1"])


class Aapt2Tests(unittest.TestCase):
    def test_check_never_modifies_launcher(self):
        from xts_agent.utils.env_validator import EnvironmentValidator

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "cts-tradefed"
            body = "--aapt='/home/someone/Android/Sdk/build-tools/34.0.0/aapt2' \\\n"
            script.write_text(body, encoding="utf-8")
            self.assertFalse(EnvironmentValidator.check_tradefed_script(script))
            self.assertEqual(script.read_text(encoding="utf-8"), body)
            self.assertEqual(list(Path(tmp).iterdir()), [script])

            stock = "--aapt='$(type -P aapt2 2>/dev/null)' \\\n"
            script.write_text(stock, encoding="utf-8")
            self.assertTrue(EnvironmentValidator.check_tradefed_script(script))

    def test_ensure_aapt2_prepends_build_tools_to_path(self):
        import os

        from xts_agent.utils.env_validator import EnvironmentValidator

        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "build-tools" / "34.0.0"
            tools.mkdir(parents=True)
            (tools / "aapt2").write_text("", encoding="utf-8")
            env = {"PATH": "/usr/bin", "ANDROID_HOME": tmp}
            # SDK build-tools must win over a distro /usr/bin/aapt2 on PATH
            with patch.dict(os.environ, env, clear=True), patch(
                "shutil.which", return_value="/usr/bin/aapt2"
            ):
                found = EnvironmentValidator.ensure_aapt2_on_path()
                self.assertEqual(found, tools / "aapt2")
                self.assertTrue(os.environ["PATH"].startswith(str(tools)))


class ScopedCleanupTests(unittest.TestCase):
    def test_kills_only_recorded_tradefed_groups(self):
        import subprocess

        from xts_agent.execution.tradefed_runner import kill_recorded_tradefed

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake-tradefed"
            script.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
            script.chmod(0o755)
            ours = subprocess.Popen([str(script)], start_new_session=True)
            other_job = subprocess.Popen([str(script)], start_new_session=True)
            try:
                logs = Path(tmp) / "logs"
                logs.mkdir()
                (logs / f"tradefed_{ours.pid}.pid").write_text(str(ours.pid))
                (logs / "tradefed_999999.pid").write_text("999999")  # stale

                killed = kill_recorded_tradefed(logs, grace_secs=5)

                self.assertEqual(killed, [ours.pid])
                self.assertIsNotNone(ours.wait(timeout=10))
                self.assertIsNone(other_job.poll())
                self.assertEqual(list(logs.glob("*.pid")), [])
            finally:
                for proc in (ours, other_job):
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()

    def test_execute_removes_pidfile_after_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "android-cts" / "tools"
            tools.mkdir(parents=True)
            script = tools / "cts-tradefed"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            runner = TradefedRunner(tools.parent, "cts-tradefed")
            logs = Path(tmp) / "logs"
            res = runner.execute([str(script)], timeout_hours=0.01, log_dir=logs)
            self.assertEqual(res.return_code, 0)
            self.assertEqual(list(logs.glob("*.pid")), [])


class ShardPoolTests(unittest.TestCase):
    def _dev(self, serial, fp, dtype="aaos"):
        from xts_agent.device.device_manager import DeviceInfo

        return DeviceInfo(serial, "m", "p", fp, 34, 100, "device", dtype)

    def test_shards_only_across_one_build(self):
        from xts_agent.device.device_manager import DeviceManager

        a = "google/car/cf:16/CP2A/1:userdebug/dev-keys"
        b = "google/car/cf:16/CP2A/2:userdebug/dev-keys"
        devices = [self._dev("s1", a), self._dev("s2", b), self._dev("s3", a), self._dev("s4", "")]
        pool = DeviceManager().select_shard_pool(devices)
        self.assertEqual([d.serial for d in pool], ["s1", "s3"])

    def test_device_type_filtered_before_counting(self):
        from xts_agent.device.device_manager import DeviceManager

        fp = "x:user/release-keys"
        devices = [self._dev(f"p{i}", fp, "phone") for i in range(6)] + [
            self._dev(f"a{i}", fp) for i in range(4)
        ]
        dm = DeviceManager()
        pool = dm.select_shard_pool(devices, "aaos")
        self.assertEqual(len(pool), 4)
        allocated = dm.allocate_devices(len(pool), "aaos", candidates=pool)
        self.assertEqual({d.serial for d in allocated}, {"a0", "a1", "a2", "a3"})

    def test_required_props_filter(self):
        from xts_agent.device.device_manager import DeviceManager

        fp = "x:user/release-keys"
        devices = [self._dev("s1", fp), self._dev("s2", fp)]
        props = {"s1": {"ro.product.model": "HU-A"}, "s2": {"ro.product.model": "HU-B"}}
        with patch.object(DeviceManager, "get_device_properties", side_effect=lambda s: props[s]):
            pool = DeviceManager().select_shard_pool(devices, required_props={"ro.product.model": "HU-B"})
        self.assertEqual([d.serial for d in pool], ["s2"])

    def test_user_build_warning(self):
        from xts_agent.execution.test_plan_executor import _warn_if_not_user_build

        with patch("xts_agent.execution.test_plan_executor.logger") as log:
            _warn_if_not_user_build("google/cf/cf:16/CP2A/1:userdebug/dev-keys")
            self.assertEqual(log.warning.call_count, 1)
            _warn_if_not_user_build("oem/hu/hu:16/AB1/42:user/release-keys")
            self.assertEqual(log.warning.call_count, 1)


class DiscoveryTests(unittest.TestCase):
    DUMPSYS = (
        "Active default network: 100\n"
        "  NetworkAgentInfo{network{100}  ni{WIFI CONNECTED}  nc{[ Transports: WIFI "
        "Capabilities: INTERNET&NOT_RESTRICTED&TRUSTED&VALIDATED&NOT_VPN ]}}\n"
        "  NetworkAgentInfo{network{101}  ni{MOBILE} nc{[ Capabilities: INTERNET ]}}\n"
    )

    def test_validated_network_reads_active_network_only(self):
        from xts_agent.device.device_manager import DeviceManager

        dm = DeviceManager()
        with patch("xts_agent.device.device_manager.AdbWrapper.shell", return_value=self.DUMPSYS):
            self.assertTrue(dm.has_validated_network("s"))
        unvalidated = self.DUMPSYS.replace("Active default network: 100", "Active default network: 101")
        with patch("xts_agent.device.device_manager.AdbWrapper.shell", return_value=unvalidated):
            self.assertFalse(dm.has_validated_network("s"))
        # The real Cuttlefish case: INTERNET without VALIDATED
        no_val = self.DUMPSYS.replace("&VALIDATED", "")
        with patch("xts_agent.device.device_manager.AdbWrapper.shell", return_value=no_val):
            self.assertFalse(dm.has_validated_network("s"))

    def test_discovery_probes_in_parallel_and_caches_type(self):
        import threading
        import time as _time

        from xts_agent.device.device_manager import DeviceManager

        serials = [f"s{i}" for i in range(8)]
        listing = "List of devices attached\n" + "".join(f"{s} device\n" for s in serials) + "x offline\n"
        active = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def slow_props(serial):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            _time.sleep(0.05)
            with lock:
                active["now"] -= 1
            return {"ro.build.fingerprint": "fp:user/k"}

        dm = DeviceManager()
        with patch("xts_agent.device.device_manager.AdbWrapper._run_cmd", return_value=listing), \
                patch.object(DeviceManager, "get_device_properties", side_effect=slow_props), \
                patch.object(DeviceManager, "is_aaos_device", return_value=True) as aaos, \
                patch.object(DeviceManager, "_read_battery", return_value=(80, True, True)):
            first = dm.discover_devices()
            second = dm.discover_devices()
        self.assertEqual([d.serial for d in first], serials + ["x"])
        self.assertEqual(first[-1].state, "offline")
        self.assertGreater(active["peak"], 1)
        self.assertEqual(aaos.call_count, len(serials))  # cached on 2nd pass
        self.assertEqual([d.device_type for d in second[:-1]], ["aaos"] * len(serials))

    def test_reboot_and_wait_all_reports_failures(self):
        from xts_agent.device.device_manager import DeviceManager

        dm = DeviceManager()
        with patch.object(DeviceManager, "reboot_device", return_value=True), \
                patch.object(DeviceManager, "wait_for_device", side_effect=lambda s, t: s != "bad"):
            self.assertEqual(dm.reboot_and_wait_all(["a", "bad"]), {"a": True, "bad": False})


class ResumeTests(unittest.TestCase):
    FP = "oem/hu/hu:16/AB1/42:user/release-keys"

    def _setup(self, tmp: Path):
        from xts_agent.config_loader import DeviceRequirements, PathsConfig, TestPlanConfig
        from xts_agent.execution.run_state import RunState

        for suite in ("cts", "vts"):
            tools = tmp / f"android-{suite}" / "tools"
            tools.mkdir(parents=True)
            (tools / f"{suite}-tradefed").write_text("#!/bin/sh\n", encoding="utf-8")
        suites = [
            SuiteConfig(name="cts", plan="cts", priority=1, package_path=str(tmp / "android-cts")),
            SuiteConfig(name="vts", plan="vts", priority=2, package_path=str(tmp / "android-vts")),
        ]
        plan = TestPlanConfig(
            name="cert", suites=suites, devices=DeviceRequirements(min_devices=1), paths=PathsConfig()
        )
        device = MagicMock(serial="hu-1", device_type="aaos", build_fingerprint=self.FP)
        dm = MagicMock()
        dm.get_available_devices.return_value = [device]
        dm.select_shard_pool.return_value = [device]
        dm.allocate_devices.return_value = [device]
        state = RunState.for_plan(tmp / "results", "cert")
        executor = TestPlanExecutor(
            plan, dm, ShardManager(dm), None, MagicMock(get_suite=MagicMock(return_value=None)),
            results_dir=tmp / "results", run_state=state,
        )
        return plan, executor, dm, state

    @staticmethod
    def _result_dir(tmp: Path, suite: str, name: str, xml: str) -> Path:
        d = tmp / f"android-{suite}" / "results" / name
        d.mkdir(parents=True)
        (d / "test_result.xml").write_text(xml, encoding="utf-8")
        return d

    def test_resume_skips_passed_and_continues_interrupted_suite(self):
        from xts_agent.execution.tradefed_runner import ExecutionResult

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            plan, executor, dm, state = self._setup(tmp)
            cts_dir = self._result_dir(tmp, "cts", "2026.10.07_01.00.00.000_1", PASSING_XML)

            # --- first run: CTS passes, then the host dies inside VTS ---
            state.start("cert", "certification")
            state.suite_updated(SuiteResult("cts", "PASSED", 1, 0, 0, 1.0, 3, str(cts_dir), 0))
            vts_runner = TradefedRunner(tmp / "android-vts", "vts-tradefed")
            state.suite_started("vts", vts_runner.snapshot_result_dirs(), self.FP, ["hu-1"])
            # TradeFed had flushed a partial result before the crash
            partial = self._result_dir(tmp, "vts", "2026.10.07_02.00.00.000_2", INCOMPLETE_XML)

            # --- resume ---
            calls = []

            def fake_execute(self_runner, cmd, timeout_hours, log_dir, env=None):
                calls.append(cmd)
                # The retry session writes its own (cumulative) result dir
                done = self._result_dir(tmp, "vts", "2026.10.07_03.00.00.000_3", PASSING_XML)
                return ExecutionResult(True, 1, 0, 1.0, str(done), "log")

            with patch.object(TradefedRunner, "execute", fake_execute), patch.object(
                TradefedRunner, "resolve_session_id", return_value=0
            ):
                result = executor.execute_plan(plan, resume=True)

            self.assertEqual(len(calls), 1, calls)  # CTS skipped, VTS continued once
            cmd = calls[0]
            self.assertEqual(cmd[1:4], ["run", "retry", "--retry"])
            self.assertEqual(cmd[cmd.index("--retry") + 1], "0")
            self.assertNotIn("--retry-type", cmd)  # incomplete => FAILED + NOT_EXECUTED
            required = dm.select_shard_pool.call_args_list[-1][0][2]
            self.assertEqual(required["ro.build.fingerprint"], self.FP)
            self.assertEqual(result.suites_results["cts"].status, "PASSED")
            self.assertEqual(result.suites_results["vts"].status, "PASSED")
            self.assertEqual(result.overall_status, "PASSED")
            self.assertTrue(state.data["complete"])
            self.assertTrue(partial.exists())

    def test_resume_after_complete_run_starts_fresh(self):
        from xts_agent.execution.tradefed_runner import ExecutionResult

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            plan, executor, dm, state = self._setup(tmp)
            state.start("cert", "certification")
            state.mark_complete("PASSED")
            with patch.object(
                TradefedRunner, "execute", return_value=ExecutionResult(True, None, 0, 1.0, None, "log")
            ) as exe:
                executor.execute_plan(plan, resume=True)
            self.assertEqual(exe.call_count, 2)
            self.assertTrue(all(c[0][0][1:3] == ["run", "commandAndExit"] for c in exe.call_args_list))


class ParallelSuiteTests(unittest.TestCase):
    def test_split_is_proportional_with_min_one_and_caps(self):
        split = ShardManager.split_devices(list(range(11)), {"CTS": 40, "VTS": 20, "STS": 8})
        self.assertEqual({k: len(v) for k, v in split.items()}, {"CTS": 7, "VTS": 3, "STS": 1})
        capped = ShardManager.split_devices(list(range(11)), {"CTS": 40, "CATBOX": 12}, {"CATBOX": 2})
        self.assertEqual(len(capped["CATBOX"]), 2)
        self.assertEqual(len(capped["CTS"]), 9)
        flat = [d for devs in split.values() for d in devs]
        self.assertEqual(sorted(flat), list(range(11)))  # each device used once
        with self.assertRaises(ValueError):
            ShardManager.split_devices([1], {"CTS": 1, "VTS": 1})

    def test_history_overrides_static_weight(self):
        mgr = ShardManager(MagicMock())
        self.assertEqual(mgr.suite_weight("CTS", lambda n: 123.0), 123.0)
        self.assertEqual(mgr.suite_weight("CTS", lambda n: None), 40.0)

    def test_estimate_device_hours_from_store(self):
        from xts_agent.results.result_store import ResultStore

        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(Path(tmp) / "db.sqlite")
            for hours, status in ((10, "FAILED"), (12, "PASSED"), (100, "INCOMPLETE"), (1, "DRY_RUN")):
                store.save_suite_run("p", "development", SuiteResult(
                    "CTS", status, 1, 1, 0, hours * 3600, 1, "", 0, device_serials=["d"] * 4))
            self.assertEqual(store.estimate_device_hours("CTS"), 48.0)  # median of 40/48/400
            self.assertIsNone(store.estimate_device_hours("VTS"))
            runs = store.recent_runs("cts")
            self.assertEqual((len(runs), runs[0]["status"], runs[0]["devices"]), (4, "DRY_RUN", ["d"] * 4))

    def test_scheduler_runs_concurrently_without_sharing_devices(self):
        import threading
        import time as _time

        from xts_agent.config_loader import DeviceRequirements, PathsConfig, TestPlanConfig
        from xts_agent.execution.tradefed_runner import ExecutionResult

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            suites = []
            for prio, name in enumerate(("cts", "vts", "sts"), start=1):
                tools = tmp / f"android-{name}" / "tools"
                tools.mkdir(parents=True)
                (tools / f"{name}-tradefed").write_text("#!/bin/sh\n", encoding="utf-8")
                from xts_agent.config_loader import ShardingConfig

                suites.append(SuiteConfig(name=name, plan=name, priority=prio,
                                          package_path=str(tmp / f"android-{name}"),
                                          sharding=ShardingConfig(shard_count="auto")))
            plan = TestPlanConfig(name="p", suites=suites, devices=DeviceRequirements(min_devices=1),
                                  paths=PathsConfig(), max_concurrent_suites=2)
            from xts_agent.device.device_manager import DeviceInfo, DeviceManager

            pool = [DeviceInfo(f"d{i}", "", "", "fp:user/k", 34, 100, "device", "aaos") for i in range(6)]
            dm = DeviceManager()
            dm.get_available_devices = MagicMock(return_value=pool)
            dm.select_shard_pool = MagicMock(return_value=pool)

            lock = threading.Lock()
            in_use, spans, overlap = set(), [], []
            running, peak = set(), []

            def fake_execute(runner_self, cmd, timeout_hours, log_dir, env=None):
                serials = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-s"]
                with lock:
                    overlap.extend(set(serials) & in_use)
                    in_use.update(serials)
                    running.add(cmd[3])
                    peak.append(len(running))
                start = _time.time()
                _time.sleep(0.2 if cmd[3] == "cts" else 0.05)
                with lock:
                    in_use.difference_update(serials)
                    running.discard(cmd[3])
                spans.append((cmd[3], start, _time.time(), serials))
                return ExecutionResult(True, None, 0, 0.1, None, "log")

            executor = TestPlanExecutor(plan, dm, ShardManager(dm), None,
                                        MagicMock(get_suite=MagicMock(return_value=None)),
                                        results_dir=tmp / "results")
            with patch.object(TradefedRunner, "execute", fake_execute):
                result = executor.execute_plan(plan)

        self.assertEqual(overlap, [])
        self.assertLessEqual(max(peak), 2)  # never above max_concurrent_suites
        by = {name: (start, end, serials) for name, start, end, serials in spans}
        self.assertLess(by["vts"][0], by["cts"][1])  # cts and vts overlapped
        self.assertGreaterEqual(by["sts"][0], by["vts"][1])  # sts waited for freed devices
        self.assertGreater(len(by["cts"][2]), len(by["vts"][2]))  # cts weighted heavier
        self.assertEqual(list(result.suites_results), ["cts", "vts", "sts"])
        self.assertEqual(dm._allocated, set())

    def test_concurrency_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "plan.yaml"
            p.write_text("name: t\nmax_concurrent_suites: 3\nsuites: []\n", encoding="utf-8")
            self.assertEqual(ConfigLoader(p).load_plan().max_concurrent_suites, 3)
        self.assertEqual(ConfigLoader("config/test_plans/cts_only.yaml").load_plan().max_concurrent_suites, 1)


if __name__ == "__main__":
    unittest.main()
