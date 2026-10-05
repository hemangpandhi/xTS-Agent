from __future__ import annotations

from .base_suite import BaseSuite


class AtsSuite(BaseSuite):
    @property
    def name(self) -> str:
        return "ATS"

    @property
    def command(self) -> str:
        return "ats"

    @property
    def plan(self) -> str:
        return "ats"
