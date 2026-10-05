"""Slack webhook notifier."""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class SlackNotifier:
    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url or ""

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def _post(self, text: str, blocks: Optional[list] = None) -> bool:
        if not self.enabled:
            return False
        payload: dict[str, Any] = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=15)
            return resp.status_code < 300
        except requests.RequestException as exc:
            logger.warning("Slack notify failed: %s", exc)
            return False

    def notify_start(self, plan_name: str) -> bool:
        return self._post(f":rocket: xTS plan started: *{plan_name}*")

    def notify_suite_complete(self, suite_name: str, status: str) -> bool:
        emoji = ":white_check_mark:" if status == "PASSED" else ":x:"
        return self._post(f"{emoji} Suite *{suite_name}* finished with status `{status}`")

    def notify_plan_complete(self, plan_result: Any) -> bool:
        return self._post(
            f":clipboard: Plan *{getattr(plan_result, 'plan_name', '')}* "
            f"`{plan_result.overall_status}` — "
            f"pass={plan_result.total_pass} fail={plan_result.total_fail} "
            f"skip={plan_result.total_skip}"
        )

    def notify_failure(self, message: str) -> bool:
        return self._post(f":warning: {message}")
