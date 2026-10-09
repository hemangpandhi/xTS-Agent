"""Live progress of a running TradeFed invocation, read from its console log.

TradeFed's stdout goes to a log file, so a 30-hour CTS run is silent in the
CI job log. A monitor thread tails that file every ``interval`` seconds and
logs one line: modules started/finished, failures so far, what each device
is running, and how long the log has been quiet. A log that stays quiet past
``stall_after`` seconds is reported as a likely hang (a wedged device or a
stuck host-side test) instead of burning the rest of the suite timeout.

Passing tests are not logged by TradeFed at INFO, so progress is counted in
modules, which is also what ``modules_done`` in test_result.xml counts. A
module counts as finished once the next module starts on the same device.
``ShardListener: Sharded test completed`` is not used: it is printed per
test run (thousands of times for dEQP, and for modules that never started
on a device) and never in unsharded runs. The final numbers come from
test_result.xml when the invocation ends.
"""

from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)

# 10-04 16:20:03 I/ITestSuite: 0.0.0.0:6527 running 1 modules: [x86_64 CtsFooTestCases]
_SUITE_START = re.compile(r"I/ITestSuite: (\S+) running (\d+) modules?(?:: \[(.*)\])?\s*$")
# 10-06 01:04:29 D/ModuleDefinition: Running module x86_64 CtsBionicTestCases
# (one per module also when a single invocation runs many modules unsharded)
_MODULE_RUNNING = re.compile(r"D/ModuleDefinition: Running module (\S+ \S+)\s*$")
# 10-04 16:20:17 I/ModuleListener: [1/1] <serial> <class>#<test> FAILURE: ...
_TEST_FAILURE = re.compile(r"I/ModuleListener: \[\d+/\d+\] (?:\S+ ){2,3}FAILURE:")


@dataclass
class TradefedProgress:
    started: Set[str] = field(default_factory=set)  # "abi module"
    finished: Set[str] = field(default_factory=set)
    failures: int = 0
    current: Dict[str, str] = field(default_factory=dict)  # serial -> "abi module"
    started_at: float = field(default_factory=time.time)
    last_output_at: float = field(default_factory=time.time)
    _serial: str = ""  # device of the latest ITestSuite line (unsharded: the only one)

    @property
    def modules_started(self) -> int:
        return len(self.started)

    @property
    def modules_completed(self) -> int:
        # A retried module is running again, not finished
        return len(self.finished - set(self.current.values()))

    def _start(self, serial: str, module: str) -> None:
        previous = self.current.get(serial)
        if previous == module:
            return
        if previous:
            self.finished.add(previous)
        self.started.add(module)
        self.current[serial] = module

    def feed(self, text: str) -> None:
        for line in text.splitlines():
            m = _SUITE_START.search(line)
            if m:
                self._serial = m.group(1)
                if m.group(2) == "1" and m.group(3):
                    self._start(self._serial, m.group(3))
                continue
            m = _MODULE_RUNNING.search(line)
            if m:
                # Has no serial: only needed when ITestSuite listed several
                # modules at once (unsharded); otherwise it echoes a start
                # already seen, possibly from another shard's device
                if m.group(1) not in self.current.values():
                    self._start(self._serial or "?", m.group(1))
            elif _TEST_FAILURE.search(line):
                self.failures += 1

    def quiet_secs(self, now: Optional[float] = None) -> float:
        return (now or time.time()) - self.last_output_at

    def as_dict(self) -> Dict[str, object]:
        return {
            "modules_started": self.modules_started,
            "modules_completed": self.modules_completed,
            "failures": self.failures,
            "current": {serial: module.split(" ", 1)[-1] for serial, module in self.current.items()},
            "elapsed_secs": round(time.time() - self.started_at),
            "quiet_secs": round(self.quiet_secs()),
            "heartbeat_at": time.time(),
        }


def _fmt_secs(secs: float) -> str:
    secs = int(secs)
    return f"{secs // 3600}h{secs % 3600 // 60:02d}m" if secs >= 3600 else f"{secs // 60}m{secs % 60:02d}s"


class ProgressMonitor(threading.Thread):
    def __init__(
        self,
        log_path: str | Path,
        interval_secs: float = 600,
        stall_after_secs: float = 3600,
        on_update: Optional[Callable[[TradefedProgress], None]] = None,
    ):
        super().__init__(name="tf-progress", daemon=True)
        self.log_path = Path(log_path)
        self.interval = max(0.1, float(interval_secs))
        self.stall_after = float(stall_after_secs)
        self.on_update = on_update
        self.progress = TradefedProgress()
        self._offset = 0
        self._partial = ""
        self._stop_event = threading.Event()
        self._stall_reported = False
        # Threads do not inherit context vars; keep the caller's (suite log tag)
        self._context = contextvars.copy_context()

    def poll(self) -> TradefedProgress:
        """Read what TradeFed wrote since the last poll and update counters."""
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except OSError:
            chunk = ""
        if chunk:
            self.progress.last_output_at = time.time()
            self._stall_reported = False
            # Only parse complete lines; keep a trailing partial line for later
            text = self._partial + chunk
            text, _, self._partial = text.rpartition("\n")
            self.progress.feed(text)
        return self.progress

    def report(self) -> None:
        p = self.poll()
        running = ", ".join(f"{s}:{m.split(' ', 1)[-1]}" for s, m in sorted(p.current.items())[:8])
        logger.info(
            "TradeFed progress: %s modules finished, %s started, %s test failures, elapsed %s%s",
            p.modules_completed,
            p.modules_started,
            p.failures,
            _fmt_secs(time.time() - p.started_at),
            f" | running {running}" if running else "",
        )
        quiet = p.quiet_secs()
        if self.stall_after > 0 and quiet >= self.stall_after and not self._stall_reported:
            self._stall_reported = True
            logger.warning(
                "TradeFed log silent for %s: possible hang (check devices %s)",
                _fmt_secs(quiet),
                sorted(p.current) or "-",
            )
        if self.on_update is not None:
            try:
                self.on_update(p)
            except Exception as exc:  # progress reporting must never break a run
                logger.debug("Progress callback failed: %s", exc)

    def run(self) -> None:
        self._context.run(self._loop)

    def _loop(self) -> None:
        while not self._stop_event.wait(self.interval):
            self.report()

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=5)
        self.poll()
