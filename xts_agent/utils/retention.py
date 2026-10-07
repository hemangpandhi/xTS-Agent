"""Disk guard and retention for TradeFed results/logs and agent logs.

TradeFed never deletes anything: every session leaves ``results/<session>``,
``results/<session>.zip`` and ``logs/<session>`` (often several GB for a
full CTS run) under the suite package. A host that fills up mid-run turns
into a wave of failures that look like test failures, so:

* ``check_free_space`` refuses to start a run when a filesystem the run
  writes to (suite packages, agent results, the temp dir) is below
  ``ops.min_free_disk_gb``;
* ``plan_prune`` / ``prune`` (``xts-agent cleanup --prune-results``) delete
  sessions older than N days, always keeping the newest few per suite and
  anything an unfinished run may still resume from.

Pruning result dirs shifts TradeFed's session numbering (sessions are
numbered by result-dir order); retries therefore re-derive the session
index from the results dir rather than trusting a stored number.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

SESSION_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})_(\d{2})\.(\d{2})\.(\d{2})\.\d{3}_\d+")


def session_key(name: str) -> Optional[str]:
    m = SESSION_RE.match(name)
    return m.group(0) if m else None


def session_time(key: str) -> Optional[float]:
    m = SESSION_RE.match(key)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


def path_size(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


# ---- free space ----------------------------------------------------------


def free_gb(path: Path) -> float:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 1024**3


def check_free_space(paths: Iterable[Path], min_gb: float) -> List[str]:
    """One problem string per filesystem below ``min_gb`` (deduplicated)."""
    if min_gb <= 0:
        return []
    problems: List[str] = []
    seen: Set[int] = set()
    for path in [*paths, Path(tempfile.gettempdir())]:
        probe = Path(path)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            dev = probe.stat().st_dev
        except OSError:
            continue
        if dev in seen:
            continue
        seen.add(dev)
        gb = free_gb(probe)
        if gb < min_gb:
            problems.append(f"{probe}: {gb:.1f} GB free (< {min_gb:g} GB)")
    return problems


# ---- pruning -------------------------------------------------------------


@dataclass
class PrunePlan:
    paths: List[Path]
    kept_sessions: int
    protected: List[str]
    freed: int = 0  # set by prune()

    @property
    def bytes(self) -> int:
        return sum(path_size(p) for p in self.paths)


def protected_sessions(run_state_dir: Path) -> Set[str]:
    """Sessions an unfinished run checkpoint may still resume from."""
    keys: Set[str] = set()
    if not run_state_dir.is_dir():
        return keys
    for state_file in run_state_dir.glob("*.json"):
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("complete"):
            continue
        for entry in (data.get("suites") or {}).values():
            key = session_key(Path(entry.get("results_dir") or "").name)
            if key:
                keys.add(key)
    return keys


def plan_prune(
    suite_roots: Iterable[Path],
    keep_days: float,
    keep_latest: int = 3,
    protect: Optional[Set[str]] = None,
    agent_log_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> PrunePlan:
    """Work out what to delete; nothing is touched here."""
    now = now or time.time()
    cutoff = now - keep_days * 86400
    protect = set(protect or ())
    doomed: List[Path] = []
    kept = 0
    hit_protect: List[str] = []
    for root in suite_roots:
        sessions: Dict[str, List[Path]] = {}
        for sub in ("results", "logs"):
            base = Path(root) / sub
            if not base.is_dir():
                continue
            for child in base.iterdir():
                key = session_key(child.name)
                if key and not child.is_symlink():
                    sessions.setdefault(key, []).append(child)
        # Newest sessions with results survive regardless of age
        with_results = sorted(
            (k for k, paths in sessions.items() if any(p.parent.name == "results" and p.is_dir() for p in paths)),
            reverse=True,
        )
        newest = set(with_results[:keep_latest])
        for key, paths in sessions.items():
            ts = session_time(key)
            if key in newest or ts is None or ts >= cutoff:
                kept += 1
                continue
            if key in protect:
                hit_protect.append(key)
                kept += 1
                continue
            doomed.extend(paths)
    if agent_log_dir is not None and Path(agent_log_dir).is_dir():
        for log in Path(agent_log_dir).glob("tradefed_run_*.log"):
            if log.stat().st_mtime < cutoff:
                doomed.append(log)
    return PrunePlan(sorted(doomed), kept, sorted(hit_protect))


def prune(plan: PrunePlan) -> int:
    freed = 0
    for path in plan.paths:
        size = path_size(path)
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            freed += size
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)
    logger.info("Pruned %s path(s), freed %.1f GB", len(plan.paths), freed / 1024**3)
    plan.freed = freed
    return freed
