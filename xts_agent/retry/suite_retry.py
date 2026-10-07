"""Suite-level retry handling."""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)


class SuiteRetryHandler:
    def __init__(self):
        self.retry_history: List[dict] = []

    def retry_failed_tests(
        self,
        runner: Any,
        session_id: int,
        max_retries: int,
        retry_type: str,
        device_serials: Optional[Sequence[str]] = None,
        timeout_hours: float = 24.0,
        log_dir: str = "./results/logs",
    ) -> Any:
        last = None
        for attempt in range(1, max_retries + 1):
            logger.info(
                "SuiteRetryHandler attempt %s/%s session=%s type=%s",
                attempt,
                max_retries,
                session_id,
                retry_type,
            )
            cmd = runner.build_retry_command(
                session_id, retry_type, device_serials=device_serials
            )
            last = runner.execute(cmd, timeout_hours=timeout_hours, log_dir=log_dir)
            self.retry_history.append(
                {
                    "attempt": attempt,
                    "session_id": session_id,
                    "success": last.success,
                    "return_code": last.return_code,
                }
            )
            if last.success:
                break
            if last.session_id is not None:
                session_id = last.session_id
        return last
