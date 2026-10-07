"""Shared fixtures for the test modules.

Every test module imports ``setUpModule``/``tearDownModule`` from here so
device leases go to a temp dir and executor tests never reach real adb.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from xts_agent.execution.test_plan_executor import TestPlanExecutor

SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='no' ?>
<Result start_display="now" end_display="later" suite_name="CTS">
  <Build device_serial="emulator-5554" />
  <Summary pass="2" failed="1" skipped="2" modules_done="3" modules_total="3" />
  <Module name="CtsSample" done="true" pass="2" fail="1" runtime="10">
    <TestCase name="com.example.Foo">
      <Test result="pass" name="testA" />
      <Test result="pass" name="testB" />
      <Test result="fail" name="testC">
        <Failure message="boom">
          <StackTrace>java.lang.NullPointerException
at com.example.Foo.testC</StackTrace>
        </Failure>
      </Test>
      <Test result="IGNORED" name="testD" />
      <Test result="ASSUMPTION_FAILURE" name="testE">
        <Failure message="got: &lt;false&gt;, expected: is &lt;true&gt;">
          <StackTrace>org.junit.AssumptionViolatedException: got: false
at org.junit.Assume.assumeTrue(Assume.java:50)</StackTrace>
        </Failure>
      </Test>
    </TestCase>
  </Module>
</Result>
"""


_LEASE_TMP = None


_PREP_PATCH = None


_SURVIVAL_PATCH = None


def setUpModule():
    # Keep device lease files out of the real host-wide lease dir
    import os

    global _LEASE_TMP
    _LEASE_TMP = tempfile.TemporaryDirectory()
    # Subdir, so the ledger (stored next to lease_dir) also stays in the temp dir
    os.environ["XTS_LEASE_DIR"] = str(Path(_LEASE_TMP.name) / "leases")
    # Executor tests use fake serials: never send prep commands to real adb or
    # probe real adb for post-suite device survival
    global _PREP_PATCH, _SURVIVAL_PATCH
    _PREP_PATCH = patch("xts_agent.execution.test_plan_executor.DevicePreparer")
    _PREP_PATCH.start()
    _SURVIVAL_PATCH = patch.object(TestPlanExecutor, "_record_device_survival")
    _SURVIVAL_PATCH.start()


def tearDownModule():
    import os

    os.environ.pop("XTS_LEASE_DIR", None)
    _LEASE_TMP.cleanup()
    _PREP_PATCH.stop()
    _SURVIVAL_PATCH.stop()


PASSING_XML = """<?xml version='1.0' encoding='UTF-8' standalone='no' ?>
<Result suite_name="CTS">
  <Summary pass="1" failed="0" modules_done="1" modules_total="1" />
  <Module name="CtsSample" abi="x86_64" done="true" pass="1" runtime="10">
    <TestCase name="com.example.Foo"><Test result="pass" name="testA" /></TestCase>
  </Module>
</Result>
"""


# Shape of the real interrupted run 2026.10.04_16.16.31.269_4285 (421 of 1251)
INCOMPLETE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='no' ?>
<Result suite_name="CTS">
  <Summary pass="161222" failed="0" modules_done="421" modules_total="1251" />
  <Module name="CtsSample" abi="x86_64" done="false" pass="1" runtime="10">
    <TestCase name="com.example.Foo"><Test result="pass" name="testA" /></TestCase>
  </Module>
</Result>
"""
