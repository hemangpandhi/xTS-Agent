from __future__ import annotations
"""
from __future__ import annotations
Result processing module for xTS Agent.
"""
from .result_parser import ResultParser, TestResults, ModuleResult, TestCaseResult
from .result_aggregator import ResultAggregator
from .result_comparator import ResultComparator, ComparisonResult
from .result_store import ResultStore

__all__ = [
    'ResultParser',
    'TestResults',
    'ModuleResult',
    'TestCaseResult',
    'ResultAggregator',
    'ResultComparator',
    'ComparisonResult',
    'ResultStore'
]
