from __future__ import annotations
"""
from __future__ import annotations
Slack/webhook notifications.
"""
from typing import Any

class SlackNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        
    def notify_start(self, plan_name: str, device_count: int):
        pass
        
    def notify_suite_complete(self, suite_name: str, result: Any):
        pass
        
    def notify_plan_complete(self, plan_result: Any):
        pass
        
    def notify_failure(self, message: str):
        pass
