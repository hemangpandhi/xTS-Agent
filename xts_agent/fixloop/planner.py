"""Turn a failing run into an ordered list of remediations to try.

Rules first: catalog patterns over the failure text, confirmed against live
device state where the remediation has a ``detect`` probe. The local LLM is
asked second, for a root cause grounded on OEM code and for catalog ids the
rules missed. The LLM can only choose ids from the catalog; it never supplies
commands. Rule and AI classifications are kept side by side, never merged.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from xts_agent.triage.ai_rca import CLASSIFICATIONS, load_json_object, parse_ai_json

from .catalog import Remediation, RemediationCatalog
from .device import DeviceSession
from .executor import RunOutcome
from .oem_sources import OemCodeLocator

logger = logging.getLogger(__name__)

PROMPT = """You are an Android Automotive (AAOS) xTS triage engineer working in an OEM lab.
A targeted CTS run on a real device failed. Decide the most likely root cause.

Failures (test id, message, stack):
{failures}

Device state (read-only probes):
{device}

OEM source code found for terms in the failure (file:line):
{code}

Allow-listed remediations you may choose from (ids only; you cannot run anything else):
{catalog}

Rules:
- ENVIRONMENT_ISSUE / INFRASTRUCTURE_FAILURE: lab state that differs from how the device ships (network,
  accounts, screen lock, leftover debug/test apps, host tools); fixable without changing the build.
- PRODUCT_BUG: the device/OEM code behaves wrongly; needs a code change in OEM or platform code.
  A system component of the build under test (SystemUI, launcher, settings, a system window,
  an overlay/config value) behaving against the test is a PRODUCT_BUG, not an environment issue.
- TEST_BUG: the CTS test itself is wrong (rare; never propose editing CTS to pass certification).
- FLAKY_TEST: timing-dependent; passes on retry.
Return only JSON:
{{"classification": "<one of {classes}>", "confidence": <0..1>,
 "root_cause": "<2-3 sentences, cite file:line when the OEM code shows it>",
 "remediation_ids": ["<catalog id>", ...],
 "suggested_fix": "<for PRODUCT_BUG: the code change in OEM code, file and what to change>",
 "evidence": "<the lines from the failure or code that prove it>"}}
"""


SEARCH_PROMPT = """An Android Automotive CTS test failed. List up to 4 identifiers to search for in the
OEM's source tree (system apps such as CarLauncher, CarSettings, CarSystemUI, Dialer; framework
overlays; device config) that would most likely show the cause: resource/config names, class or
method names, role or permission names, system property keys. Use names exactly as they appear in
Android source (e.g. framework resource names like config_xxx, package names like com.oem.app),
not descriptions. Identifiers only, no sentences.

{failures}

Return only JSON: {{"terms": ["...", "..."]}}
"""


@dataclass
class Candidate:
    remediation: Remediation
    source: str  # rule | ai
    evidence: str
    match: Optional[re.Match] = None

    def to_dict(self) -> Dict[str, Any]:
        return {**self.remediation.summary(), "source": self.source, "evidence": self.evidence[:400]}


@dataclass
class Diagnosis:
    rule_category: str
    candidates: List[Candidate] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)  # rule hit but device state fine
    ai: Dict[str, Any] = field(default_factory=dict)
    oem: Dict[str, Any] = field(default_factory=dict)
    device: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_category": self.rule_category,
            "candidates": [c.to_dict() for c in self.candidates],
            "skipped": self.skipped,
            "ai": self.ai,
            "oem": self.oem,
            "device": self.device,
        }


REFINE_PROMPT = """An Android Automotive CTS test failed. A first search of the OEM source found:

{hits}

Failure:
{failures}

The defect may be in code these hits point to rather than in the hits themselves (e.g. the screen's
layout/preference XML that references a resource, the resource a screen uses instead of the expected
one, the class that creates a window). List up to 4 more identifiers to search for next: exact
resource names, XML file names without extension, class or method names. Identifiers only.

