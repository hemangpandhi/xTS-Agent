"""
from __future__ import annotations
Known failure pattern matching.
"""
import re
from dataclasses import dataclass
from typing import List, Optional
from .rca_engine import FailureType
from pathlib import Path

@dataclass
class FailurePattern:
    pattern: str
    classification: FailureType
    description: str
    known_bug_id: Optional[str] = None

class PatternMatcher:
    def __init__(self, patterns_file: str | Path = None):
        self.patterns_file = patterns_file
        self.patterns = self.load_patterns()
        
    def load_patterns(self) -> List[FailurePattern]:
        return [
            FailurePattern(r"NullPointerException", FailureType.PRODUCT_BUG, "NPE encountered"),
            FailurePattern(r"device not found", FailureType.INFRASTRUCTURE_FAILURE, "Device Offline")
        ]
        
    def match(self, error_message: str, stack_trace: str) -> Optional[FailurePattern]:
        text = f"{error_message or ''} {stack_trace or ''}"
        for p in self.patterns:
            if re.search(p.pattern, text):
                return p
        return None
