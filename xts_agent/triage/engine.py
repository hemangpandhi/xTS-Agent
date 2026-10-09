"""Triage engine: failures -> groups enriched with history, known issues, owners."""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from xts_agent.results.result_parser import ResultParser

from .history import NO_HISTORY, FailureHistory, group_label
from .known_issues import KnownIssueDB, KnownIssueMatch
from .ownership import Owner, OwnershipMap
from .signature import FailureGroup, group_failures

logger = logging.getLogger(__name__)

MAX_TESTS_IN_REPORT = 50


@dataclass
class TriagedGroup:
    group: FailureGroup
    label: str
    label_counts: Dict[str, int]
    owner: Owner
    classification: str
    known: Optional[KnownIssueMatch] = None
    last_pass_build: str = ""
    first_fail_build: str = ""
    jira_key: str = ""
    jira_action: str = ""
    ai: Optional[Dict[str, Any]] = None

    @property
    def waived(self) -> bool:
        return bool(self.known and self.known.waived)

    @property
    def actionable(self) -> bool:
        """Needs a human: not waived and not already tracked in Jira."""
        return not self.waived and not self.jira_key

    def to_dict(self) -> Dict[str, Any]:
        g = self.group
        return {
            "signature": g.signature,
            "title": g.title,
            "exception": g.exception,
            "message": g.message,
            "frames": g.frames,
            "count": g.count,
            "suites": g.suites,
            "modules": g.modules,
            "tests": [t.test_id for t in g.tests[:MAX_TESTS_IN_REPORT]],
            "tests_truncated": max(0, g.count - MAX_TESTS_IN_REPORT),
            "sample_stack": g.sample_stack[:4000],
            "label": self.label,
            "label_counts": self.label_counts,
            "last_pass_build": self.last_pass_build,
            "first_fail_build": self.first_fail_build,
            "classification": self.classification,
            "owner": {"team": self.owner.team, "jira_component": self.owner.jira_component,
                      "assignee": self.owner.assignee},
            "known_issue": (
                {
                    "id": self.known.issue.id,
                    "title": self.known.issue.title,
                    "jira": self.known.issue.jira,
                    "waived": self.known.waived,
                    "waiver_expired": self.known.waiver_expired,
                }
                if self.known
                else None
            ),
            "actionable": self.actionable,
            "jira_key": self.jira_key,
            "jira_action": self.jira_action,
            "ai": self.ai,
        }


@dataclass
class TriageReport:
    plan_name: str
    profile: str
    fingerprint: str
    groups: List[TriagedGroup] = field(default_factory=list)

    @property
    def summary(self) -> Dict[str, Any]:
        return {
            "failures": sum(t.group.count for t in self.groups),
            "groups": len(self.groups),
            "actionable_groups": sum(1 for t in self.groups if t.actionable),
            "known_groups": sum(1 for t in self.groups if t.known),
            "waived_groups": sum(1 for t in self.groups if t.waived),
            "by_label": dict(Counter(t.label for t in self.groups)),
            "by_team": dict(Counter(t.owner.team for t in self.groups if t.actionable)),
            "ai_agreement": self.ai_agreement,
        }

    @property
    def ai_agreement(self) -> Dict[str, Any]:
        """AI vs human classification on known issues: the running eval set.

        Known issues carry a human-assigned classification; every time the AI
        analyses such a group we learn whether it agreed. Check this before
        trusting AI suggestions on unknown groups.
        """
        pairs = [
            (t.ai.get("classification"), t.known.issue.classification)
            for t in self.groups
            if t.ai and t.known and t.known.issue.classification
        ]
        agree = sum(1 for ai, human in pairs if ai == human)
        return {
            "evaluated": len(pairs),
            "agreed": agree,
            "rate": round(agree / len(pairs), 2) if pairs else None,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_name": self.plan_name,
            "profile": self.profile,
            "fingerprint": self.fingerprint,
            "summary": self.summary,
            "groups": [t.to_dict() for t in self.groups],
        }

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path


class TriageEngine:
    def __init__(
        self,
        history: Optional[FailureHistory] = None,
        known_issues: Optional[KnownIssueDB] = None,
        ownership: Optional[OwnershipMap] = None,
    ):
        self.history = history
        self.known_issues = known_issues or KnownIssueDB([])
        self.ownership = ownership or OwnershipMap([], Owner("xts-triage"))

    def triage(
        self,
        plan_result: Any,
        rca_report: Any = None,
        record_history: bool = True,
    ) -> TriageReport:
        parser = ResultParser()
        failures: List[Any] = []
        labels: Dict[str, Any] = {}
        fingerprint = ""
        for suite_name, suite_res in (plan_result.suites_results or {}).items():
            details = suite_res.details
            if details is None:
                continue
            fingerprint = fingerprint or details.device_info.get("build_fingerprint", "")
            failed = parser.get_failed_tests(details)
            failures.extend((suite_name, tc) for tc in failed)
            if self.history is not None:
                # classify against earlier runs *before* recording this one
                labels.update(
                    self.history.classify(suite_name, details, [(t.module, t.test_id) for t in failed])
                )
                if record_history:
                    self.history.record_run(
                        suite_name, details, plan=plan_result.plan_name,
                        results_dir=suite_res.results_dir or "",
                    )

        rca_classes = {
            f.test_id: f.classification.name for f in getattr(rca_report, "failures", None) or []
        }
        profile = getattr(plan_result, "profile", "development")
        report = TriageReport(plan_result.plan_name, profile, fingerprint)
        for group in group_failures(failures):
            test_labels = [labels[t.test_id] for t in group.tests if t.test_id in labels]
            label = group_label(h.label for h in test_labels) if test_labels else NO_HISTORY
            known = self.known_issues.match(group, profile, fingerprint)
            if known and known.issue.classification:
                classification = known.issue.classification
            else:
                votes = Counter(rca_classes.get(t.test_id, "PRODUCT_BUG") for t in group.tests)
                classification = votes.most_common(1)[0][0]
            new_tests = [h for h in test_labels if h.label == label]
            report.groups.append(
                TriagedGroup(
                    group=group,
                    label=label,
                    label_counts=dict(Counter(h.label for h in test_labels)),
                    owner=self.ownership.owner_for_group(group),
                    classification=classification,
                    known=known,
                    last_pass_build=next((h.last_pass_build for h in new_tests if h.last_pass_build), ""),
                    first_fail_build=next((h.first_fail_build for h in new_tests if h.first_fail_build), ""),
                    jira_key=known.issue.jira if known else "",
                )
            )
        logger.info("Triage: %s", report.summary)
        return report
