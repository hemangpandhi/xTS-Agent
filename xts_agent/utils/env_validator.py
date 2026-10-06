"""Environment validation helpers for TradeFed / Android SDK."""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class EnvironmentValidator:
    @staticmethod
    def resolve_android_sdk() -> Optional[Path]:
        for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            value = os.environ.get(key)
            if value and Path(value).exists():
                return Path(value)
        # Common fallbacks (non-destructive discovery only)
        for candidate in (
            Path.home() / "Android" / "Sdk",
            Path("/opt/android-sdk"),
            Path("/usr/lib/android-sdk"),
        ):
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def find_aapt2(sdk_root: Optional[Path] = None) -> Optional[Path]:
        sdk_root = sdk_root or EnvironmentValidator.resolve_android_sdk()
        if not sdk_root:
            return None
        build_tools = sdk_root / "build-tools"
        if not build_tools.is_dir():
            return None
        versions = sorted(
            [d for d in build_tools.iterdir() if d.is_dir()],
            reverse=True,
        )
        for version_dir in versions:
            aapt2 = version_dir / "aapt2"
            if aapt2.exists():
                return aapt2
        return None

    @staticmethod
    def ensure_aapt2_on_path() -> Optional[Path]:
        """Make aapt2 resolvable for TradeFed via PATH, without touching xTS files.

        Stock ``*-tradefed`` launchers resolve aapt2 with ``type -P aapt2``, so
        prepending the SDK build-tools dir to this process's PATH (inherited by
        the TradeFed subprocess) is enough. Certification packages must stay
        unmodified, so the launcher script is never rewritten.
        """
        # Prefer SDK build-tools: distro aapt2 (/usr/bin) is often too old for
        # current xTS APKs.
        aapt2 = EnvironmentValidator.find_aapt2()
        if not aapt2:
            existing = shutil.which("aapt2")
            if existing:
                logger.warning("Using aapt2 from PATH (no SDK build-tools found): %s", existing)
                return Path(existing)
            logger.error(
                "Could not find aapt2. Set ANDROID_HOME or ANDROID_SDK_ROOT to your SDK "
                "or put build-tools on PATH."
            )
            return None
        os.environ["PATH"] = f"{aapt2.parent}{os.pathsep}{os.environ.get('PATH', '')}"
        sdk = EnvironmentValidator.resolve_android_sdk()
        if sdk:
            os.environ.setdefault("ANDROID_HOME", str(sdk))
            os.environ.setdefault("ANDROID_SDK_ROOT", str(sdk))
        logger.info("Prepended %s to PATH for TradeFed", aapt2.parent)
        return aapt2

    @staticmethod
    def check_tradefed_script(tradefed_script_path: Path) -> bool:
        """Read-only check: warn if the launcher was modified with a stale aapt path."""
        tradefed_script_path = Path(tradefed_script_path)
        if not tradefed_script_path.exists():
            logger.warning("TradeFed script not found: %s", tradefed_script_path)
            return False
        content = tradefed_script_path.read_text(encoding="utf-8", errors="replace")
        ok = True
        for match in re.finditer(r"--aapt='([^'$]+)'", content):
            ok = False
            logger.warning(
                "%s has a hard-coded --aapt=%s (stock launchers use `type -P aapt2`). "
                "Restore the unmodified script before certification runs.%s",
                tradefed_script_path,
                match.group(1),
                "" if Path(match.group(1)).exists() else " That path does not exist.",
            )
        return ok