Return only JSON: {{"terms": ["...", "..."]}}
"""


class Planner:
    # One LLM request at a time across all DUT workers: a shared on-prem
    # server answers one request fast, but four at once can all time out
    _llm_lock = threading.Lock()

    def __init__(
        self,
        catalog: RemediationCatalog,
        locator: Optional[OemCodeLocator] = None,
        provider: Any = None,
    ):
        self.catalog = catalog
        self.locator = locator
        self.provider = provider

    def diagnose(self, outcome: RunOutcome, device: DeviceSession, tried: List[str]) -> Diagnosis:
        text = outcome.failure_text()
        ok, why = device.healthy()
        snapshot = device.snapshot() if ok else {"unreachable": why}
        diag = Diagnosis(rule_category="PRODUCT_BUG", device=snapshot)

        for rem, m in self.catalog.matching(text):
            if rem.id in tried:
                continue
            confirmed, why = self._confirm(rem, device)
            if not confirmed:
                diag.skipped.append({"id": rem.id, "reason": why})
                continue
            diag.candidates.append(Candidate(rem, "rule", f"matched {m.group(0)[:160]!r}; {why}", m))
        if diag.candidates:
            diag.rule_category = diag.candidates[0].remediation.category

        # The LLM is asked only where it adds something: nothing in the catalog
        # matched, or the leading cause is product code and the owning team
        # needs a root cause grounded on OEM source
        use_llm = self.provider is not None and (
            not diag.candidates or diag.rule_category == "PRODUCT_BUG"
        )
        context = "\n".join(f"{k}: {v}" for k, v in snapshot.items())
        if self.locator is not None:
            terms = self._ask_search_terms(outcome) if use_llm else []
            # The window that holds focus names the code that creates it
            terms = focus_window_terms(text, snapshot) + terms
            diag.oem = self.locator.locate(text, context, extra_terms=terms)
            if use_llm and diag.oem.get("hits"):
                # Second round: follow the first hits to where the defect is
                more = [t for t in self._ask_search_terms(outcome, diag.oem["hits"]) if t not in diag.oem["terms"]]
                if more:
                    nxt = self.locator.locate(text, context, max_hits=6, extra_terms=more, failure_terms=False)
                    seen = {(h["file"], h["line"]) for h in diag.oem["hits"]}
                    diag.oem["hits"] += [h for h in nxt["hits"] if (h["file"], h["line"]) not in seen]
                    diag.oem["terms"] += nxt["terms"]
                    terms += more
            diag.oem["llm_terms"] = terms

        if use_llm:
            diag.ai = self._ask_llm(outcome, snapshot, diag.oem, tried)
            rule_ids = {c.remediation.id for c in diag.candidates}
            for rid in diag.ai.get("remediation_ids", []):
                rem = self.catalog.by_id.get(rid)
                if rem is None or rid in rule_ids or rid in tried:
                    continue
                if any("{m" in c for c in rem.apply) and rem.find(text) is None:
                    diag.skipped.append({"id": rid, "reason": "AI suggestion needs a value the failure text does not contain"})
                    continue
                if diag.ai.get("classification") == "PRODUCT_BUG" and rem.category != "PRODUCT_BUG":
                    # A lab/infra action cannot fix what the LLM itself calls a product bug
                    diag.skipped.append({"id": rid, "reason": "AI suggestion contradicts its own PRODUCT_BUG classification"})
                    continue
                confirmed, why = self._confirm(rem, device)
                if confirmed:
                    diag.candidates.append(Candidate(rem, "ai", f"chosen by {diag.ai.get('model', 'LLM')}; {why}"))
                else:
                    diag.skipped.append({"id": rid, "reason": f"AI suggestion rejected: {why}"})
        return diag

    @staticmethod
    def _confirm(rem: Remediation, device: DeviceSession) -> tuple:
        """Check the device is really in the state the remediation fixes."""
        if rem.scope != "device" or not rem.detect:
            return True, "no device probe needed"
        res = device.shell(rem.detect, timeout=30)
        if rem.broken_if and re.search(rem.broken_if, res.out, re.IGNORECASE | re.MULTILINE):
            return True, f"probe confirmed: {res.out.strip()[:160]!r}"
        return False, f"probe shows device OK: {res.out.strip()[:160]!r}"

    def _ask_search_terms(self, outcome: RunOutcome, hits: Optional[List[Dict[str, Any]]] = None) -> List[str]:
        """Tool step: which OEM symbols/resources/config keys to look up (again, given ``hits``)."""
        failures = "\n\n".join(f"{f.test_id}\n{f.message[:400]}\n{f.stack[:1200]}" for f in outcome.failures[:3])
        if hits:
            prompt = REFINE_PROMPT.format(hits="\n".join(_hit_line(h) for h in hits[:10]), failures=failures[:3000])
        else:
            prompt = SEARCH_PROMPT.format(failures=failures[:5000])
        try:
            with self._llm_lock:
                raw = self.provider.generate(prompt, json_mode=True) or ""
            data = load_json_object(raw)
        except Exception as exc:
            logger.warning("LLM search-term step failed: %s", exc)
            return []
        terms = [str(t).strip() for t in data.get("terms") or [] if isinstance(t, (str, int))]
        # Identifiers only: these go to grep / the OEM MCP server
        return [t for t in terms if re.fullmatch(r"[A-Za-z0-9_.:/$-]{4,80}", t)][:4]

    def _ask_llm(self, outcome: RunOutcome, snapshot: Dict[str, str], oem: Dict[str, Any], tried: List[str]) -> Dict[str, Any]:
        failures = "\n\n".join(
            f"[{f.kind}] {f.test_id}\n{f.message[:600]}\n{f.stack[:1500]}" for f in outcome.failures[:4]
        ) + ("\n" + "\n".join(outcome.setup_errors[:5]) if outcome.setup_errors else "")
        code = "\n\n".join(f"--- {h['file']}:{h['line']} (term {h['term']})\n{h['snippet']}" for h in (oem or {}).get("hits", [])) or "(none found)"
        catalog = "\n".join(
            f"- {r.id}: {r.title} [{r.category}]" + (" (already tried)" if r.id in tried else "")
            for r in self.catalog.remediations
        )
        prompt = PROMPT.format(
            failures=failures[:7000],
            device=json.dumps(snapshot, indent=1)[:2500],
            code=code[:6000],
            catalog=catalog,
            classes="|".join(CLASSIFICATIONS),
        )
        try:
            with self._llm_lock:
                raw = self.provider.generate(prompt, json_mode=True) or ""
        except Exception as exc:  # the loop keeps going on rules alone
            logger.warning("LLM diagnosis failed: %s", exc)
            return {"error": str(exc)}
        parsed = parse_ai_json(raw)
        if parsed is None:
            return {"error": "unparseable LLM output", "raw": raw[:500]}
        ids: List[str] = []
        try:
            data = load_json_object(raw)
            ids = [str(i) for i in data.get("remediation_ids") or [] if str(i) in self.catalog.by_id]
        except ValueError:
            pass
        parsed["remediation_ids"] = ids
        parsed["model"] = getattr(self.provider, "model_id", "") or getattr(self.provider, "model", "") or type(self.provider).__name__
        return parsed


def _hit_line(hit: Dict[str, Any]) -> str:
    """``file:line: <source line>`` for one code hit."""
    text = ""
    for ln in str(hit.get("snippet", "")).splitlines():
        num, _, rest = ln.strip().partition("  ")
        if num == str(hit["line"]):
            text = rest.strip()
    return f"{hit['file']}:{hit['line']}: {text[:160]}"


def focus_window_terms(failure_text: str, snapshot: Dict[str, str]) -> List[str]:
    """For focus failures held by a system window: its type constant, e.g. TYPE_STATUS_BAR_ADDITIONAL."""
    if not re.search(r"focus", failure_text, re.I):
        return []
    m = re.search(r"\bty=([A-Z_]+)", snapshot.get("focused_window", ""))
    if not m or m.group(1) in ("BASE_APPLICATION", "APPLICATION", "APPLICATION_STARTING"):
        return []
    # FLAG_NOT_FOCUSABLE is what keeps a system window from taking focus from apps
    return ["TYPE_" + m.group(1), "FLAG_NOT_FOCUSABLE"]
