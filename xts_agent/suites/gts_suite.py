from __future__ import annotations

from .base_suite import BaseSuite


class GtsSuite(BaseSuite):
    @property
    def name(self) -> str:
        return "GTS"

    @property
    def command(self) -> str:
        return "gts"

    @property
    def plan(self) -> str:
        return "gts"
