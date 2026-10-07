"""Known issues and time-boxed waivers, matched against failure groups.

Waivers are applied at report time only: they never change TradeFed
results or suite status. A waived group is shown as such and is not
"actionable" (no new Jira ticket), and every waiver must expire, so a
temporary exception cannot silently become permanent. Certification runs
only honour waivers that explicitly list the ``certification`` profile.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

import yaml

from .signature import FailureGroup

logger = logging.getLogger(__name__)


@dataclass
class Waiver:
    reason: str
    expires: _dt.date
    builds_regex: str = ""
    profiles: List[str] = field(default_factory=lambda: ["development"])

    def applies(self, profile: str, fingerprint: str, today: _dt.date) -> bool:
        if today > self.expires:
            return False
        if profile not in self.profiles:
            return False
        return not self.builds_regex or re.search(self.builds_regex, fingerprint or "") is not None


@dataclass
class KnownIssue:
    id: str
    title: str
    jira: str = ""
    classification: str = ""
    signatures: List[str] = field(default_factory=list)
    message_regex: str = ""
    test_regex: str = ""
    module_regex: str = ""
    waiver: Optional[Waiver] = None

    def matches(self, group: FailureGroup) -> bool:
        if self.signatures and group.signature not in self.signatures:
            return False
        if self.message_regex:
            sample = group.tests[0] if group.tests else None
            text = f"{sample.message or ''}\n{sample.stack_trace or ''}" if sample else group.message
            if not re.search(self.message_regex, text, re.IGNORECASE):
                return False
        if self.test_regex and not any(re.search(self.test_regex, t.test_id) for t in group.tests):
            return False
        if self.module_regex and not any(re.search(self.module_regex, m) for m in group.modules):
            return False
        return bool(self.signatures or self.message_regex or self.test_regex or self.module_regex)


@dataclass
class KnownIssueMatch:
    issue: KnownIssue
    waived: bool
    waiver_expired: bool = False


def _as_date(value: Any) -> _dt.date:
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(str(value))


class KnownIssueDB:
    def __init__(self, issues: List[KnownIssue]):
        self.issues = issues

    @classmethod
    def load(cls, path: str | Path) -> "KnownIssueDB":
        path = Path(path)
        if not path.exists():
            logger.info("No known-issues file at %s", path)
            return cls([])
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        issues = []
        for raw in data.get("issues") or []:
            try:
                match = raw.get("match") or {}
                sigs = match.get("signature") or []
                waiver = None
                if raw.get("waiver"):
                    w = raw["waiver"]
                    if not w.get("expires"):
                        raise ValueError("waiver requires an 'expires' date")
                    waiver = Waiver(
                        reason=str(w.get("reason", "")),
                        expires=_as_date(w["expires"]),
                        builds_regex=str(w.get("builds_regex", "")),
                        profiles=list(w.get("profiles") or ["development"]),
                    )
                issues.append(
                    KnownIssue(
                        id=str(raw["id"]),
                        title=str(raw.get("title", "")),
                        jira=str(raw.get("jira", "") or ""),
                        classification=str(raw.get("classification", "") or "").upper(),
                        signatures=[sigs] if isinstance(sigs, str) else list(sigs),
                        message_regex=str(match.get("message_regex", "") or ""),
                        test_regex=str(match.get("test_regex", "") or ""),
                        module_regex=str(match.get("module_regex", "") or ""),
                        waiver=waiver,
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                logger.error("Skipping invalid known issue %r in %s: %s", raw.get("id"), path, exc)
        logger.info("Loaded %s known issue(s) from %s", len(issues), path)
        return cls(issues)

    def match(
        self,
        group: FailureGroup,
        profile: str,
        fingerprint: str,
        today: Optional[_dt.date] = None,
    ) -> Optional[KnownIssueMatch]:
        today = today or _dt.date.today()
        for issue in self.issues:
            if not issue.matches(group):
                continue
            waiver = issue.waiver
            waived = bool(waiver and waiver.applies(profile, fingerprint, today))
            expired = bool(waiver and today > waiver.expires)
            if waiver and expired:
                logger.warning("Waiver for %s expired on %s", issue.id, waiver.expires)
            return KnownIssueMatch(issue, waived=waived, waiver_expired=expired)
        return None
