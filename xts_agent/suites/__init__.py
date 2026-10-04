from __future__ import annotations
from .base_suite import BaseSuite, SuiteResult, TestResults
from .cts_suite import CtsSuite
from .vts_suite import VtsSuite
from .sts_suite import StsSuite
from .gts_suite import GtsSuite
from .ats_suite import AtsSuite
from .catbox_suite import CatboxSuite
from .suite_registry import SuiteRegistry
from .suite_downloader import SuiteDownloader

__all__ = [
    "BaseSuite", "SuiteResult", "TestResults",
    "CtsSuite", "VtsSuite", "StsSuite", "GtsSuite", "AtsSuite", "CatboxSuite",
    "SuiteRegistry", "SuiteDownloader"
]
