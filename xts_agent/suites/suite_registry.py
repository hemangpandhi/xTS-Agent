"""Installed xTS packages under ``paths.xts_packages_dir`` (``android-<suite>``)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

KNOWN_SUITES = ("cts", "vts", "sts", "gts", "ats", "catbox")


class SuiteRegistry:
    def __init__(self) -> None:
        self._dirs: Dict[str, Path] = {}

    def discover_installed_suites(self, base_path: Path) -> List[str]:
        installed: List[str] = []
        for name in KNOWN_SUITES:
            suite_dir = Path(base_path) / f"android-{name}"
            if suite_dir.is_dir():
                installed.append(name.upper())
                self._dirs[name.upper()] = suite_dir
                logger.info("Discovered suite package: %s", suite_dir)
        return installed

    def suite_dir(self, name: str) -> Optional[Path]:
        return self._dirs.get(name.upper())
