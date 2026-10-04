from __future__ import annotations
"""
from __future__ import annotations
Multi-session result merger.
"""
from typing import List, Dict, Any
from .result_parser import TestResults, ModuleResult, TestCaseResult
import copy

class ResultAggregator:
    """Aggregates multiple TradeFed test sessions."""
    
    def merge_results(self, results_list: List[TestResults]) -> TestResults:
        """Merges multiple sessions. Pass in ANY session -> final PASS."""
        if not results_list:
            raise ValueError("No results provided to merge.")
            
        base = copy.deepcopy(results_list[0])
        base_test_map = self._build_test_map(base)
        
        for res in results_list[1:]:
            res_map = self._build_test_map(res)
            for key, tc in res_map.items():
                if key not in base_test_map:
                    # add module if not exists
                    mod_name = key[0]
                    self._add_to_base(base, mod_name, tc)
                    base_test_map[key] = tc
                else:
                    if base_test_map[key].result != 'PASS' and tc.result == 'PASS':
                        base_test_map[key].result = 'PASS'
                        base_test_map[key].message = None
                        base_test_map[key].stack_trace = None
                        
        # Recompute summary
        self._recompute_summary(base)
        return base
        
    def _build_test_map(self, res: TestResults) -> Dict[tuple, TestCaseResult]:
        m = {}
        for mod in res.modules:
            for tc in mod.test_cases:
                m[(mod.name, tc.class_name, tc.test_name)] = tc
        return m
        
    def _add_to_base(self, base: TestResults, mod_name: str, tc: TestCaseResult):
        for mod in base.modules:
            if mod.name == mod_name:
                mod.test_cases.append(tc)
                return
        new_mod = ModuleResult(name=mod_name, done=True, pass_count=0, fail_count=0, runtime=0, test_cases=[tc])
        base.modules.append(new_mod)
        
    def _recompute_summary(self, res: TestResults):
        passes, fails, skips = 0, 0, 0
        for mod in res.modules:
            mp, mf = 0, 0
            for tc in mod.test_cases:
                if tc.result == 'PASS':
                    mp += 1
                elif tc.result in ('FAIL', 'ERROR'):
                    mf += 1
                else:
                    skips += 1
            mod.pass_count = mp
            mod.fail_count = mf
            passes += mp
            fails += mf
            
        res.summary = {"pass": passes, "fail": fails, "skip": skips, "error": 0}

    def aggregate_suite_results(self, suite_results: Dict[str, Any]) -> Any:
        pass
