"""Logging: one console stream plus a rotating JSON file, tagged with run id and suite.

* Console: Rich on an interactive terminal; plain single-line records with
  timestamps everywhere else (CI job logs, docker logs), so nothing is
  wrapped at 80 columns or colour-coded into noise.
* File: JSON lines, rotated by size so a host never fills up with agent logs.
* Every record carries ``run_id`` (``XTS_RUN_ID``, else the GitLab
  ``CI_JOB_ID``, else time + pid) and the suite being executed, so lines from
  parallel suites, retries and different jobs can be told apart and joined
  with metrics and reports.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

LOGGER_NAME = "xts_agent"
PLAIN_FORMAT = "%(asctime)s %(levelname)-7s [%(run_id)s%(suite_tag)s] %(name)s: %(message)s"

_suite: contextvars.ContextVar[str] = contextvars.ContextVar("xts_suite", default="")
_run_id: Optional[str] = None


def run_id() -> str:
    global _run_id
    if _run_id is None:
        _run_id = (
            os.environ.get("XTS_RUN_ID")
            or (f"ci{os.environ['CI_JOB_ID']}" if os.environ.get("CI_JOB_ID") else "")
            or f"{time.strftime('%Y%m%d%H%M%S')}-{os.getpid()}"
        )
    return _run_id


@contextmanager
def suite_context(name: str) -> Iterator[None]:
    """Tag log records from this thread with ``name`` while the block runs."""
    token = _suite.set(name)
    try:
        yield
    finally:
        _suite.reset(token)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id()
        suite = _suite.get()
        record.suite = suite
        record.suite_tag = f" {suite}" if suite else ""
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", ""),
            "message": record.getMessage(),
        }
        suite = getattr(record, "suite", "")
        if suite:
            data["suite"] = suite
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data)


def _interactive() -> bool:
    return sys.stderr.isatty() and not os.environ.get("CI")


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    max_bytes: int = 50 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """(Re)configure the ``xts_agent`` logger; safe to call more than once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False  # never also print through a root handler
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    context = ContextFilter()
    if _interactive():
        from rich.logging import RichHandler

        console: logging.Handler = RichHandler(rich_tracebacks=True, show_path=False)
    else:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(PLAIN_FORMAT, "%Y-%m-%d %H:%M:%S"))
    console.addFilter(context)
    logger.addHandler(console)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(JSONFormatter())
        file_handler.addFilter(context)
        logger.addHandler(file_handler)
    return logger


def setup_logger(name: str = LOGGER_NAME, log_file: str = "xts_agent.log", level: int = logging.INFO) -> logging.Logger:
    """Backwards-compatible alias for :func:`setup_logging`."""
    return setup_logging(level=level, log_file=log_file)
