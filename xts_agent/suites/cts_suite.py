from __future__ import annotations

from .base_suite import BaseSuite


class CtsSuite(BaseSuite):
    @property
    def name(self) -> str:
        return "CTS"

    @property
    def command(self) -> str:
        return "cts"

    @property
    def plan(self) -> str:
        return "cts"
