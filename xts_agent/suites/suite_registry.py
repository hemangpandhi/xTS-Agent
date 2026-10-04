from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Optional, Type
import logging
from .base_suite import BaseSuite
from .cts_suite import CtsSuite
from .vts_suite import VtsSuite
from .sts_suite import StsSuite
from .gts_suite import GtsSuite
from .ats_suite import AtsSuite
from .catbox_suite import CatboxSuite

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

    def discover_installed_suites(self, base_path: Path) -> List[str]:
        installed = []
        for name, cls in self._suite_types.items():
            suite_dir = base_path / f"android-{name}"
            if suite_dir.exists() and suite_dir.is_dir():
                installed.append(name.upper())
                self._suites[name.upper()] = cls(base_path)
        return installed

    def get_suite(self, name: str) -> Optional[BaseSuite]:
        return self._suites.get(name.upper())

    def get_all_suites(self) -> Dict[str, BaseSuite]:
        return self._suites
