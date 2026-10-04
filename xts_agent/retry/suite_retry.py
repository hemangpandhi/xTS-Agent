"""Suite-level retry handling."""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)

class SuiteRetryHandler:
    def __init__(self):
        self.retry_history = []

    def retry_failed_tests(self, runner: Any, session_id: int, max_retries: int, retry_type: str) -> Any:
        logger.info(f"Retrying session {session_id} up to {max_retries} times.")
        return None
