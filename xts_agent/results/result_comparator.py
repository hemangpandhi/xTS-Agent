"""Baseline comparison & regression detection."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List

from .result_parser import ModuleResult, ResultParser, TestCaseResult, TestResults


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
        """Load a baseline from a TradeFed test_result.xml or a JSON snapshot.

        Pickle is deliberately unsupported: unpickling a shared or downloaded
        baseline file can execute arbitrary code.
        """
        path = Path(path)
        if path.suffix == ".xml":
            return ResultParser().parse_xml(path)
        if path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            modules = [
                ModuleResult(
                    **{k: v for k, v in m.items() if k != "test_cases"},
                    test_cases=[TestCaseResult(**tc) for tc in m.get("test_cases", [])],
                )
                for m in data.pop("modules", [])
            ]
            return TestResults(**data, modules=modules)
        raise ValueError(f"Unsupported baseline format {path.suffix!r}: use .xml or .json")

    def save_baseline(self, results: TestResults, path: str | Path):
        path = Path(path)
        if path.suffix != ".json":
            raise ValueError("Baselines are saved as .json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(results)), encoding="utf-8")

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
