"""Failure signatures: collapse many failing tests into a few root causes.

A signature combines the exception type, the first message line with
volatile parts (numbers, ids, component names, hashes) normalised away, and
the first few stack frames *above* the failing test's own class, skipping
assertion/reflection plumbing. Tests that fail through the same helper with
the same error land in one group, even across modules. On a real CTS run
this reduced 1303 failures to 183 groups (413 of them one focus issue).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from xts_agent.results.result_parser import TestCaseResult

SIGNATURE_VERSION = 1

_SKIP_FRAME_PREFIXES = (
    "org.junit.",
    "junit.framework.",
    "com.google.common.truth.",
    "java.lang.reflect.",
    "jdk.internal.",
    "sun.reflect.",
    "androidx.test.",
    "android.test.",
    "org.mockito.",
)
_FRAME = re.compile(r"^\s*at\s+([\w$.]+)\.([\w$<>]+)\(")
_NORMALIZERS = (
    (re.compile(r"ComponentInfo\{[^}]*\}"), "<component>"),
    (re.compile(r"Intent \{[^}]*\}"), "<intent>"),
    (re.compile(r"\b[a-zA-Z_][\w.]*/[\w.$]+"), "<component>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    # hash-like tokens (window ids etc.): hex with both digits and letters
    (re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{5,}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"(['\"]).*?\1"), "<str>"),
    (re.compile(r"\s+"), " "),
)


def normalize_message(message: str, limit: int = 160) -> str:
    text = (message or "").split("\n", 1)[0]
    for pattern, repl in _NORMALIZERS:
        text = pattern.sub(repl, text)
    return text.strip()[:limit]


def signature_parts(tc: TestCaseResult, depth: int = 3) -> Tuple[str, str, List[str]]:
    """(exception type, normalised message, distinguishing frames)."""
    stack = tc.stack_trace or ""
    first = stack.splitlines()[0] if stack.strip() else (tc.message or "")
    exc, _, msg = first.partition(":")
    if not msg and " " in exc.strip():
        # Message without an exception prefix (e.g. bare TradeFed error text)
        exc, msg = "", first
    test_class = (tc.class_name or "").split("$")[0]
    frames: List[str] = []
    for line in stack.splitlines()[1:]:
        if line.strip().startswith("Caused by"):
            break
        match = _FRAME.match(line)
        if not match:
            continue
        cls, method = match.groups()
        if cls.startswith(_SKIP_FRAME_PREFIXES):
            continue
        if cls.split("$")[0] == test_class:
            # Reached the test itself: below here every test differs
            if not frames:
                frames.append(test_class)
            break
        frames.append(f"{cls}.{method}")
        if len(frames) >= depth:
            break
    return exc.strip(), normalize_message(msg), frames


def compute_signature(tc: TestCaseResult) -> str:
    exc, msg, frames = signature_parts(tc)
    key = "|".join([f"v{SIGNATURE_VERSION}", exc, msg, *frames])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


@dataclass
class FailureGroup:
    signature: str
    exception: str
    message: str
    frames: List[str]
    tests: List[TestCaseResult] = field(default_factory=list)
    suites: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.tests)

    @property
    def modules(self) -> List[str]:
        return sorted({t.module.split(" ")[-1] for t in self.tests if t.module})

    @property
    def title(self) -> str:
        head = self.exception.rsplit(".", 1)[-1] or "Failure"
        return f"{head}: {self.message}" if self.message else head

    @property
    def sample_stack(self) -> str:
        return next((t.stack_trace for t in self.tests if t.stack_trace), "") or ""


def group_failures(
    failures: Iterable[Tuple[str, TestCaseResult]],
) -> List[FailureGroup]:
    """Group (suite_name, failed test) pairs by signature, largest group first."""
    groups: Dict[str, FailureGroup] = {}
    for suite_name, tc in failures:
        sig = compute_signature(tc)
        group = groups.get(sig)
        if group is None:
            exc, msg, frames = signature_parts(tc)
            group = groups[sig] = FailureGroup(sig, exc, msg, frames)
        group.tests.append(tc)
        if suite_name not in group.suites:
            group.suites.append(suite_name)
    return sorted(groups.values(), key=lambda g: (-g.count, g.signature))


def find_group(groups: Iterable[FailureGroup], signature: str) -> Optional[FailureGroup]:
    return next((g for g in groups if g.signature == signature), None)
