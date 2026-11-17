"""What a suite package can run, asked from the suite itself.

``<suite>-tradefed list modules`` prints one line per module, ABI and
parameterisation (``x86_64 CtsFooTestCases[secondary_user]``); ``list plans``
prints the TradeFed plans. Both take a few seconds (a JVM start), so results
are cached per package build. If the command cannot run (no JDK, broken
package), modules fall back to the package's ``testcases/*/<module>.config``
and the result says so.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ABIS = {"x86_64", "x86", "arm64-v8a", "armeabi-v7a", "arm64", "arm", "riscv64"}
_MODULE_LINE = re.compile(r"^(\S+) ([A-Za-z0-9_.+-]+)(?:\[([^\]]+)\])?\s*$")
_PLAN_LINE = re.compile(r"^\s{2}([A-Za-z0-9_.-]+): ?(.*)$")
_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _build_id(package: Path) -> str:
    try:
        return (package / "tools" / "version.txt").read_text(encoding="utf-8").strip()[:40]
    except OSError:
        return ""


def _cache_file(cache_dir: Path, kind: str, suite: str, package: Path) -> Path:
    digest = hashlib.sha1(str(package.resolve()).encode()).hexdigest()[:10]
    return cache_dir / f"{kind}-{suite}-{digest}-{_build_id(package) or 'nobuild'}.json"


def _run_tradefed(package: Path, suite: str, args: List[str], java_home: str, timeout: int) -> str:
    tool = package / "tools" / f"{suite}-tradefed"
    if not tool.exists():
        raise FileNotFoundError(f"{tool} not found")
    env = dict(os.environ)
    if java_home:
        env["JAVA_HOME"] = java_home
        env["PATH"] = str(Path(java_home) / "bin") + os.pathsep + env.get("PATH", "")
    proc = subprocess.run(  # noqa: S603 - fixed argv, package path validated by the caller
        [str(tool), *args], cwd=str(tool.parent), env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return proc.stdout


def parse_modules(text: str) -> List[Dict[str, Any]]:
    """Group ``list modules`` lines into one entry per module with its ABIs and variants."""
    by_name: Dict[str, Dict[str, Any]] = {}
    for line in text.splitlines():
        m = _MODULE_LINE.match(line.strip())
        if not m or m.group(1) not in _ABIS:
            continue
        abi, name, param = m.groups()
        entry = by_name.setdefault(name, {"name": name, "abis": [], "params": []})
        if abi not in entry["abis"]:
            entry["abis"].append(abi)
        if param and param not in entry["params"]:
            entry["params"].append(param)
    return sorted(by_name.values(), key=lambda e: e["name"].lower())


def parse_plans(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    started = False
    for line in text.splitlines():
        if "Available plans include" in line:
            started = True
            continue
        if not started:
            continue
        m = _PLAN_LINE.match(line)
        if m:
            out.append({"name": m.group(1), "description": m.group(2).strip()})
        elif line.strip() and not line.startswith(" "):
            break
    return out


def _modules_from_testcases(package: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    testcases = package / "testcases"
    if not testcases.is_dir():
        return out
    for d in sorted(testcases.iterdir()):
        if d.is_dir() and (d / f"{d.name}.config").exists():
            abis = sorted(c.name for c in d.iterdir() if c.is_dir() and c.name in _ABIS)
            out.append({"name": d.name, "abis": abis, "params": []})
    return out


def _cached(cache: Path, refresh: bool) -> Optional[Dict[str, Any]]:
    if refresh or not cache.exists():
        return None
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _store(cache: Path, data: Dict[str, Any]) -> None:
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, cache)
    except OSError as exc:
        logger.warning("Could not cache %s: %s", cache, exc)


def list_modules(package_path: str, suite: str, cache_dir: Path, java_home: str = "",
                 refresh: bool = False, timeout: int = 300) -> Dict[str, Any]:
    package, suite = Path(package_path), suite.lower()
    cache = _cache_file(Path(cache_dir), "modules", suite, package)
    with _lock(str(cache)):
        hit = _cached(cache, refresh)
        if hit is not None:
            return {**hit, "cached": True}
        started = time.time()
        source, error = "tradefed", ""
        try:
            modules = parse_modules(_run_tradefed(package, suite, ["list", "modules"], java_home, timeout))
            if not modules:
                raise RuntimeError("no modules in the command output")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning("%s list modules failed (%s); reading testcases/ instead", suite, error)
            modules, source = _modules_from_testcases(package), "testcases"
        data = {
            "suite": suite, "package": str(package), "build": _build_id(package), "source": source,
            "command": f"{suite}-tradefed list modules", "error": error, "modules": modules,
            "variants": sum(len(m["abis"]) * (1 + len(m["params"])) for m in modules),
            "fetched_at": time.time(), "seconds": round(time.time() - started, 1),
        }
        if modules:
            _store(cache, data)
        return {**data, "cached": False}


def list_plans(package_path: str, suite: str, cache_dir: Path, java_home: str = "",
               refresh: bool = False, timeout: int = 300) -> Dict[str, Any]:
    package, suite = Path(package_path), suite.lower()
    cache = _cache_file(Path(cache_dir), "plans", suite, package)
    with _lock(str(cache)):
        hit = _cached(cache, refresh)
        if hit is not None:
            return {**hit, "cached": True}
        error = ""
        try:
            plans = parse_plans(_run_tradefed(package, suite, ["list", "plans"], java_home, timeout))
        except Exception as exc:
            error, plans = f"{type(exc).__name__}: {exc}"[:300], []
        data = {"suite": suite, "package": str(package), "build": _build_id(package), "plans": plans,
                "error": error, "fetched_at": time.time()}
        if plans:
            _store(cache, data)
        return {**data, "cached": False}
