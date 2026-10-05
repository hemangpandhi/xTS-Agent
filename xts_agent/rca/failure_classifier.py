"""Rule-based failure classification."""

from __future__ import annotations

from xts_agent.results.result_parser import TestCaseResult

from .rca_engine import FailureType


class FailureClassifier:
    def classify(
        self, test_case: TestCaseResult, logcat: str, device_state: dict
    ) -> FailureType:
        msg = (test_case.message or "").lower()
        stack = (test_case.stack_trace or "").lower()
        log = (logcat or "").lower()

        if any(
            s in msg
            for s in (
                "device disconnected",
                "adb timeout",
                "adb connection",
                "not found",
                "device unresponsive",
            )
        ):
            return FailureType.INFRASTRUCTURE_FAILURE
        if "outofmemory" in stack or "anr in" in log or "fatal exception" in log:
            return FailureType.INFRASTRUCTURE_FAILURE
        if "install_failed" in msg or "unknownhost" in stack:
            return FailureType.ENVIRONMENT_ISSUE
        if "java.lang.assertionerror" in stack and "test" in stack:
            return FailureType.TEST_BUG
        if "flaky" in msg or ("timeout" in msg and "intermittent" in msg):
            return FailureType.FLAKY_TEST
        if "timeout" in msg:
            return FailureType.FLAKY_TEST
        if "network" in msg or "wifi" in msg:
            return FailureType.ENVIRONMENT_ISSUE

        if device_state.get("offline"):
            return FailureType.INFRASTRUCTURE_FAILURE

        return FailureType.PRODUCT_BUG
