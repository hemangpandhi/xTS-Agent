"""Multi-session result merger."""

from __future__ import annotations

import copy
from typing import Any, Dict, List

from .result_parser import ModuleResult, TestCaseResult, TestResults


class ResultAggregator:
    """Aggregates multiple TradeFed test sessions (PASS wins across retries)."""

    def merge_results(self, results_list: List[TestResults]) -> TestResults:
        if not results_list:
            raise ValueError("No results provided to merge.")

        base = copy.deepcopy(results_list[0])
        base_test_map = self._build_test_map(base)

        for res in results_list[1:]:
            res_map = self._build_test_map(res)
            for key, tc in res_map.items():
                if key not in base_test_map:
                    self._add_to_base(base, key[0], tc)
                    base_test_map[key] = tc
                else:
                    if base_test_map[key].result != "PASS" and tc.result == "PASS":
                        base_test_map[key].result = "PASS"
                        base_test_map[key].message = None
                        base_test_map[key].stack_trace = None
                        base_test_map[key].rca_category = None
                        base_test_map[key].rca_recommendation = None

        self._recompute_summary(base)
        return base

    def _build_test_map(self, res: TestResults) -> Dict[tuple, TestCaseResult]:
        mapping: Dict[tuple, TestCaseResult] = {}
        for mod in res.modules:
            for tc in mod.test_cases:
                mapping[(mod.name, tc.class_name, tc.test_name)] = tc
        return mapping

    def _add_to_base(self, base: TestResults, mod_name: str, tc: TestCaseResult):
        for mod in base.modules:
            if mod.name == mod_name:
                mod.test_cases.append(tc)
                return
        new_mod = ModuleResult(
            name=mod_name,
            done=True,
            pass_count=0,
            fail_count=0,
            runtime=0,
            test_cases=[tc],
        )
        base.modules.append(new_mod)

    def _recompute_summary(self, res: TestResults):
        passes = fails = skips = errors = 0
        for mod in res.modules:
            mp = mf = 0
            for tc in mod.test_cases:
                if tc.result == "PASS":
                    mp += 1
                    passes += 1
                elif tc.result == "FAIL":
                    mf += 1
                    fails += 1
                elif tc.result == "ERROR":
                    errors += 1
                else:
                    skips += 1
            mod.pass_count = mp
            mod.fail_count = mf

        res.summary = {
            "pass": passes,
            "fail": fails,
            "skip": skips,
            "error": errors,
        }

    def aggregate_suite_results(self, suite_results: Dict[str, Any]) -> Dict[str, int]:
        return {
            "pass": sum(getattr(s, "pass_count", 0) for s in suite_results.values()),
            "fail": sum(getattr(s, "fail_count", 0) for s in suite_results.values()),
            "skip": sum(getattr(s, "skip_count", 0) for s in suite_results.values()),
        }
