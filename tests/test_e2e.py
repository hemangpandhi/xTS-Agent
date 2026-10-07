"""End to end: the real CLI drives a fake ``cts-tradefed`` and a fake ``adb``.

Everything else is real: config loading, device discovery and leases, the
TradeFed subprocess, results-dir detection, session lookup via ``list
results``, suite retry, parsing, triage, the results database and reports.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from tests import support

setUpModule = support.setUpModule
tearDownModule = support.tearDownModule

REPO = Path(__file__).resolve().parents[1]
FINGERPRINT = "oem/car_hu/hu:16/AP4A/1:userdebug/dev-keys"

FAKE_ADB = r"""#!/bin/sh
case "$*" in
  "devices -l") printf 'List of devices attached\nhu-1           device product:car_hu model:HU transport_id:1\n' ;;
  "devices") printf 'List of devices attached\nhu-1\tdevice\n' ;;
  *"shell getprop sys.boot_completed") echo 1 ;;
  *"shell getprop init.svc.bootanim") echo stopped ;;
  *"shell getprop") printf '[ro.build.fingerprint]: [%s]\n[ro.build.version.sdk]: [36]\n[ro.build.type]: [userdebug]\n[ro.product.model]: [HU]\n[ro.hardware]: [oemhw]\n' "$XTS_FAKE_FP" ;;
  *"dumpsys battery") printf 'Current Battery Service state:\n  present: false\n  level: 100\n' ;;
  *"dumpsys connectivity") printf 'Active default network: 100\n  NetworkAgentInfo{network{100} Capabilities: INTERNET&VALIDATED}\n' ;;
  *"df /data") printf 'Filesystem 1K-blocks Used Available Use%% Mounted on\n/dev/block/dm-5 10000000 1000 9000000 1%% /data\n' ;;
  *mWakefulness*) echo 'mWakefulness=Awake' ;;
  *"pm list features") echo 'feature:android.hardware.type.automotive' ;;
