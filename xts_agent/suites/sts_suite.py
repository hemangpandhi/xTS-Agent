from __future__ import annotations

from .base_suite import BaseSuite


class StsSuite(BaseSuite):
    @property
    def name(self) -> str:
        return "STS"

    @property
    def command(self) -> str:
        return "sts"

    @property
    def plan(self) -> str:
        return "sts-dynamic-full"
