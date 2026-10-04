from __future__ import annotations
"""
from __future__ import annotations
Baseline comparison & regression detection.
"""
from dataclasses import dataclass, field
from typing import List, Dict
import pickle
from pathlib import Path
from .result_parser import TestResults, TestCaseResult

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
        with open(path, 'rb') as f:
            return pickle.load(f)
            
    def save_baseline(self, results: TestResults, path: str | Path):
        with open(path, 'wb') as f:
            pickle.dump(results, f)
            
    def compare(self, current: TestResults, baseline: TestResults) -> ComparisonResult:
        comp = ComparisonResult()
        
        cur_map = {}
        for mod in current.modules:
            for tc in mod.test_cases:
                cur_map[f"{mod.name}::{tc.class_name}#{tc.test_name}"] = tc
                
        base_map = {}
        for mod in baseline.modules:
            for tc in mod.test_cases:
                base_map[f"{mod.name}::{tc.class_name}#{tc.test_name}"] = tc
                
        for k, cur_tc in cur_map.items():
            if k not in base_map:
                comp.new_tests.append(cur_tc)
                if cur_tc.result == 'FAIL':
                    comp.new_failures.append(cur_tc)
            else:
                base_tc = base_map[k]
                if base_tc.result == 'PASS' and cur_tc.result == 'FAIL':
                    comp.new_failures.append(cur_tc)
                elif base_tc.result == 'FAIL' and cur_tc.result == 'PASS':
                    comp.fixed.append(cur_tc)
                elif base_tc.result == 'FAIL' and cur_tc.result == 'FAIL':
                    comp.persistent_failures.append(cur_tc)
                    
        for k, base_tc in base_map.items():
            if k not in cur_map:
                comp.removed_tests.append(base_tc)
                
        comp.summary = {
            "new_failures": len(comp.new_failures),
            "fixed": len(comp.fixed),
            "persistent_failures": len(comp.persistent_failures),
            "new_tests": len(comp.new_tests),
            "removed_tests": len(comp.removed_tests)
        }
        return comp
