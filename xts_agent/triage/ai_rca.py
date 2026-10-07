"""Structured AI root-cause analysis per failure *group* (not per test).

One LLM call per actionable group (largest first, capped), cached by
signature + model so a recurring root cause is analysed once. The model must
return a JSON object, which is validated: classification is restricted to
the known enum and confidence clamped to [0, 1]. The result is advisory: it
is shown in reports and Jira tickets but never overrides a known-issue or
rule-based classification.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional, Union

from xts_agent.storage.db import Database

from .engine import TriagedGroup, TriageReport

logger = logging.getLogger(__name__)

CLASSIFICATIONS = (
    "PRODUCT_BUG",
    "TEST_BUG",
    "FLAKY_TEST",
    "INFRASTRUCTURE_FAILURE",
    "ENVIRONMENT_ISSUE",
)

PROMPT = """You are an expert Android Automotive (AAOS) platform engineer triaging an xTS
(CTS/VTS/GTS) failure group. All tests below fail with the same signature.

Failure group ({count} failing tests, history: {label}):
- Exception: {exception}
- Normalised message: {message}
- Distinguishing frames: {frames}
- Modules: {modules}
- Example tests:
{tests}
- Build: {fingerprint}

Sample stack trace:
```
{stack}
```

Possibly relevant OEM source code (may be empty or unrelated):
```
{code}
```

Reply with ONLY a JSON object with exactly these keys:
{{"root_cause": "<one or two sentences>",
  "classification": "<one of {classes}>",
  "confidence": <number 0..1, how sure you are>,
  "suggested_fix": "<concrete next step or patch direction>",
  "evidence": "<which lines of the stack/message/code support this>"}}
Use ENVIRONMENT_ISSUE for lab/device setup problems, INFRASTRUCTURE_FAILURE for
adb/host/TradeFed problems, PRODUCT_BUG when platform behaviour is wrong."""


def parse_ai_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract and validate the model's JSON object; None if unusable."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("root_cause"):
        return None
    cls = str(data.get("classification", "")).upper().strip()
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "root_cause": str(data["root_cause"])[:1000],
        "classification": cls if cls in CLASSIFICATIONS else "UNKNOWN",
        "confidence": round(min(max(confidence, 0.0), 1.0), 2),
        "suggested_fix": str(data.get("suggested_fix", ""))[:2000],
        "evidence": str(data.get("evidence", ""))[:2000],
    }


class _Cache:
    def __init__(self, db: Any):
        self.db = db if isinstance(db, Database) else Database(db)
        self.db.ddl(
            "CREATE TABLE IF NOT EXISTS ai_cache (signature TEXT, model TEXT, result TEXT, "
            "created {float}, PRIMARY KEY (signature, model))"
        )

    def get(self, signature: str, model: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one(
            "SELECT result FROM ai_cache WHERE signature = ? AND model = ?", (signature, model)
        )
        return json.loads(row[0]) if row else None

    def put(self, signature: str, model: str, result: Dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO ai_cache VALUES (?, ?, ?, ?) ON CONFLICT (signature, model) "
            "DO UPDATE SET result = excluded.result, created = excluded.created",
            (signature, model, json.dumps(result), time.time()),
        )


class GroupAIAnalyzer:
    def __init__(self, provider: Any, indexer: Any = None, cache_db: Union[str, Database, None] = None, max_groups: int = 20):
        self.provider = provider
        self.indexer = indexer
        self.cache = _Cache(cache_db) if cache_db else None
        self.max_groups = max_groups

    def prompt(self, tg: TriagedGroup, fingerprint: str) -> str:
        g = tg.group
        code = ""
        if self.indexer is not None:
            query = f"{g.exception} {g.message}\n" + "\n".join(g.frames)
            code = self.indexer.search(query, top_k=3)
        return PROMPT.format(
            count=g.count,
            label=tg.label,
            exception=g.exception or "-",
            message=g.message or "-",
            frames=", ".join(g.frames) or "-",
            modules=", ".join(g.modules[:10]),
            tests="\n".join(f"  {t.test_id}" for t in g.tests[:5]),
            fingerprint=fingerprint or "unknown",
            stack=g.sample_stack[:4000],
            code=code[:6000],
            classes="|".join(CLASSIFICATIONS),
        )

    def analyze(self, report: TriageReport) -> Dict[str, int]:
        stats = {"analyzed": 0, "cached": 0, "unparseable": 0}
        model = getattr(self.provider, "model_id", "") or type(self.provider).__name__
        # Actionable groups first; known-but-classified groups (no Jira yet or
        # waived) are included after them to keep measuring AI agreement.
        targets = [tg for tg in report.groups if tg.actionable][: self.max_groups]
        eval_slots = max(0, self.max_groups - len(targets))
        targets += [
            tg for tg in report.groups
            if not tg.actionable and tg.known and tg.known.issue.classification
        ][:eval_slots]
        for tg in targets:
            sig = tg.group.signature
            cached = self.cache.get(sig, model) if self.cache else None
            if cached:
                tg.ai = {**cached, "cached": True}
                stats["cached"] += 1
                continue
            raw = self.provider.generate(self.prompt(tg, report.fingerprint), json_mode=True)
            result = parse_ai_json(raw or "")
            if result is None:
                logger.warning("AI output for group %s was not valid JSON; ignored", sig)
                stats["unparseable"] += 1
                continue
            result["model"] = model
            tg.ai = result
            if self.cache:
                self.cache.put(sig, model, result)
            stats["analyzed"] += 1
        logger.info("AI RCA over %s group(s): %s", len(targets), stats)
        return stats
