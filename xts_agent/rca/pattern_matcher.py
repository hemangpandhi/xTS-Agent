"""Known failure pattern matching."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml

from .rca_engine import FailureType

logger = logging.getLogger(__name__)


@dataclass
class FailurePattern:
    pattern: str
    classification: FailureType
    description: str
    known_bug_id: Optional[str] = None
    suggested_action: str = ""


class PatternMatcher:
    def __init__(self, patterns_file: str | Path | None = None):
        self.patterns_file = Path(patterns_file) if patterns_file else None
        self.patterns = self.load_patterns()

    def load_patterns(self) -> List[FailurePattern]:
        builtins = [
            FailurePattern(
                r"NullPointerException",
                FailureType.PRODUCT_BUG,
                "NPE encountered",
                suggested_action="Inspect NPE stack for framework null dereference",
            ),
            FailurePattern(
                r"device not found",
                FailureType.INFRASTRUCTURE_FAILURE,
                "Device Offline",
                suggested_action="Check ADB connection and reboot device",
            ),
        ]
        if not self.patterns_file or not self.patterns_file.exists():
            return builtins

        try:
            with open(self.patterns_file, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            loaded: List[FailurePattern] = []
            for entry in data.get("patterns", []):
                cls_name = str(entry.get("classification", "PRODUCT_BUG")).upper()
                classification = (
                    FailureType[cls_name]
                    if cls_name in FailureType.__members__
                    else FailureType.PRODUCT_BUG
                )
                loaded.append(
                    FailurePattern(
                        pattern=entry.get("pattern", ""),
                        classification=classification,
                        description=entry.get("description", ""),
                        known_bug_id=entry.get("known_bug_id") or None,
                        suggested_action=entry.get("suggested_action", ""),
                    )
                )
            logger.info("Loaded %s RCA patterns from %s", len(loaded), self.patterns_file)
            return loaded + builtins
        except Exception as exc:
            logger.error("Failed to load patterns file: %s", exc)
            return builtins

    def match(self, error_message: str, stack_trace: str) -> Optional[FailurePattern]:
        text = f"{error_message or ''} {stack_trace or ''}"
        for p in self.patterns:
            if not p.pattern:
                continue
            try:
                if re.search(p.pattern, text, re.IGNORECASE):
                    return p
            except re.error:
                logger.warning("Invalid RCA regex skipped: %s", p.pattern)
        return None
