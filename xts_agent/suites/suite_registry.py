from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Type

from .ats_suite import AtsSuite
from .base_suite import BaseSuite
from .catbox_suite import CatboxSuite
from .cts_suite import CtsSuite
from .gts_suite import GtsSuite
from .sts_suite import StsSuite
from .vts_suite import VtsSuite

logger = logging.getLogger(__name__)


class SuiteRegistry:
    def __init__(self):
        self._suite_types: Dict[str, Type[BaseSuite]] = {
            "cts": CtsSuite,
            "vts": VtsSuite,
            "sts": StsSuite,
            "gts": GtsSuite,
            "ats": AtsSuite,
            "catbox": CatboxSuite,
        }
        self._suites: Dict[str, BaseSuite] = {}
        self._base_path: Optional[Path] = None

    def discover_installed_suites(self, base_path: Path) -> List[str]:
        self._base_path = Path(base_path)
        installed: List[str] = []
        for name, cls in self._suite_types.items():
            suite_dir = self._base_path / f"android-{name}"
            if suite_dir.exists() and suite_dir.is_dir():
                installed.append(name.upper())
                self._suites[name.upper()] = cls(self._base_path)
                logger.info("Discovered suite package: %s", suite_dir)
        return installed

    def get_suite(self, name: str) -> Optional[BaseSuite]:
        return self._suites.get(name.upper())

    def get_all_suites(self) -> Dict[str, BaseSuite]:
        return self._suites

    def resolve_package_path(self, name: str) -> Optional[Path]:
        suite = self.get_suite(name)
        if suite:
            return suite.suite_dir
        if self._base_path:
            candidate = self._base_path / f"android-{name.lower()}"
            if candidate.exists():
                return candidate
        return None
