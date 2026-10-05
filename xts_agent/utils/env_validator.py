"""Environment validation helpers for TradeFed / Android SDK."""

from __future__ import annotations

import logging
import os
import re
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
    def validate_aapt2(tradefed_script_path: Path, repair: bool = True) -> bool:
        """Ensures TradeFed can resolve a working aapt2 binary.

        When ``repair`` is True and ANDROID_HOME/SDK is discoverable, rewrites
        broken aapt2 mappings in the TradeFed launcher script in place.
        """
        tradefed_script_path = Path(tradefed_script_path)
        if not tradefed_script_path.exists():
            logger.warning("TradeFed script not found: %s", tradefed_script_path)
            return False

        logger.info("Running pre-flight check on AAPT2 parser...")
        content = tradefed_script_path.read_text(encoding="utf-8", errors="replace")

        broken = "$(type -P aapt2" in content or "/usr/bin/aapt2" in content
        if not broken:
            logger.info("AAPT2 environment mapping looks healthy.")
            return True

        logger.warning("Detected fragile/system AAPT2 mapping in TradeFed script")
        aapt2 = EnvironmentValidator.find_aapt2()
        if not aapt2:
            logger.error(
                "Could not find aapt2. Set ANDROID_HOME or ANDROID_SDK_ROOT to your SDK."
            )
            return False

        if not repair:
            logger.info("Valid aapt2 found at %s (repair disabled)", aapt2)
            return True

        logger.info("Auto-repairing TradeFed script with aapt2: %s", aapt2)
        new_content = re.sub(r"--aapt=.*?\\", f"--aapt={aapt2} \\\\", content)
        # Also replace bare /usr/bin/aapt2 if present
        new_content = new_content.replace("/usr/bin/aapt2", str(aapt2))
        backup = tradefed_script_path.with_suffix(tradefed_script_path.suffix + ".bak")
        if not backup.exists():
            backup.write_text(content, encoding="utf-8")
        tradefed_script_path.write_text(new_content, encoding="utf-8")
        return True