esac
exit 0
"""

# First run: one failure. `run retry`: TradeFed re-runs the failure and writes
# a new session holding the merged result, which now passes.
FAKE_TRADEFED = textwrap.dedent('''\
    #!{python}
    import json, sys
    from pathlib import Path

    suite = Path(__file__).resolve().parents[1]
    results = suite / "results"
    args = sys.argv[1:]
    with open(suite / "calls.jsonl", "a") as fh:
        fh.write(json.dumps(args) + "\\n")
    sessions = sorted(d.name for d in results.glob("2026.*") if d.is_dir()) if results.is_dir() else []
    if args[:2] == ["list", "results"]:
        print("Session  Pass  Fail  Modules Complete  Result Directory")
        for i, name in enumerate(sessions):
            print(f"{{i}}        1     0     1 of 1            {{name}}")
        sys.exit(0)
    retry = args[:2] == ["run", "retry"]
    n = len(sessions) + 1
    session = results / f"2026.10.07_06.00.0{{n}}.000_{{n}}"
    session.mkdir(parents=True)
    test_b = "pass" if retry else "fail"
    failure = "" if retry else (
        '<Failure message="java.lang.AssertionError: expected 1"><StackTrace>java.lang.AssertionError: '
        'expected 1&#10;\\tat com.oem.car.cts.HvacTest.testFanSpeed(HvacTest.java:42)</StackTrace></Failure>')
    fails = 0 if retry else 1
    (session / "test_result.xml").write_text(f"""<?xml version='1.0' encoding='UTF-8' standalone='no' ?>
    <Result start="1791345600000" end="1791349200000" suite_name="CTS" devices="hu-1">
      <Build build_fingerprint="{fp}" device_serial="hu-1" />
      <Summary pass="{{2 - fails}}" failed="{{fails}}" modules_done="1" modules_total="1" />
      <Module name="CtsCarHvacTestCases" abi="arm64-v8a" done="true" pass="{{2 - fails}}">
        <TestCase name="com.oem.car.cts.HvacTest">
          <Test result="pass" name="testPower" />
          <Test result="{{test_b}}" name="testFanSpeed">{{failure}}</Test>
        </TestCase>
      </Module>
    </Result>
    """)
    print("I/ITestSuite: hu-1 running 1 modules: [arm64-v8a CtsCarHvacTestCases]")
    print(f"RESULT DIRECTORY            : {{session}}")
    sys.exit(0)
''').format(python=sys.executable, fp=FINGERPRINT)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "adb").write_text(FAKE_ADB, encoding="utf-8")
        tools = self.tmp / "xts" / "android-cts" / "tools"
        tools.mkdir(parents=True)
        (tools / "cts-tradefed").write_text(FAKE_TRADEFED, encoding="utf-8")
        for script in (bin_dir / "adb", tools / "cts-tradefed"):
            script.chmod(0o755)

        defaults = yaml.safe_load((REPO / "config" / "default_config.yaml").read_text(encoding="utf-8"))
        out = self.tmp / "out"
        defaults["agent"].update(results_dir=str(out), log_dir=str(out / "logs"),
                                 database_path=str(out / "xts.db"))
        defaults["paths"]["xts_packages_dir"] = str(self.tmp / "xts")
        defaults["device"]["lease_dir"] = str(self.tmp / "leases")
        defaults.setdefault("ops", {})["min_free_disk_gb"] = 0
        self.defaults = self.tmp / "defaults.yaml"
        self.defaults.write_text(yaml.safe_dump(defaults), encoding="utf-8")
        self.plan = self.tmp / "plan.yaml"
        self.plan.write_text(yaml.safe_dump({
            "name": "E2E CTS",
            "profile": "development",
            "devices": {"min_devices": 1, "prepare": False},
            "suites": [{"name": "cts", "plan": "cts", "priority": 1,
                        "include_filters": ["CtsCarHvacTestCases"], "retry": {"max_retries": 1}}],
            "post_execution": {"suite_retry": {"enabled": True, "max_suite_retries": 1,
                                               "retry_type": "FAILED", "cooldown_secs": 0}},
        }), encoding="utf-8")
        self.out = out
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                        XTS_FAKE_FP=FINGERPRINT, XTS_LEASE_DIR=str(self.tmp / "leases"),
                        XTS_RUN_ID="e2e", PYTHONPATH=str(REPO))
        self.env.pop("CI", None)

    def tearDown(self):
        self._tmp.cleanup()

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "xts_agent.cli", *args, "--plan", str(self.plan), "--config", str(self.defaults)],
            cwd=REPO, env=self.env, capture_output=True, text=True, timeout=120, check=False,
        )

    def test_run_retry_report(self):
        proc = self._cli("run", "--auto-retry")
        log = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, log[-3000:])

        calls = [json.loads(line) for line in
                 (self.tmp / "xts" / "android-cts" / "calls.jsonl").read_text().splitlines()]
        run = next(c for c in calls if c[:2] == ["run", "commandAndExit"])
        self.assertEqual(run[2], "cts")
        self.assertIn("CtsCarHvacTestCases", run)
        self.assertEqual(run[run.index("-s") + 1], "hu-1")
        # Retry targets session 0 (the first run) and only its failures, on the same device
        retry = next(c for c in calls if c[:2] == ["run", "retry"])
        self.assertEqual(retry[retry.index("--retry") + 1], "0")
        self.assertEqual(retry[retry.index("--retry-type") + 1], "FAILED")
        self.assertEqual(retry[retry.index("-s") + 1], "hu-1")

        reports = sorted((self.out / "reports").glob("xts_report_*.json"))
        self.assertTrue(reports, log[-3000:])
        report = json.loads(reports[-1].read_text())
        self.assertEqual(report["status"], "PASSED", log[-3000:])
        suite = report["suites"]["cts"]
        self.assertEqual((suite["pass_count"], suite["fail_count"], suite["retry_count"]), (2, 0, 1))
        self.assertTrue(suite["results_dir"].endswith("2026.10.07_06.00.02.000_2"))
        self.assertTrue(list((self.out / "junit").glob("*.xml")))
        html = next((self.out / "reports").glob("xts_report_*.html")).read_text()
        self.assertIn("E2E CTS", html)

        # Leases released, run recorded for trends, log lines tagged with the run id
        import fcntl
        import sqlite3

        for lock in (self.tmp / "leases").glob("*.lock"):  # files stay; the flock must be free
            with open(lock, "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with sqlite3.connect(self.out / "xts.db") as db:
            rows = db.execute("SELECT suite, status, pass_count, fail_count FROM suite_runs").fetchall()
        self.assertEqual(rows, [("CTS", "PASSED", 2, 0)])  # suite name as TradeFed reports it
        self.assertIn("[e2e cts]", log)

    def test_failure_without_retry_is_reported_and_triaged(self):
        plan = yaml.safe_load(self.plan.read_text())
        plan["post_execution"]["suite_retry"]["enabled"] = False  # otherwise on even without --auto-retry
        self.plan.write_text(yaml.safe_dump(plan), encoding="utf-8")
        proc = self._cli("run")
        log = proc.stdout + proc.stderr
        report = json.loads(sorted((self.out / "reports").glob("xts_report_*.json"))[-1].read_text())
        self.assertEqual(report["status"], "FAILED", log[-3000:])
        triage = json.loads(sorted((self.out / "triage").glob("triage_*.json"))[-1].read_text())
        self.assertEqual(triage["summary"]["failures"], 1)
        self.assertIn("HvacTest", json.dumps(triage["groups"][0]))
        # Exit code tells CI the run failed
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
