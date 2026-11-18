"""Host settings edited from the web console (``config/local_settings.yaml``).

``default_config.yaml`` is the shipped, commented reference; this file holds
what a lab sets for its own host, so the console can write it without
touching the reference (PyYAML would drop its comments). Every run merges it
over the defaults:

* ``paths``: xts_packages_dir, java_home, android_sdk, adb_path
* ``suites``: installed suite packages per suite *and* architecture, because
  Google ships separate x86 and ARM downloads that cannot be mixed, plus the
  default architecture used when a plan does not name one
* ``ai_rca``: the on-prem LLM endpoint (base URL and model)

A suite's package path then resolves as: the plan's explicit
``package_path`` > the installation for (suite, arch) > the installation
marked default for that suite > ``<xts_packages_dir>/android-<suite>``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

SUITES = ("cts", "vts", "sts", "gts", "ats", "catbox")
ARCHES = ("x86_64", "arm64")
AI_PROVIDERS = ("openai_compatible", "llama_cpp", "gemini", "none")
# ABI folder names inside a suite's testcases/<module>/ -> architecture family
_ABI_ARCH = {"x86_64": "x86_64", "x86": "x86_64", "arm64": "arm64", "arm": "arm64", "arm64-v8a": "arm64", "armeabi-v7a": "arm64"}
LOCAL_SETTINGS_NAME = "local_settings.yaml"
_PATH_RE = re.compile(r"^/[^\0\n\r]*$")


def settings_path(defaults_path: Optional[Path] = None) -> Path:
    env = os.environ.get("XTS_LOCAL_SETTINGS")
    if env:
        return Path(env)
    base = Path(defaults_path).parent if defaults_path else Path("config")
    return base / LOCAL_SETTINGS_NAME


def load(path: Path) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Ignoring unreadable local settings %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save(path: Path, data: Dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(
        "# Host settings written by `xts-agent ui` (Settings page). Merged over\n"
        "# default_config.yaml for every run. Safe to edit by hand.\n"
        + yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def merge_into_defaults(defaults: Dict[str, Any], local: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay ``paths`` and ``ai_rca`` from local settings onto the defaults."""
    out = dict(defaults)
    for section in ("paths", "ai_rca"):
        overlay = {k: v for k, v in (local.get(section) or {}).items() if v not in (None, "")}
        if overlay:
            base_sec = dict(defaults.get(section) or {})
            if section == "ai_rca":
                # Server-specific extra_body in default_config.yaml (e.g. vLLM Qwen3
                # chat_template_kwargs) must not leak to a different OpenAI-compatible URL
                if (
                    overlay.get("openai_base_url")
                    and overlay.get("openai_base_url") != base_sec.get("openai_base_url")
                    and "openai_extra_body" not in overlay
                ):
                    base_sec["openai_extra_body"] = {}
            out[section] = {**base_sec, **overlay}
    if local.get("suites"):
        out["suite_installations"] = local["suites"]
    return out


