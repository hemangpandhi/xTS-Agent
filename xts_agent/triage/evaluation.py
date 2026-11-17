"""Measured triage accuracy against held-out human labels.

The agreement figure on the triage page compares the AI with the known
issues of the same run, which were often written from that run: a
consistency check, not accuracy. This module scores the rule classifier
and the AI against labels that come from somewhere else (closed Jira
tickets with a confirmed root cause, a reviewer's sheet) and that the
triage pass never saw: known issues are disabled while scoring so neither
the prompt nor the grouping can read the answer.

Labels use the known-issues schema (``issues:`` with ``classification`` and
a ``match`` block) so a ticket export and a known-issue entry look alike;
``source`` (e.g. ``jira:AAOS-1234``) records where the label came from.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .known_issues import KnownIssueDB

logger = logging.getLogger(__name__)


@dataclass
class ScoredGroup:
    signature: str
    title: str
    count: int
    label_id: str
    expected: str
    rule: str
    ai: Optional[str]
    ai_confidence: Optional[float]


@dataclass
class ClassifierScore:
    name: str
    scored: int = 0
    correct: int = 0
    confusion: Dict[str, Dict[str, int]] = field(default_factory=dict)  # expected -> predicted -> n

    @property
    def accuracy(self) -> Optional[float]:
        return round(self.correct / self.scored, 3) if self.scored else None

    def add(self, expected: str, predicted: Optional[str]) -> None:
        if predicted is None:
            return  # not evaluated (e.g. AI cap reached): neither right nor wrong
        self.scored += 1
        self.correct += int(expected == predicted)
        self.confusion.setdefault(expected, {})
        self.confusion[expected][predicted] = self.confusion[expected].get(predicted, 0) + 1

    def per_class(self) -> Dict[str, Dict[str, Any]]:
        """Precision / recall / F1 per class over the scored groups."""
        tp: Counter = Counter()
        fp: Counter = Counter()
        fn: Counter = Counter()
        for expected, row in self.confusion.items():
            for predicted, n in row.items():
                if expected == predicted:
                    tp[expected] += n
                else:
                    fp[predicted] += n
                    fn[expected] += n
        out: Dict[str, Dict[str, Any]] = {}
        for cls in sorted(set(tp) | set(fp) | set(fn)):
            p = tp[cls] / (tp[cls] + fp[cls]) if tp[cls] + fp[cls] else None
            r = tp[cls] / (tp[cls] + fn[cls]) if tp[cls] + fn[cls] else None
            f1 = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
            out[cls] = {
                "support": tp[cls] + fn[cls],
                "precision": None if p is None else round(p, 3),
                "recall": None if r is None else round(r, 3),
                "f1": None if f1 is None else round(f1, 3),
            }
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scored": self.scored,
            "correct": self.correct,
            "accuracy": self.accuracy,
            "per_class": self.per_class(),
            "confusion": self.confusion,
        }


@dataclass
class EvalResult:
    labels_file: str
    labels_total: int
    groups_total: int
    failures_total: int
    groups_labelled: int
    failures_labelled: int
    rule: ClassifierScore
    ai: ClassifierScore
    scored: List[ScoredGroup] = field(default_factory=list)
    unmatched_labels: List[str] = field(default_factory=list)
    leaked_labels: List[str] = field(default_factory=list)

    @property
    def held_out(self) -> bool:
        return not self.leaked_labels

    def to_dict(self) -> Dict[str, Any]:
        return {
            "labels_file": self.labels_file,
            "labels_total": self.labels_total,
            "held_out": self.held_out,
            "leaked_labels": self.leaked_labels,
            "unmatched_labels": self.unmatched_labels,
            "coverage": {
                "groups": f"{self.groups_labelled}/{self.groups_total}",
                "failures": f"{self.failures_labelled}/{self.failures_total}",
            },
            "rule": self.rule.to_dict(),
            "ai": self.ai.to_dict(),
            "groups": [vars(s) for s in self.scored],
        }

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    def text(self) -> str:
        lines = [
            f"labels: {self.labels_total} from {self.labels_file}"
            + ("" if self.held_out else f"  !! {len(self.leaked_labels)} also in known_issues (not held out)"),
            f"coverage: {self.groups_labelled}/{self.groups_total} groups, "
            f"{self.failures_labelled}/{self.failures_total} failures carry a label",
        ]
        for score in (self.rule, self.ai):
            acc = "n/a" if score.accuracy is None else f"{score.accuracy:.0%}"
            lines.append(f"{score.name:<5} accuracy {acc} ({score.correct}/{score.scored} labelled groups)")
            for cls, m in score.per_class().items():
                fmt = lambda v: "  -  " if v is None else f"{v:5.2f}"  # noqa: E731
                lines.append(
                    f"      {cls:<18} n={m['support']:<3} precision {fmt(m['precision'])} "
                    f"recall {fmt(m['recall'])} f1 {fmt(m['f1'])}"
                )
        wrong = [s for s in self.scored if s.expected != s.rule or (s.ai and s.ai != s.expected)]
        if wrong:
            lines.append("disagreements (expected / rule / ai):")
            for s in wrong[:20]:
                lines.append(f"  {s.count:4d} {s.label_id:<10} {s.expected} / {s.rule} / {s.ai or '-'}  {s.title[:60]}")
        if self.unmatched_labels:
            lines.append(f"labels matching no group in this run: {', '.join(self.unmatched_labels[:15])}")
        return "\n".join(lines)


def evaluate(report: Any, labels: KnownIssueDB, labels_file: str = "",
             known_issues: Optional[KnownIssueDB] = None) -> EvalResult:
    """Score ``report`` (a TriageReport) against ``labels``.

    ``known_issues`` is the config the triage ran with; any label whose id
    is also there was visible to the triage pass and is reported as leaked.
    """
    rule = ClassifierScore("rule")
    ai = ClassifierScore("ai")
    scored: List[ScoredGroup] = []
    seen: set = set()
    failures_labelled = 0
    by_sig: Dict[str, Any] = {}
    for tg in report.groups:
        match = labels.match(tg.group, report.profile, report.fingerprint)
        if match is None or not match.issue.classification:
            continue
        expected = match.issue.classification
        ai_cls = (tg.ai or {}).get("classification")
        ai_conf = (tg.ai or {}).get("confidence")
        rule.add(expected, tg.classification)
        ai.add(expected, ai_cls)
        seen.add(match.issue.id)
        failures_labelled += tg.group.count
        scored.append(ScoredGroup(
            signature=tg.group.signature, title=tg.group.title, count=tg.group.count,
            label_id=match.issue.id, expected=expected, rule=tg.classification,
            ai=ai_cls, ai_confidence=ai_conf,
        ))
        by_sig[tg.group.signature] = tg
    known_ids = {i.id for i in (known_issues.issues if known_issues else [])}
    leaked = sorted(i.id for i in labels.issues if i.id in known_ids)
    unmatched = sorted(i.id for i in labels.issues if i.classification and i.id not in seen)
    result = EvalResult(
        labels_file=labels_file,
        labels_total=len(labels.issues),
        groups_total=len(report.groups),
        failures_total=sum(t.group.count for t in report.groups),
        groups_labelled=len(scored),
        failures_labelled=failures_labelled,
        rule=rule, ai=ai, scored=scored,
        unmatched_labels=unmatched, leaked_labels=leaked,
    )
    by_expected: Dict[str, int] = defaultdict(int)
    for s in scored:
        by_expected[s.expected] += 1
    logger.info("Triage eval: %d labelled groups %s; rule %.0f%%, ai %s",
                len(scored), dict(by_expected), 100 * (rule.accuracy or 0),
                "n/a" if ai.accuracy is None else f"{ai.accuracy:.0%}")
    return result
