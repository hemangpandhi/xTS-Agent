from __future__ import annotations

from .base_suite import BaseSuite


class VtsSuite(BaseSuite):
    @property
    def name(self) -> str:
        return "VTS"

    @property
    def command(self) -> str:
        return "vts"

    @property
    def plan(self) -> str:
        return "vts"