def installations(local_suites: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [i for i in (local_suites or {}).get("installations") or [] if isinstance(i, dict)]


def resolve_package_path(suite: str, arch: str, local_suites: Dict[str, Any], packages_dir: str) -> str:
    suite = suite.lower()
    items = [i for i in installations(local_suites) if str(i.get("suite", "")).lower() == suite and i.get("path")]
    arch = arch or (local_suites or {}).get("default_arch") or ""
    for i in items:
        if arch and i.get("arch") == arch:
            return str(i["path"])
    for i in items:
        if i.get("default"):
            return str(i["path"])
    if len(items) == 1 and not arch:
        return str(items[0]["path"])
    return str(Path(packages_dir) / f"android-{suite}")


# ---------------------------------------------------------------- inspection


def inspect_package(path: str, suite: str = "") -> Dict[str, Any]:
    """What a suite package directory contains: suite, build id, ABIs, problems."""
    p = Path(path)
    info: Dict[str, Any] = {"path": str(path), "ok": False, "problems": [], "suite": suite.lower(), "build": "",
                            "abis": [], "arch": "", "modules": 0}
    if not path or not _PATH_RE.match(str(path)):
        info["problems"].append("must be an absolute path")
        return info
    if not p.is_dir():
        info["problems"].append("directory does not exist")
        return info
    tools = p / "tools"
    if not suite:
        guess = re.match(r"android-([a-z]+)", p.name)
        if guess and guess.group(1) in SUITES:
            info["suite"] = suite = guess.group(1)
        else:
            for s in SUITES:
                if (tools / f"{s}-tradefed").exists():
                    info["suite"] = suite = s
                    break
    if not (tools / "tradefed.jar").exists():
        info["problems"].append("tools/tradefed.jar missing: not an xTS package")
    if suite and not (tools / f"{suite}-tradefed").exists():
        info["problems"].append(f"tools/{suite}-tradefed missing")
    try:
        info["build"] = (tools / "version.txt").read_text(encoding="utf-8").strip()[:40]
    except OSError:
        pass
    abis: set = set()
    testcases = p / "testcases"
    if testcases.is_dir():
        modules = [d for d in testcases.iterdir() if d.is_dir()]
        info["modules"] = len(modules)
        for d in modules[:80]:
            try:
                abis.update(c.name for c in d.iterdir() if c.is_dir() and c.name in _ABI_ARCH)
            except OSError:
                continue
    info["abis"] = sorted(abis)
    arches = sorted({_ABI_ARCH[a] for a in abis})
    info["arch"] = arches[0] if len(arches) == 1 else ("multi" if arches else "")
    info["ok"] = not info["problems"]
    return info


def scan(root: str, max_depth: int = 3) -> List[Dict[str, Any]]:
    """Suite packages (folders with tools/tradefed.jar) under ``root``."""
    base = Path(root)
    found: List[Dict[str, Any]] = []
    if not base.is_dir():
        return found

    def walk(d: Path, depth: int) -> None:
        if (d / "tools" / "tradefed.jar").exists():
            found.append(inspect_package(str(d)))
            return
        if depth >= max_depth:
            return
        try:
            children = sorted(c for c in d.iterdir() if c.is_dir() and not c.name.startswith("."))
        except OSError:
            return
        for c in children[:200]:
            if c.name in ("testcases", "results", "logs", "jdk", "subplans"):
                continue
            walk(c, depth + 1)

    walk(base, 0)
    return found


def has_llama_cpp_pkg() -> bool:
    import importlib.util

    return importlib.util.find_spec("llama_cpp") is not None


def check_tool_path(kind: str, path: str) -> Dict[str, Any]:
    """Validate a host tool path from the Settings page."""
    if kind == "llama_model_path":
        if not path:
            return {"ok": False, "note": "enter an absolute path to a .gguf model file"}
        if not _PATH_RE.match(path):
            return {"ok": False, "note": "must be an absolute path"}
        mp = Path(path)
        if not mp.is_file() or not os.access(mp, os.R_OK):
            return {"ok": False, "note": "model file not found or not readable"}
        sz = mp.stat().st_size
        size_label = f"{sz / (1024 ** 3):.1f} GB" if sz >= 100 * 1024 * 1024 else f"{max(1, sz // 1024)} KB"
        if not has_llama_cpp_pkg():
            return {
                "ok": False,
                "note": f"file found ({size_label}), but llama-cpp-python is not installed — install `.[ai]` or run `llama-server -m {mp.name} --port 8080` and choose Local/LAN Server",
            }
        return {"ok": True, "note": f"GGUF model ready ({size_label}, llama-cpp-python installed)"}
    if not path:
        return {"ok": True, "note": "not set: taken from PATH / environment"}
    if not _PATH_RE.match(path):
        return {"ok": False, "note": "must be an absolute path"}
    p = Path(path)
    if kind == "java_home":
        ok = (p / "bin" / "java").exists()
        return {"ok": ok, "note": "bin/java found" if ok else "bin/java not found"}
    if kind == "android_sdk":
        tools = sorted((p / "build-tools").glob("*/aapt2")) if (p / "build-tools").is_dir() else []
        return {"ok": bool(tools), "note": f"aapt2 in build-tools/{tools[-1].parent.name}" if tools else "no build-tools/*/aapt2"}
    if kind == "adb_path":
        target = p / "adb" if p.is_dir() else p
        ok = target.exists() and os.access(target, os.X_OK)
        return {"ok": ok, "note": "adb found" if ok else "adb not found or not executable"}
    if kind == "xts_packages_dir":
        ok = p.is_dir()
        return {"ok": ok, "note": "exists" if ok else "directory does not exist"}
    return {"ok": p.exists(), "note": ""}


def validate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise and validate a settings document posted by the console; raises ValueError."""
    out: Dict[str, Any] = {}
    paths = data.get("paths") or {}
    clean_paths = {}
    for key in ("xts_packages_dir", "java_home", "android_sdk", "adb_path"):
        value = str(paths.get(key) or "").strip()
        if value and not _PATH_RE.match(value):
            raise ValueError(f"paths.{key} must be an absolute path")
        if value:
            clean_paths[key] = value
    if clean_paths:
        out["paths"] = clean_paths
    suites = data.get("suites") or {}
    items = []
    seen = set()
    for raw in suites.get("installations") or []:
        suite = str(raw.get("suite") or "").lower()
        arch = str(raw.get("arch") or "")
        path = str(raw.get("path") or "").strip()
        if suite not in SUITES:
            raise ValueError(f"unknown suite {suite!r}; one of {', '.join(SUITES)}")
        if arch not in ARCHES:
            raise ValueError(f"unknown architecture {arch!r}; one of {', '.join(ARCHES)}")
        if not _PATH_RE.match(path):
            raise ValueError(f"{suite} {arch}: path must be absolute")
        if (suite, arch) in seen:
            raise ValueError(f"{suite} {arch} is listed twice")
        seen.add((suite, arch))
        item: Dict[str, Any] = {"suite": suite, "arch": arch, "path": path}
        if raw.get("default"):
            item["default"] = True
        items.append(item)
    default_arch = str(suites.get("default_arch") or "")
    if default_arch and default_arch not in ARCHES:
        raise ValueError(f"default architecture must be one of {', '.join(ARCHES)}")
    if items or default_arch:
        out["suites"] = {"default_arch": default_arch or ARCHES[0], "installations": items}
    ai = data.get("ai_rca") or {}
    if not isinstance(ai, dict):
        raise ValueError("ai_rca must be an object")
    clean_ai: Dict[str, Any] = {}
    raw_provider = str(ai.get("provider") or "").strip().lower()
    if raw_provider and raw_provider not in AI_PROVIDERS:
        raise ValueError(f"ai_rca.provider must be one of {', '.join(AI_PROVIDERS)}")
    for key in ("openai_base_url", "openai_model", "gemini_model"):
        value = str(ai.get(key) or "").strip()
        if value and (len(value) > 300 or "\n" in value or "\r" in value):
            raise ValueError(f"ai_rca.{key} is invalid")
        if value:
            clean_ai[key] = value
    if clean_ai.get("openai_base_url") and not re.match(r"^https?://", clean_ai["openai_base_url"]):
        raise ValueError("ai_rca.openai_base_url must start with http:// or https://")
    llama_path = str(ai.get("llama_model_path") or "").strip()
    if llama_path:
        if len(llama_path) > 500 or not _PATH_RE.match(llama_path):
            raise ValueError("ai_rca.llama_model_path must be an absolute file path")
        clean_ai["llama_model_path"] = llama_path
    if ai.get("llama_n_ctx") not in (None, ""):
        try:
            n_ctx = int(ai["llama_n_ctx"])
        except (TypeError, ValueError):
            raise ValueError("ai_rca.llama_n_ctx must be an integer") from None
        if not (512 <= n_ctx <= 131072):
            raise ValueError("ai_rca.llama_n_ctx must be between 512 and 131072")
        clean_ai["llama_n_ctx"] = n_ctx
    if ai.get("llama_n_gpu_layers") not in (None, ""):
        try:
            n_gpu = int(ai["llama_n_gpu_layers"])
        except (TypeError, ValueError):
            raise ValueError("ai_rca.llama_n_gpu_layers must be an integer") from None
        if not (-1 <= n_gpu <= 999):
            raise ValueError("ai_rca.llama_n_gpu_layers must be between -1 and 999")
        clean_ai["llama_n_gpu_layers"] = n_gpu
    if "allow_external_providers" in ai and ai.get("allow_external_providers") is not None:
        clean_ai["allow_external_providers"] = bool(ai["allow_external_providers"])
    if "enabled" in ai and ai.get("enabled") is not None:
        clean_ai["enabled"] = bool(ai["enabled"])
    if raw_provider == "none":
        clean_ai["provider"] = "none"
        clean_ai["enabled"] = False
        out["ai_rca"] = clean_ai
    elif raw_provider or clean_ai:
        provider = raw_provider or ("llama_cpp" if clean_ai.get("llama_model_path") and not (clean_ai.get("openai_base_url") or clean_ai.get("openai_model")) else "openai_compatible")
        if provider == "llama_cpp" and not clean_ai.get("llama_model_path"):
            raise ValueError("ai_rca.llama_model_path is required when provider is llama_cpp")
        clean_ai["provider"] = provider
        out["ai_rca"] = clean_ai
    return out
