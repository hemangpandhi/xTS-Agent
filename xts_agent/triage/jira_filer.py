"""File one Jira ticket per actionable failure group, deduplicated by signature.

Every ticket carries an ``xts-sig-<signature>`` label. Before creating a
ticket the filer searches for an open issue with that label and, if found,
comments on it instead, so the same root cause never produces duplicates
across runs. New tickets are only opened for configured history labels
(NEW / NO_HISTORY by default) and are capped per run to avoid ticket storms.

``mode: dry_run`` (the default) makes no HTTP calls and writes the tickets
that would be filed to a preview JSON. Uses the v2 REST API, which works on
both Jira Cloud and Server/Data Center.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .engine import TriagedGroup, TriageReport

logger = logging.getLogger(__name__)

MAX_TESTS_IN_TICKET = 30


@dataclass
class JiraConfig:
    enabled: bool = False
    mode: str = "dry_run"  # dry_run | live
    base_url: str = ""
    project: str = ""
    issue_type: str = "Bug"
    auth: str = "bearer"  # bearer (Server/DC PAT) | basic (Cloud: user + API token)
    user: str = ""
    labels: List[str] = field(default_factory=lambda: ["xts-agent"])
    file_labels: List[str] = field(default_factory=lambda: ["NEW", "NO_HISTORY"])
    max_new_issues_per_run: int = 20
    comment_on_existing: bool = True
    assignee_field: str = "name"  # "name" (Server/DC) or "accountId" (Cloud)
    timeout_secs: int = 30
    report_url: str = ""  # optional link to the CI job / report, added to tickets


class JiraClient:
    def __init__(self, cfg: JiraConfig, token: str):
        self.base = cfg.base_url.rstrip("/")
        self.timeout = cfg.timeout_secs
        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"
        if cfg.auth == "basic":
            self.session.auth = (cfg.user, token)
        else:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _call(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        resp = self.session.request(method, f"{self.base}{path}", timeout=self.timeout, **kwargs)
        if resp.status_code >= 400:
            # Body only: never echo request headers (auth) into logs
            raise JiraError(resp.status_code, resp.text[:500])
        return resp.json() if resp.content else {}

    def find_open_by_label(self, project: str, label: str) -> Optional[str]:
        jql = f'project = "{project}" AND labels = "{label}" AND statusCategory != Done ORDER BY created DESC'
        data = self._call("POST", "/rest/api/2/search", json={"jql": jql, "maxResults": 1, "fields": ["key"]})
        issues = data.get("issues") or []
        return issues[0]["key"] if issues else None

    def create(self, fields: Dict[str, Any]) -> str:
        return self._call("POST", "/rest/api/2/issue", json={"fields": fields})["key"]

    def comment(self, key: str, body: str) -> None:
        self._call("POST", f"/rest/api/2/issue/{key}/comment", json={"body": body})


class JiraError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Jira HTTP {status}: {body}")
        self.status = status
        self.body = body


def signature_label(signature: str) -> str:
    return f"xts-sig-{signature}"


class JiraFiler:
    def __init__(self, cfg: JiraConfig, client: Optional[JiraClient] = None):
        self.cfg = cfg
        self.client = client

    @classmethod
    def from_config(cls, cfg: JiraConfig) -> "JiraFiler":
        if cfg.mode != "live":
            return cls(cfg)
        token = os.environ.get("XTS_JIRA_TOKEN", "")
        if not (cfg.base_url and cfg.project and token):
            raise ValueError("jira live mode needs jira.base_url, jira.project and XTS_JIRA_TOKEN")
        return cls(cfg, JiraClient(cfg, token))

    # ---- content ---------------------------------------------------------

    def summary(self, tg: TriagedGroup) -> str:
        suites = "/".join(tg.group.suites)
        return f"[xTS][{suites}] {tg.group.title}"[:250]

    def description(self, tg: TriagedGroup, report: TriageReport) -> str:
        g = tg.group
        tests = "\n".join(t.test_id for t in g.tests[:MAX_TESTS_IN_TICKET])
        more = f"\n... and {g.count - MAX_TESTS_IN_TICKET} more" if g.count > MAX_TESTS_IN_TICKET else ""
        lines = [
            f"*{g.count} failing test(s)* in {len(g.modules)} module(s), filed by xTS Agent.",
            "",
            f"*History:* {tg.label}"
            + (f" (last passed on {tg.last_pass_build})" if tg.last_pass_build else ""),
            f"*Build:* {report.fingerprint or 'unknown'}",
            f"*Plan:* {report.plan_name} ({report.profile} profile)",
            f"*Classification:* {tg.classification}",
            f"*Owner:* {tg.owner.team}",
            f"*Signature:* {g.signature}",
        ]
        if self.cfg.report_url:
            lines.append(f"*Report:* {self.cfg.report_url}")
        if tg.ai:
            lines += ["", f"*AI analysis ({tg.ai.get('confidence', '?')}):* {tg.ai.get('root_cause', '')}",
                      str(tg.ai.get("suggested_fix", ""))]
        lines += ["", "*Modules:* " + ", ".join(g.modules), "", "*Tests:*", "{noformat}", tests + more,
                  "{noformat}", "", "*Sample stack trace:*", "{noformat}", g.sample_stack[:6000], "{noformat}"]
        return "\n".join(lines)

    def fields(self, tg: TriagedGroup, report: TriageReport) -> Dict[str, Any]:
        labels = list(dict.fromkeys(
            [*self.cfg.labels, signature_label(tg.group.signature), f"xts-{tg.label.lower()}"]
        ))
        fields: Dict[str, Any] = {
            "project": {"key": self.cfg.project},
            "issuetype": {"name": self.cfg.issue_type},
            "summary": self.summary(tg),
            "description": self.description(tg, report),
            "labels": labels,
        }
        if tg.owner.jira_component:
            fields["components"] = [{"name": tg.owner.jira_component}]
        if tg.owner.assignee:
            fields["assignee"] = {self.cfg.assignee_field: tg.owner.assignee}
        return fields

    # ---- filing ----------------------------------------------------------

    def file(self, report: TriageReport, preview_path: Optional[Path] = None) -> Dict[str, int]:
        stats = {"created": 0, "commented": 0, "would_create": 0, "skipped_cap": 0, "errors": 0}
        preview: List[Dict[str, Any]] = []
        for tg in report.groups:
            if not tg.actionable:
                continue  # waived, or the known issue already has a ticket
            sig_label = signature_label(tg.group.signature)
            try:
                existing = None
                if self.client is not None:
                    existing = self.client.find_open_by_label(self.cfg.project, sig_label)
                if existing:
                    tg.jira_key, tg.jira_action = existing, "seen again"
                    if self.cfg.comment_on_existing:
                        self.client.comment(existing, self._recurrence_comment(tg, report))
                        tg.jira_action = "commented"
                        stats["commented"] += 1
                    continue
                if tg.label not in self.cfg.file_labels:
                    continue  # e.g. PERSISTENT without an open ticket: leave for humans
                if stats["created"] + stats["would_create"] >= self.cfg.max_new_issues_per_run:
                    stats["skipped_cap"] += 1
                    continue
                fields = self.fields(tg, report)
                if self.client is None:
                    tg.jira_action = "would create"
                    stats["would_create"] += 1
                    preview.append(fields)
                    continue
                tg.jira_key = self._create(fields)
                tg.jira_action = "created"
                stats["created"] += 1
            except (JiraError, requests.RequestException) as exc:
                logger.error("Jira filing failed for group %s: %s", tg.group.signature, exc)
                stats["errors"] += 1
        if stats["skipped_cap"]:
            logger.warning(
                "%s group(s) not filed: max_new_issues_per_run=%s reached",
                stats["skipped_cap"], self.cfg.max_new_issues_per_run,
            )
        if preview and preview_path is not None:
            preview_path.parent.mkdir(parents=True, exist_ok=True)
            preview_path.write_text(json.dumps(preview, indent=2), encoding="utf-8")
            logger.info("Jira dry run: %s ticket(s) previewed in %s", len(preview), preview_path)
        logger.info("Jira filing: %s", stats)
        return stats

    def _create(self, fields: Dict[str, Any]) -> str:
        try:
            return self.client.create(fields)
        except JiraError as exc:
            if exc.status == 400 and ("components" in exc.body or "assignee" in exc.body):
                # Unknown component/assignee in this project: file anyway, without them
                logger.warning("Retrying ticket without components/assignee: %s", exc.body[:200])
                trimmed = {k: v for k, v in fields.items() if k not in ("components", "assignee")}
                return self.client.create(trimmed)
            raise

    def _recurrence_comment(self, tg: TriagedGroup, report: TriageReport) -> str:
        return (
            f"Seen again by xTS Agent: {tg.group.count} failing test(s) in "
            f"{', '.join(tg.group.modules[:10])} on build {report.fingerprint or 'unknown'} "
            f"(plan {report.plan_name}, history {tg.label})."
            + (f" Report: {self.cfg.report_url}" if self.cfg.report_url else "")
        )
