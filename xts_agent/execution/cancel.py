"""Process-wide cancellation: SIGTERM / Ctrl-C stop a run cleanly.

A CI cancel, job timeout, ``docker stop`` or Ctrl-C used to kill the agent
while TradeFed (in its own session) kept running and holding the devices.
Now the first SIGTERM/SIGINT:

* sends SIGTERM to every running TradeFed process group, so TradeFed flushes
  a partial test_result.xml that ``run --resume`` can continue from;
* stops new suites, retries, isolation reboots and cooldowns from starting;
* lets the orchestrator write the checkpoint and reports, then exit 143/130.

A second signal SIGKILLs the TradeFed groups and exits immediately.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Any, Optional, Set

logger = logging.getLogger(__name__)

_event = threading.Event()
_lock = threading.Lock()
_runners: Set[Any] = set()
_signum: Optional[int] = None


class RunCancelled(Exception):
    """Raised where work must not start because the run was cancelled."""


def cancelled() -> bool:
    return _event.is_set()


def check() -> None:
    if _event.is_set():
        raise RunCancelled("run cancelled")


def wait(seconds: float) -> bool:
    """Sleep that ends early on cancel; returns True if cancelled."""
    return _event.wait(max(0.0, seconds))


def exit_code() -> int:
    """Conventional 128 + signal number (143 SIGTERM, 130 SIGINT)."""
    return 128 + (_signum or signal.SIGTERM)


def register(runner: Any) -> None:
    with _lock:
        _runners.add(runner)


def unregister(runner: Any) -> None:
    with _lock:
        _runners.discard(runner)


def _signal_groups(sig: int) -> None:
    with _lock:
        pids = [r.pid for r in _runners if getattr(r, "pid", None)]
    for pid in pids:
        try:
            os.killpg(pid, sig)  # TradeFed runs in its own session: pgid == pid
        except (ProcessLookupError, PermissionError):
            pass


def request_cancel(signum: int = signal.SIGTERM) -> None:
    global _signum
    if _event.is_set():
        return
    _signum = signum
    _event.set()
    _signal_groups(signal.SIGTERM)


def _handler(signum: int, _frame: Any) -> None:
    if _event.is_set():
        # Second signal: no more waiting for TradeFed to flush
        logger.error("Second %s: killing TradeFed and exiting now", signal.Signals(signum).name)
        _signal_groups(signal.SIGKILL)
        logging.shutdown()
        os._exit(128 + signum)
    logger.warning(
        "%s received: stopping TradeFed, saving checkpoint (send again to force quit)",
        signal.Signals(signum).name,
    )
    request_cancel(signum)


def install_signal_handlers() -> None:
    """Install SIGTERM/SIGINT/SIGHUP handlers (main thread only)."""
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _handler)


def reset() -> None:
    """For tests: clear cancellation state."""
    global _signum
    _event.clear()
    _signum = None
    with _lock:
        _runners.clear()
