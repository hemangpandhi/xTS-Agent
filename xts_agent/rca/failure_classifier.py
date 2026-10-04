"""
from __future__ import annotations
Rule-based failure classification.
"""
from .rca_engine import FailureType
from ..results.result_parser import TestCaseResult

class FailureClassifier:
    def classify(self, test_case: TestCaseResult, logcat: str, device_state: dict) -> FailureType:
        msg = (test_case.message or "").lower()
        stack = (test_case.stack_trace or "").lower()
        
        if "device disconnected" in msg or "adb timeout" in msg or "adb connection" in msg:
            return FailureType.INFRASTRUCTURE_FAILURE
        if "outofmemory" in stack or "anr in" in logcat.lower():
            return FailureType.INFRASTRUCTURE_FAILURE
        if "java.lang.assertionerror" in stack and "test" in stack:
            return FailureType.TEST_BUG
        if "timeout" in msg:
            return FailureType.FLAKY_TEST
        if "network" in msg or "wifi" in msg:
            return FailureType.ENVIRONMENT_ISSUE
            
        return FailureType.PRODUCT_BUG
