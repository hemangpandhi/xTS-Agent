"""Rule-based failure classification.

Only unambiguous infrastructure/environment signals are classified here.
Anything else returns ``None`` and stays PRODUCT_BUG (the safe default for
certification): assertion failures, crashes and timeouts in CTS are how the
suite reports wrong product behavior, so they must not be filed as test bugs
or infra noise. TEST_BUG / FLAKY_TEST come from curated patterns or history.
"""

from __future__ import annotations

from typing import Optional

from xts_agent.results.result_parser import TestCaseResult

from .rca_engine import FailureType

INFRA_SIGNALS = (
    "devicenotavailableexception",
    "deviceunresponsiveexception",
    "shellcommandunresponsiveexception",
    "device disconnected",
    "device offline",
    "device unresponsive",
    "adb timeout",
    "adb connection",
    "connection reset by peer",
)

ENVIRONMENT_SIGNALS = (
    "install_failed_insufficient_storage",
    "unknownhostexception",
    "no internet connection",
    "not connected to wifi",
)


class FailureClassifier:
    def classify(
        self, test_case: TestCaseResult, logcat: str, device_state: dict
    ) -> Optional[FailureType]:
        text = f"{test_case.message or ''}\n{test_case.stack_trace or ''}".lower()

        if device_state.get("offline") or any(s in text for s in INFRA_SIGNALS):
            return FailureType.INFRASTRUCTURE_FAILURE
        if any(s in text for s in ENVIRONMENT_SIGNALS):
            return FailureType.ENVIRONMENT_ISSUE
        return None
