"""Baseline comparison & regression detection."""

from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from .result_parser import TestCaseResult, TestResults


@dataclass
class ComparisonResult:
    new_failures: List[TestCaseResult] = field(default_factory=list)
    fixed: List[TestCaseResult] = field(default_factory=list)
    persistent_failures: List[TestCaseResult] = field(default_factory=list)
    new_tests: List[TestCaseResult] = field(default_factory=list)
    removed_tests: List[TestCaseResult] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=dict)


class ResultComparator:
    """Compares current results against a baseline."""

    def load_baseline(self, path: str | Path) -> TestResults:
        with open(path, "rb") as f:
            return pickle.load(f)

    def save_baseline(self, results: TestResults, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(results, f)

    def compare(self, current: TestResults, baseline: TestResults) -> ComparisonResult:
        def key(tc: TestCaseResult):
            return (tc.module, tc.class_name, tc.test_name)

        current_map = {}
        for mod in current.modules:
            for tc in mod.test_cases:
                current_map[key(tc)] = tc
        baseline_map = {}
        for mod in baseline.modules:
            for tc in mod.test_cases:
                baseline_map[key(tc)] = tc

        result = ComparisonResult()
        for k, tc in current_map.items():
            if k not in baseline_map:
                result.new_tests.append(tc)
                if tc.result == "FAIL":
                    result.new_failures.append(tc)
            else:
                prev = baseline_map[k]
                if tc.result == "FAIL" and prev.result != "FAIL":
                    result.new_failures.append(tc)
                elif tc.result != "FAIL" and prev.result == "FAIL":
                    result.fixed.append(tc)
                elif tc.result == "FAIL" and prev.result == "FAIL":
                    result.persistent_failures.append(tc)

        for k, tc in baseline_map.items():
            if k not in current_map:
                result.removed_tests.append(tc)

        result.summary = {
            "new_failures": len(result.new_failures),
            "fixed": len(result.fixed),
            "persistent_failures": len(result.persistent_failures),
            "new_tests": len(result.new_tests),
            "removed_tests": len(result.removed_tests),
        }
        return result
