"""Allow-listed remediations the fix loop may apply.

The loop never runs commands an LLM made up. Every action it can take lives in
``config/remediations.yaml``, written and reviewed by the lab owner. A
remediation is chosen when its ``match`` patterns hit the failure text (rule)
or when the LLM picks its id from this catalog (advisory). ``detect`` lets a
remediation confirm the device really is in the broken state before acting.
"""

from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

SCOPES = ("device", "host_tool", "reboot", "retry")

# Values captured from failure text ({m1}..{m9}) are substituted into adb shell
# commands, so they must look like a package/component name and nothing else.
DEFAULT_CAPTURE_PATTERN = r"^[A-Za-z][A-Za-z0-9_.]{1,200}(/[A-Za-z0-9_.$]{1,200})?$"


class UnsafeCaptureError(ValueError):
    """A captured value did not pass the remediation's capture_pattern."""


@dataclass
class Remediation:
    id: str
    title: str
    category: str
    scope: str
    match: List[str]
    why: str = ""
    # Read-only device command (scope device) whose output shows the problem
    detect: str = ""
    broken_if: str = ""
    apply: List[str] = field(default_factory=list)
    revert: List[str] = field(default_factory=list)
    settle_secs: int = 0
    capture_pattern: str = DEFAULT_CAPTURE_PATTERN
    # scope host_tool: put a working copy of `tool` first on TradeFed's PATH
    tool: str = ""
    tool_candidates: List[str] = field(default_factory=list)
    verify_args: List[str] = field(default_factory=list)
    # Bake into device prep once proven (shown in the report)
    prep_hint: str = ""
    # A device-side change that proves a product root cause but is not the
    # shippable fix; permanent_fix says what must change in OEM code/config
    workaround: bool = False
    permanent_fix: str = ""
    references: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(f"remediation {self.id}: scope must be one of {SCOPES}")
        self._patterns = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in self.match]

    def find(self, text: str) -> Optional[re.Match]:
        for pattern in self._patterns:
            m = pattern.search(text)
            if m:
                return m
        return None

    def render(self, command: str, m: Optional[re.Match], variables: Optional[Dict[str, str]] = None) -> str:
        """Substitute {m1}..{m9} from the match and {var.NAME} from lab config.

        Captures come from failure text, so they are refused unless they pass
        ``capture_pattern``; lab variables come from reviewed config.
        """
        groups = m.groups() if m else ()
        variables = variables or {}

        def sub_capture(token: re.Match) -> str:
            idx = int(token.group(1))
            value = groups[idx - 1] if idx <= len(groups) else None
            if not value or not re.fullmatch(self.capture_pattern, value):
                raise UnsafeCaptureError(f"{self.id}: captured value {value!r} rejected for {command!r}")
            return value

        def sub_var(token: re.Match) -> str:
            name = token.group(1)
            if name not in variables:
                raise UnsafeCaptureError(f"{self.id}: lab variable {name!r} is not set (fix_loop.variables)")
            return str(variables[name])

        command = re.sub(r"\{var\.([A-Za-z0-9_]+)\}", sub_var, command)
        return re.sub(r"\{m([1-9])\}", sub_capture, command)

    def summary(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "category": self.category, "scope": self.scope}


def _expand(path: str) -> List[str]:
    expanded = os.path.expandvars(os.path.expanduser(path))
    # Newest build-tools first: sort globbed dirs descending
    return sorted(glob.glob(expanded), reverse=True) if any(c in expanded for c in "*?[") else [expanded]


def resolve_tool_dirs(rem: Remediation) -> List[str]:
    """Candidate directories holding an executable ``rem.tool``."""
    dirs: List[str] = []
    for cand in rem.tool_candidates:
        for d in _expand(cand):
            exe = Path(d) / rem.tool
            if exe.is_file() and os.access(exe, os.X_OK) and d not in dirs:
                dirs.append(d)
    return dirs


class RemediationCatalog:
    def __init__(self, remediations: List[Remediation]):
        ids = [r.id for r in remediations]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate remediation ids: {sorted(dupes)}")
        self.remediations = remediations
        self.by_id = {r.id: r for r in remediations}

    @classmethod
    def load(cls, path: str | Path) -> "RemediationCatalog":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        items = data.get("remediations") or []
        known = set(Remediation.__dataclass_fields__)
        rems = []
        for item in items:
            unknown = set(item) - known
            if unknown:
                logger.warning("remediation %s: ignoring unknown keys %s", item.get("id"), sorted(unknown))
            rems.append(Remediation(**{k: v for k, v in item.items() if k in known}))
        return cls(rems)

    def matching(self, text: str) -> List[tuple]:
        """[(remediation, match)] in catalog order for every remediation whose pattern hits."""
        out = []
        for rem in self.remediations:
            m = rem.find(text)
            if m:
                out.append((rem, m))
        return out
