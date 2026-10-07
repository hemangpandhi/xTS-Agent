"""Route failure groups to owning teams from config/ownership.yaml."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

from .signature import FailureGroup

logger = logging.getLogger(__name__)


@dataclass
class Owner:
    team: str
    jira_component: str = ""
    assignee: str = ""
    watchers: List[str] = field(default_factory=list)


@dataclass
class _Rule:
    pattern: "re.Pattern[str]"
    owner: Owner


class OwnershipMap:
    def __init__(self, rules: List[_Rule], default: Owner):
        self.rules = rules
        self.default = default

    @classmethod
    def load(cls, path: str | Path) -> "OwnershipMap":
        path = Path(path)
        if not path.exists():
            logger.info("No ownership map at %s; everything routes to the default team", path)
            return cls([], Owner("xts-triage"))
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        def owner(raw: dict) -> Owner:
            return Owner(
                team=str(raw.get("team") or "xts-triage"),
                jira_component=str(raw.get("jira_component") or ""),
                assignee=str(raw.get("assignee") or ""),
                watchers=list(raw.get("watchers") or []),
            )

        rules = []
        for raw in data.get("rules") or []:
            try:
                rules.append(_Rule(re.compile(raw["module_regex"]), owner(raw)))
            except (KeyError, re.error) as exc:
                logger.error("Skipping invalid ownership rule %r: %s", raw, exc)
        return cls(rules, owner(data.get("default") or {}))

    def owner_for_module(self, module: str) -> Owner:
        name = module.split(" ")[-1]  # drop the ABI prefix
        return next((r.owner for r in self.rules if r.pattern.search(name)), self.default)

    def owner_for_group(self, group: FailureGroup) -> Owner:
        """Owner of the module contributing most of the group's failures."""
        counts = Counter(t.module.split(" ")[-1] for t in group.tests if t.module)
        if not counts:
            return self.default
        return self.owner_for_module(counts.most_common(1)[0][0])
