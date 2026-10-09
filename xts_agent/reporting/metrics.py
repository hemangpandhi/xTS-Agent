"""Prometheus metrics for xTS runs, without a long-running exporter.

Runs are CI jobs, not a service, so metrics are published two ways (either or
both, configured under ``ops:``):

* ``metrics_textfile_dir``: node_exporter's textfile collector directory. The
  agent atomically rewrites ``xts_<plan>.prom`` on every heartbeat and at the
  end of the run; node_exporter on the host exposes it.
* ``pushgateway_url``: the same payload PUT to a Prometheus Pushgateway,
  grouped by plan.

Useful alerts: heartbeat older than 2x the progress interval while a run is
in progress (agent or host died), ``xts_suite_quiet_seconds`` above the stall
threshold (TradeFed hung), pass rate dropping per build, quarantined devices.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

Sample = Tuple[str, Dict[str, str], float]

HELP = {
    "xts_run_in_progress": ("gauge", "1 while a run of this plan is executing"),
    "xts_run_heartbeat_timestamp_seconds": ("gauge", "Last time the agent reported progress"),
    "xts_run_last_completion_timestamp_seconds": ("gauge", "When the last run of this plan finished"),
    "xts_run_status": ("gauge", "1 for the overall status of the last finished run"),
    "xts_run_duration_seconds": ("gauge", "Wall-clock duration of the last finished run"),
    "xts_suite_modules_completed": ("gauge", "Modules finished in the running TradeFed invocation"),
    "xts_suite_failures_seen": ("gauge", "Test failures seen so far in the running invocation"),
    "xts_suite_quiet_seconds": ("gauge", "Seconds since TradeFed last wrote to its log"),
    "xts_suite_tests": ("gauge", "Tests by result in the last finished run"),
    "xts_suite_status": ("gauge", "1 for the status of each suite in the last finished run"),
    "xts_suite_duration_seconds": ("gauge", "Suite duration in the last finished run, retries included"),
    "xts_suite_retries": ("gauge", "Suite-level retries used in the last finished run"),
    "xts_triage_groups": ("gauge", "Failure groups by history label in the last finished run"),
    "xts_triage_actionable_groups": ("gauge", "Failure groups that still need a human"),
    "xts_devices_quarantined": ("gauge", "Devices currently quarantined on this host"),
}


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render(samples: Iterable[Sample]) -> str:
    """Prometheus text exposition format, grouped by metric name."""
    by_name: Dict[str, List[Sample]] = {}
    for sample in samples:
        by_name.setdefault(sample[0], []).append(sample)
    lines: List[str] = []
    for name, group in by_name.items():
        kind, text = HELP.get(name, ("gauge", name))
        lines += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
        for _, labels, value in group:
            label_text = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
            lines.append(f"{name}{{{label_text}}} {float(value):g}")
    return "\n".join(lines) + "\n"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_").lower() or "plan"


class MetricsPublisher:
    def __init__(self, plan_name: str, textfile_dir: str = "", pushgateway_url: str = "", timeout: int = 10):
        self.plan = plan_name
        self.textfile_dir = Path(textfile_dir) if textfile_dir else None
        self.pushgateway_url = pushgateway_url.rstrip("/")
        self.timeout = timeout
        # Last known values per suite while running, so one heartbeat
        # rewrites the whole file with every suite in it
        self._progress: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()  # parallel suites report from their own threads

    @property
    def enabled(self) -> bool:
        return bool(self.textfile_dir or self.pushgateway_url)

    def _labels(self, **extra: str) -> Dict[str, str]:
        return {"plan": self.plan, **extra}

    # ---- while running ---------------------------------------------------

    def suite_progress(self, suite: str, progress: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._progress[suite] = progress
            self._publish_progress()

    def _publish_progress(self) -> None:
        samples: List[Sample] = [
            ("xts_run_in_progress", self._labels(), 1),
            ("xts_run_heartbeat_timestamp_seconds", self._labels(), time.time()),
        ]
        for name, p in self._progress.items():
            labels = self._labels(suite=name)
            samples += [
                ("xts_suite_modules_completed", labels, p.get("modules_completed", 0)),
                ("xts_suite_failures_seen", labels, p.get("failures", 0)),
                ("xts_suite_quiet_seconds", labels, p.get("quiet_secs", 0)),
            ]
        self.publish(samples)

    # ---- at the end ------------------------------------------------------

    def run_finished(self, plan_result: Any, triage: Any = None, quarantined: int = 0) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._progress.clear()
        samples: List[Sample] = [
            ("xts_run_in_progress", self._labels(), 0),
            ("xts_run_heartbeat_timestamp_seconds", self._labels(), time.time()),
            ("xts_run_last_completion_timestamp_seconds", self._labels(), time.time()),
            ("xts_run_duration_seconds", self._labels(), plan_result.duration or 0),
            ("xts_run_status", self._labels(status=plan_result.overall_status), 1),
            ("xts_devices_quarantined", self._labels(), quarantined),
        ]
        for name, suite in (plan_result.suites_results or {}).items():
            labels = self._labels(suite=name)
            for result, count in (("pass", suite.pass_count), ("fail", suite.fail_count), ("skip", suite.skip_count)):
                samples.append(("xts_suite_tests", self._labels(suite=name, result=result), count or 0))
            samples += [
                ("xts_suite_status", self._labels(suite=name, status=suite.status), 1),
                ("xts_suite_duration_seconds", labels, suite.duration or 0),
                ("xts_suite_retries", labels, suite.retry_count or 0),
            ]
        if triage is not None:
            summary = triage.summary
            for label, count in (summary.get("by_label") or {}).items():
                samples.append(("xts_triage_groups", self._labels(label=label), count))
            samples.append(("xts_triage_actionable_groups", self._labels(), summary.get("actionable_groups", 0)))
        self.publish(samples)

    # ---- output ----------------------------------------------------------

    def publish(self, samples: List[Sample]) -> None:
        body = render(samples)
        if self.textfile_dir:
            try:
                self.textfile_dir.mkdir(parents=True, exist_ok=True)
                target = self.textfile_dir / f"xts_{_slug(self.plan)}.prom"
                # node_exporter may read at any moment: write aside, then rename
                tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
                tmp.write_text(body, encoding="utf-8")
                os.replace(tmp, target)
            except OSError as exc:
                logger.warning("Could not write metrics textfile in %s: %s", self.textfile_dir, exc)
        if self.pushgateway_url:
            url = f"{self.pushgateway_url}/metrics/job/xts_agent/plan/{_slug(self.plan)}"
            try:
                resp = requests.put(url, data=body.encode(), timeout=self.timeout,
                                    headers={"Content-Type": "text/plain; version=0.0.4"})
                if resp.status_code >= 400:
                    logger.warning("Pushgateway rejected metrics: HTTP %s %s", resp.status_code, resp.text[:200])
            except requests.RequestException as exc:
                logger.warning("Pushgateway unreachable (%s): %s", self.pushgateway_url, exc)


def publisher_for(plan: Any) -> Optional[MetricsPublisher]:
    ops = getattr(plan, "ops", None)
    if ops is None:
        return None
    pub = MetricsPublisher(plan.name, ops.metrics_textfile_dir, ops.pushgateway_url)
    return pub if pub.enabled else None
