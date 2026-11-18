"""Interactive triage workspace: deterministic Level-1 root-cause groups,
persisted discussion threads & human verdicts (keyed by signature), on-demand
Level-2 AI RCA + multi-turn grounded chat, and promotion to known_issues.yaml.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from xts_agent.config_loader import AiRcaConfig, RCAPostConfig, TriageConfig, _filter_dataclass_kwargs
from xts_agent.execution.test_plan_executor import PlanResult, SuiteResult, TestPlanExecutor
from xts_agent.execution.tradefed_runner import ExecutionResult
from xts_agent.rca.failure_classifier import FailureClassifier
from xts_agent.rca.llm_provider import ExternalProviderNotAllowed, LlamaCppProvider, get_llm_provider
from xts_agent.rca.pattern_matcher import PatternMatcher
from xts_agent.rca.rca_engine import RCAEngine
from xts_agent.results.result_parser import TestCaseResult, overall_status
from xts_agent.storage.db import Database

from .ai_rca import CLASSIFICATIONS, GroupAIAnalyzer, parse_ai_json, provider_failed
from .engine import TriagedGroup, TriageEngine, TriageReport
from .history import FailureHistory
from .known_issues import KnownIssueDB
from .ownership import Owner, OwnershipMap
from .signature import FailureGroup

logger = logging.getLogger(__name__)

VERDICT_STATUSES = ("open", "investigating", "confirmed", "waived", "resolved")
_SIG_RE = re.compile(r"^[0-9a-f]{8,24}$")
_JIRA_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,19}-\d{1,8}$")
MAX_MESSAGE_LEN = 4000
MAX_CHAT_HISTORY_IN_PROMPT = 8

KNOWN_ISSUES_HEADER = """\
# Known xTS issues, matched against failure groups during triage.
#
# A group matching an issue is reported as KNOWN (with its Jira key) instead
# of opening a new ticket. All `match` fields given must match:
#   signature      - failure-group signature(s) from the triage report
#   message_regex  - regex over the failure message + stack trace
#   test_regex     - regex over "<abi> <module> <class>#<test>" ids
#   module_regex   - regex over module names
#
# Waivers are report-time only: they never change TradeFed results or suite
# status, every waiver must have an `expires` date, and certification runs
# only honour waivers that list `certification` in `profiles` (default:
# development only).

"""

DISCUSSION_PROMPT = """\
You are an expert Android Automotive (AAOS) & AOSP platform engineer collaborating
in an interactive triage session on a specific xTS failure root-cause group.

=== LEVEL 1 DETERMINISTIC GROUP FACTS ===
- Signature: {signature}
- Failing tests in this group: {count} (History label: {label})
- Exception: {exception}
- Normalised message: {message}
- Distinguishing stack frames: {frames}
- Affected modules: {modules}
- Example tests:
{tests}
- Build fingerprint: {fingerprint}
- Deterministic rule classification: {rule_class}
- Owning team: {team}
- Structural signals: {signals}
- Known issue match: {known_issue}
- Human verdict so far: {verdict}
{prior_ai}

=== SAMPLE STACK TRACE ===
```
{stack}
```

=== DEVICE LOGCAT EXCERPT (around failure) ===
```
{logcat}
```

=== GROUNDED AOSP / OEM SOURCE SNIPPETS ===
```
{code}
```

=== PRIOR DISCUSSION THREAD ===
{history}

=== ENGINEER'S QUESTION ===
{question}

Instructions:
1. Ground your answer strictly in the stack trace, logcat excerpt, deterministic signals, and AOSP/OEM source snippets above. Cite exact `File.java:line` numbers when referencing code.
2. Clearly separate verified evidence from hypotheses when code or logcat is incomplete.
3. Keep the response concise, practical, and well-structured (use short bullet points or exact `adb` / config / Java snippets when helpful).
"""


class _SourcePathsAdapter:
    """Lightweight holder exposing ``source_paths`` for exact stack-frame file lookup
    without loading SentenceTransformers / ChromaDB on interactive UI requests.
    """

    def __init__(self, source_paths: Sequence[str]):
        self.source_paths = list(source_paths or [])


def _parse_test_id(test_id: str, stack: str = "", message: str = "") -> TestCaseResult:
    """Reconstruct a TestCaseResult from '<abi> <module> <class>#<method>' strings."""
    parts = test_id.strip().split(" ")
    target = parts[-1] if parts else test_id
    module = " ".join(parts[:-1]) if len(parts) > 1 else ""
    cls_name, sep, method = target.partition("#")
    if not sep:
        cls_name, method = target, "unknown"
    return TestCaseResult(
        class_name=cls_name,
        test_name=method,
        result="FAIL",
        message=message or None,
        stack_trace=stack or None,
        module=module,
    )


def _group_from_dict(raw: Dict[str, Any]) -> TriagedGroup:
    """Reconstruct a TriagedGroup from a serialized triage_*.json group dict."""
    stack = str(raw.get("sample_stack") or "")
    msg = str(raw.get("message") or "")
    tests = [
        _parse_test_id(str(tid), stack=stack if i == 0 else "", message=msg if i == 0 else "")
        for i, tid in enumerate(raw.get("tests") or [])
    ]
    fg = FailureGroup(
        signature=str(raw.get("signature") or ""),
        exception=str(raw.get("exception") or ""),
        message=msg,
        frames=[str(f) for f in (raw.get("frames") or [])],
        tests=tests,
        suites=[str(s) for s in (raw.get("suites") or [])],
    )
    own = raw.get("owner") or {}
    return TriagedGroup(
        group=fg,
        label=str(raw.get("label") or "NO_HISTORY"),
        label_counts=dict(raw.get("label_counts") or {}),
        owner=Owner(
            team=str(own.get("team") or "xts-triage"),
            jira_component=str(own.get("jira_component") or ""),
            assignee=str(own.get("assignee") or ""),
        ),
        classification=str(raw.get("classification") or "PRODUCT_BUG"),
        last_pass_build=str(raw.get("last_pass_build") or ""),
        first_fail_build=str(raw.get("first_fail_build") or ""),
        jira_key=str(raw.get("jira_key") or ""),
        jira_action=str(raw.get("jira_action") or ""),
        ai=dict(raw["ai"]) if isinstance(raw.get("ai"), dict) else None,
    )


def _format_numbered_snippet(chunk: Dict[str, Any]) -> Dict[str, Any]:
    fpath = str(chunk.get("file") or "")
    start = int(chunk.get("start_line") or 1)
    text = str(chunk.get("text") or "")
    lines = [{"line": start + idx, "text": ln} for idx, ln in enumerate(text.splitlines())]
    parts = Path(fpath).parts
    short = "/".join(parts[-4:]) if len(parts) > 4 else fpath
    return {"file": fpath, "short": short, "start_line": start, "lines": lines, "text": text}


class TriageDiscussionStore:
    """SQLite / PostgreSQL persistence for signature-keyed discussions and human verdicts."""

    def __init__(self, db: Database):
        self.db = db
        self._ready = False
        self._lock = threading.Lock()

    def _ensure_tables(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self.db.ddl(
                "CREATE TABLE IF NOT EXISTS triage_messages ("
                "id {pk}, signature TEXT NOT NULL, run_key TEXT NOT NULL, "
                "role TEXT NOT NULL, author TEXT NOT NULL, content TEXT NOT NULL, "
                "meta TEXT NOT NULL, created_at {float} NOT NULL);"
                "CREATE INDEX IF NOT EXISTS idx_triage_msg_sig ON triage_messages(signature);"
                "CREATE TABLE IF NOT EXISTS triage_verdicts ("
                "signature TEXT PRIMARY KEY, classification TEXT NOT NULL, "
                "status TEXT NOT NULL, jira_key TEXT NOT NULL, owner_team TEXT NOT NULL, "
                "note TEXT NOT NULL, author TEXT NOT NULL, updated_at {float} NOT NULL)"
            )
            self._ready = True

    def list_messages(self, signature: str, limit: int = 100) -> List[Dict[str, Any]]:
        self._ensure_tables()
        rows = self.db.query(
            "SELECT id, signature, run_key, role, author, content, meta, created_at "
            "FROM triage_messages WHERE signature = ? ORDER BY id ASC LIMIT ?",
            (signature, int(limit)),
        )
        out = []
        for mid, sig, run_key, role, author, content, meta_raw, created_at in rows:
            try:
                meta = json.loads(meta_raw or "{}")
            except ValueError:
                meta = {}
            out.append(
                {
                    "id": mid,
                    "signature": sig,
                    "run_key": run_key,
                    "role": role,
                    "author": author,
                    "content": content,
                    "meta": meta,
                    "created_at": created_at,
                }
            )
        return out

    def message_counts(self, signatures: Sequence[str]) -> Dict[str, int]:
        sigs = [s for s in signatures if s]
        if not sigs:
            return {}
        self._ensure_tables()
        placeholders = ",".join("?" for _ in sigs)
        rows = self.db.query(
            f"SELECT signature, COUNT(*) FROM triage_messages WHERE signature IN ({placeholders}) GROUP BY signature",  # noqa: S608
            sigs,
        )
        return {str(sig): int(cnt) for sig, cnt in rows}

    def add_message(
        self,
        signature: str,
        run_key: str,
        role: str,
        author: str,
        content: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._ensure_tables()
        ts = time.time()
        meta_dict = dict(meta or {})
        with self.db.transaction() as tx:
            mid = tx.insert_id(
                "INSERT INTO triage_messages (signature, run_key, role, author, content, meta, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (signature, run_key, role, author, content, json.dumps(meta_dict), ts),
            )
        return {
            "id": mid,
            "signature": signature,
            "run_key": run_key,
            "role": role,
            "author": author,
            "content": content,
            "meta": meta_dict,
            "created_at": ts,
        }

    def get_verdict(self, signature: str) -> Optional[Dict[str, Any]]:
        self._ensure_tables()
        row = self.db.query_one(
            "SELECT signature, classification, status, jira_key, owner_team, note, author, updated_at "
            "FROM triage_verdicts WHERE signature = ?",
            (signature,),
        )
        if not row:
            return None
        return {
            "signature": row[0],
            "classification": row[1],
            "status": row[2],
            "jira_key": row[3],
            "owner_team": row[4],
            "note": row[5],
            "author": row[6],
            "updated_at": row[7],
        }

    def get_verdicts(self, signatures: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        sigs = [s for s in signatures if s]
        if not sigs:
            return {}
        self._ensure_tables()
        placeholders = ",".join("?" for _ in sigs)
        rows = self.db.query(
            "SELECT signature, classification, status, jira_key, owner_team, note, author, updated_at "
            f"FROM triage_verdicts WHERE signature IN ({placeholders})",  # noqa: S608
            sigs,
        )
        return {
            str(r[0]): {
                "signature": r[0],
                "classification": r[1],
                "status": r[2],
                "jira_key": r[3],
                "owner_team": r[4],
                "note": r[5],
                "author": r[6],
                "updated_at": r[7],
            }
            for r in rows
        }

    def save_verdict(
        self,
        signature: str,
        classification: str = "",
        status: str = "open",
        jira_key: str = "",
        owner_team: str = "",
        note: str = "",
        author: str = "",
    ) -> Dict[str, Any]:
        if not _SIG_RE.match(signature):
            raise ValueError("invalid signature")
        cls = str(classification or "").strip().upper()
        if cls and cls not in CLASSIFICATIONS:
            raise ValueError(f"classification must be one of {', '.join(CLASSIFICATIONS)}")
        st = str(status or "open").strip().lower()
        if st not in VERDICT_STATUSES:
            raise ValueError(f"status must be one of {', '.join(VERDICT_STATUSES)}")
        jira = str(jira_key or "").strip().upper()
        if jira and not _JIRA_RE.match(jira):
            raise ValueError("jira_key must look like PROJECT-123")
        team = str(owner_team or "").strip()[:80]
        clean_note = str(note or "").strip()[:2000]
        who = str(author or "").strip()[:60] or getpass.getuser()
        ts = time.time()
        self._ensure_tables()
        self.db.execute(
            "INSERT INTO triage_verdicts (signature, classification, status, jira_key, owner_team, note, author, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (signature) DO UPDATE SET "
            "classification = excluded.classification, status = excluded.status, "
            "jira_key = excluded.jira_key, owner_team = excluded.owner_team, "
            "note = excluded.note, author = excluded.author, updated_at = excluded.updated_at",
            (signature, cls, st, jira, team, clean_note, who, ts),
        )
        return {
            "signature": signature,
            "classification": cls,
            "status": st,
            "jira_key": jira,
            "owner_team": team,
            "note": clean_note,
            "author": who,
            "updated_at": ts,
        }

    def cached_ai_for(self, signatures: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Read latest cached structured AI RCA per signature from ``ai_cache`` if present."""
        sigs = [s for s in signatures if s]
        if not sigs:
            return {}
        try:
            placeholders = ",".join("?" for _ in sigs)
            rows = self.db.query(
                f"SELECT signature, model, result, created FROM ai_cache WHERE signature IN ({placeholders}) ORDER BY created ASC",  # noqa: S608
                sigs,
            )
        except Exception:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for sig, model, raw, _created in rows:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("root_cause"):
                    parsed.setdefault("model", model)
                    parsed["cached"] = True
                    out[str(sig)] = parsed
            except ValueError:
                continue
        return out


class TriageWorkspace:
    """End-to-end interactive triage coordinator used by ``xts-agent ui``."""

    def __init__(self, root: Path, results_dir: Path, db_path: Optional[Path]):
        self.root = Path(root)
        self.results_dir = Path(results_dir)
        self.db_path = Path(db_path) if db_path else (self.results_dir / "xts_agent.db")
        self._ki_lock = threading.Lock()
        self._llm_lock = threading.Lock()
        self._cached_llama_key: Optional[Tuple[str, int, int]] = None
        self._cached_llama_provider: Any = None
        self._fmap_cache: Dict[Tuple[str, ...], Dict[str, List[Path]]] = {}
        self._fmap_lock = threading.Lock()

    def _database(self, defaults: Dict[str, Any]) -> Database:
        agent = defaults.get("agent") or {}
        url = agent.get("database_url") or str(self.db_path)
        return Database(url)

    def store(self, defaults: Dict[str, Any]) -> TriageDiscussionStore:
        return TriageDiscussionStore(self._database(defaults))

    def _resolve_config_path(self, rel_or_abs: str, fallback: str) -> Path:
        raw = Path(rel_or_abs or fallback)
        if raw.is_absolute():
            return raw
        cand = self.root / raw
        return cand if cand.exists() or cand.parent.exists() else raw

    def _triage_configs(self, defaults: Dict[str, Any]) -> Tuple[TriageConfig, RCAPostConfig, AiRcaConfig]:
        t_cfg = TriageConfig(**_filter_dataclass_kwargs(TriageConfig, defaults.get("triage") or {}))
        rca_raw = defaults.get("rca") or {}
        r_cfg = RCAPostConfig(
            enabled=bool(rca_raw.get("enabled", True)),
            ai_powered=bool(rca_raw.get("ai_powered", False)),
            failure_classification=bool(rca_raw.get("classify_failures", True)),
            patterns_file=str(rca_raw.get("patterns_file") or "config/known_failures/patterns.yaml"),
        )
        ai_cfg = AiRcaConfig(**_filter_dataclass_kwargs(AiRcaConfig, defaults.get("ai_rca") or {}))
        if os.environ.get("XTS_GEMINI_API_KEY"):
            ai_cfg.gemini_api_key = os.environ["XTS_GEMINI_API_KEY"]
        if os.environ.get("XTS_LLM_API_KEY"):
            ai_cfg.openai_api_key = os.environ["XTS_LLM_API_KEY"]
        return t_cfg, r_cfg, ai_cfg

    @staticmethod
    def _describe_ai(ai_cfg: AiRcaConfig) -> Dict[str, Any]:
        prov = str(ai_cfg.provider or "openai_compatible").lower()
        if prov in ("none", "disabled", "off"):
            return {"provider": "none", "enabled": False, "model": "disabled (deterministic only)", "base_url": ""}
        if prov == "llama_cpp":
            mp = str(ai_cfg.llama_model_path or "")
            return {
                "provider": "llama_cpp",
                "enabled": True,
                "model": Path(mp).name if mp else "(GGUF path not set)",
                "base_url": mp,
            }
        if prov == "gemini":
            return {
                "provider": "gemini",
                "enabled": True,
                "model": ai_cfg.gemini_model or "gemini-2.5-flash",
                "base_url": "https://generativelanguage.googleapis.com",
            }
        return {
            "provider": "openai_compatible",
            "enabled": True,
            "model": ai_cfg.openai_model or "(not set)",
            "base_url": ai_cfg.openai_base_url or "",
        }

    def _resolve_provider(self, ai_cfg: AiRcaConfig) -> Any:
        prov_name = str(ai_cfg.provider or "").lower()
        if prov_name in ("none", "disabled", "off"):
            raise RuntimeError(
                "AI root-cause analysis is set to Disabled in Settings. "
                "Configure llama.cpp / Ollama / Online AI in Settings, or use 'Copy prompt' / 'Add team note'."
            )
        if prov_name == "llama_cpp":
            key = (str(ai_cfg.llama_model_path or ""), int(ai_cfg.llama_n_ctx), int(ai_cfg.llama_n_gpu_layers))
            with self._llm_lock:
                if self._cached_llama_provider is not None and self._cached_llama_key == key:
                    return self._cached_llama_provider
                old = self._cached_llama_provider
                self._cached_llama_provider = None
                self._cached_llama_key = None
            if old is not None:
                old.unload()
        try:
            provider = get_llm_provider(ai_cfg)
        except ExternalProviderNotAllowed as exc:
            raise RuntimeError(str(exc)) from None
        if isinstance(provider, LlamaCppProvider):
            key = (str(ai_cfg.llama_model_path or ""), int(ai_cfg.llama_n_ctx), int(ai_cfg.llama_n_gpu_layers))
            with self._llm_lock:
                self._cached_llama_key = key
                self._cached_llama_provider = provider
        return provider

    # ------------------------------------------------------------------ runs list

    def list_runs(self, runs_view: Any) -> Dict[str, Any]:
        seen_keys = set()
        seen_run_ids = set()
        items: List[Dict[str, Any]] = []

        for v in runs_view.live():
            rid = v.get("run_id") or ""
            key = rid or v["key"]
            totals = v.get("totals") or {}
            fail = totals.get("fail")
            if fail is None:
                vals = [s.get("fail_count") for s in v.get("suites") or [] if isinstance(s.get("fail_count"), int)]
                fail = sum(vals) if vals else 0
            passed = totals.get("pass")
            if passed is None:
                vals = [s.get("pass_count") for s in v.get("suites") or [] if isinstance(s.get("pass_count"), int)]
                passed = sum(vals) if vals else 0
            ts = v.get("triage_summary") or {}
            items.append(
                {
                    "key": key,
                    "plan_key": v["key"],
                    "plan": v.get("plan") or v["key"],
                    "run_id": rid,
                    "state": v.get("state") or "done",
                    "status": v.get("overall_status") or str(v.get("state") or "").upper(),
                    "started_at": v.get("started_at") or 0,
                    "pass": passed,
                    "fail": fail,
                    "groups": ts.get("groups"),
                    "has_triage": bool(v.get("triage_json")),
                }
            )
            seen_keys.add(key)
            seen_keys.add(v["key"])
            if rid:
                seen_run_ids.add(rid)

        for r in runs_view.history(limit=150):
            rid = r.get("run_id") or ""
            key = r["key"]
            if key in seen_keys or (rid and rid in seen_run_ids):
                continue
            items.append(
                {
                    "key": key,
                    "plan_key": key,
                    "plan": r.get("plan") or key,
                    "run_id": rid,
                    "state": r.get("state") or "done",
                    "status": r.get("status") or "DONE",
                    "started_at": r.get("started_at") or 0,
                    "pass": r.get("pass") or 0,
                    "fail": r.get("fail") or 0,
                    "groups": r.get("triage_groups"),
                    "has_triage": bool(r.get("triage_html") or r.get("triage_groups") is not None),
                }
            )
            seen_keys.add(key)
            if rid:
                seen_run_ids.add(rid)

        items.sort(key=lambda x: -(x.get("started_at") or 0))
        default_run = next((x["key"] for x in items if (x.get("fail") or 0) > 0), items[0]["key"] if items else "")
        return {"runs": items, "default_key": default_run}

    # ------------------------------------------------------------------ deterministic triage load

    def _resolve_run_view(self, key: str, runs_view: Any) -> Dict[str, Any]:
        view = runs_view.get(key)
        if view is None and key.startswith("db-"):
            view = runs_view.detail(key)
        if view is None:
            raise LookupError(f"no such run {key!r}")
        return view

    def _compute_deterministic_triage(
        self, view: Dict[str, Any], defaults: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Level-1 deterministic grouping + rule RCA + history/known-issues/ownership on the fly."""
        t_cfg, r_cfg, _ = self._triage_configs(defaults)
        suites_results: Dict[str, SuiteResult] = {}
        results_dirs: List[str] = []
        for s in view.get("suites") or []:
            sname = str(s.get("name") or "cts")
            rdir = str(s.get("results_dir") or "")
            if rdir and Path(rdir).exists():
                results_dirs.append(rdir)
                exec_res = ExecutionResult(True, s.get("session_id"), 0, 0.0, rdir, "")
                suites_results[sname] = TestPlanExecutor._to_suite_result(
                    sname, exec_res, list(s.get("devices") or []), int(s.get("retry_count") or 0)
                )
        plan_res = PlanResult(
            plan_name=str(view.get("plan") or view.get("key") or "Plan"),
            suites_results=suites_results,
            total_pass=sum(sr.pass_count for sr in suites_results.values()),
            total_fail=sum(sr.fail_count for sr in suites_results.values()),
            total_skip=sum(sr.skip_count for sr in suites_results.values()),
            duration=float((view.get("totals") or {}).get("duration") or 0.0),
            overall_status=overall_status(sr.status for sr in suites_results.values()) if suites_results else "UNKNOWN",
            device_serials=sorted({d for sr in suites_results.values() for d in sr.device_serials}),
            profile=str(view.get("profile") or "development"),
        )
        rca_report = None
        if suites_results and r_cfg.enabled:
            patterns_path = self._resolve_config_path(r_cfg.patterns_file, "config/known_failures/patterns.yaml")
            pm = PatternMatcher(str(patterns_path)) if patterns_path.exists() else None
            fc = FailureClassifier() if r_cfg.failure_classification else None
            rca_report = RCAEngine(r_cfg, diagnostic_collector=None, failure_classifier=fc, pattern_matcher=pm).analyze_plan_results(plan_res)

        db = Database(t_cfg.history_db) if t_cfg.history_db else self._database(defaults)
        ki_path = self._resolve_config_path(t_cfg.known_issues_file, "config/known_issues.yaml")
        own_path = self._resolve_config_path(t_cfg.ownership_file, "config/ownership.yaml")
        engine = TriageEngine(
            history=FailureHistory(db, window=t_cfg.history_window),
            known_issues=KnownIssueDB.load(ki_path),
            ownership=OwnershipMap.load(own_path),
        )
        report = engine.triage(plan_res, rca_report, record_history=False)
        return report.to_dict(), results_dirs

    def _raw_triage_and_dirs(
        self, view: Dict[str, Any], defaults: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], List[str], Optional[Path]]:
        results_dirs = [
            str(s.get("results_dir"))
            for s in (view.get("suites") or [])
            if s.get("results_dir") and Path(str(s.get("results_dir"))).exists()
        ]
        raw_triage_path = str(view.get("triage_json") or "")
        if raw_triage_path:
            cand = Path(raw_triage_path)
            if not cand.is_absolute():
                cand = (self.root / cand) if (self.root / cand).exists() else (self.results_dir.parent / cand)
            if cand.is_file():
                try:
                    data = json.loads(cand.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and "groups" in data:
                        return data, results_dirs, cand
                except (OSError, ValueError) as exc:
                    logger.warning("Could not read %s, recomputing deterministic triage: %s", cand, exc)
        computed, rdirs = self._compute_deterministic_triage(view, defaults)
        return computed, rdirs or results_dirs, None

    def load_triage(self, key: str, runs_view: Any, defaults: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministic Level-1 root-cause groups enriched with live known issues, AI cache, verdicts, and chat counts."""
        view = self._resolve_run_view(key, runs_view)
        t_cfg, _, ai_cfg = self._triage_configs(defaults)
        triage_data, results_dirs, triage_file = self._raw_triage_and_dirs(view, defaults)

        profile = str(triage_data.get("profile") or view.get("profile") or "development")
        fingerprint = str(triage_data.get("fingerprint") or "")
        if not fingerprint:
            fingerprint = next((str(s.get("fingerprint") or "") for s in (view.get("suites") or []) if s.get("fingerprint")), "")

        ki_path = self._resolve_config_path(t_cfg.known_issues_file, "config/known_issues.yaml")
        known_db = KnownIssueDB.load(ki_path)
        raw_groups: List[Dict[str, Any]] = [dict(g) for g in (triage_data.get("groups") or []) if isinstance(g, dict)]
        sigs = [str(g.get("signature") or "") for g in raw_groups]

        disc_store = self.store(defaults)
        verdicts = disc_store.get_verdicts(sigs)
        msg_counts = disc_store.message_counts(sigs)
        ai_cached = disc_store.cached_ai_for(sigs)

        groups: List[Dict[str, Any]] = []
        for idx, g in enumerate(raw_groups, start=1):
            sig = str(g.get("signature") or "")
            tg = _group_from_dict(g)
            live_known = known_db.match(tg.group, profile, fingerprint)
            if live_known:
                g["known_issue"] = {
                    "id": live_known.issue.id,
                    "title": live_known.issue.title,
                    "jira": live_known.issue.jira,
                    "classification": live_known.issue.classification,
                    "waived": live_known.waived,
                    "waiver_expired": live_known.waiver_expired,
                }
                if live_known.issue.classification:
                    g["classification"] = live_known.issue.classification
                if live_known.issue.jira and not g.get("jira_key"):
                    g["jira_key"] = live_known.issue.jira
                g["actionable"] = not live_known.waived and not bool(g.get("jira_key"))
            if not g.get("ai") and sig in ai_cached:
                g["ai"] = ai_cached[sig]
            verdict = verdicts.get(sig)
            if verdict and verdict.get("status") in ("waived", "resolved"):
                g["actionable"] = False
            g["rank"] = idx
            g["rule_classification"] = g.get("classification") or "PRODUCT_BUG"
            g["effective_classification"] = (
                (verdict or {}).get("classification")
                or (g.get("known_issue") or {}).get("classification")
                or g["rule_classification"]
            )
            g["verdict"] = verdict
            g["discussion_count"] = msg_counts.get(sig, 0)
            groups.append(g)

        total_failures = sum(int(g.get("count") or 0) for g in groups)
        summary = {
            "failures": total_failures,
            "groups": len(groups),
            "reduction": round(total_failures / len(groups), 1) if groups else 1.0,
            "actionable_groups": sum(1 for g in groups if g.get("actionable")),
            "known_groups": sum(1 for g in groups if g.get("known_issue")),
            "waived_groups": sum(1 for g in groups if (g.get("known_issue") or {}).get("waived") or (g.get("verdict") or {}).get("status") == "waived"),
            "ai_groups": sum(1 for g in groups if g.get("ai")),
            "reviewed_groups": sum(1 for g in groups if g.get("verdict")),
            "discussed_groups": sum(1 for g in groups if g.get("discussion_count", 0) > 0),
            "by_label": dict(Counter(str(g.get("label") or "NO_HISTORY") for g in groups)),
            "by_classification": dict(Counter(str(g.get("effective_classification") or "PRODUCT_BUG") for g in groups)),
            "by_team": dict(Counter(str((g.get("owner") or {}).get("team") or "xts-triage") for g in groups)),
        }
        totals = view.get("totals") or {}
        return {
            "key": view.get("run_id") or view.get("key") or key,
            "plan_key": view.get("key") or key,
            "plan_name": str(triage_data.get("plan_name") or view.get("plan") or key),
            "profile": profile,
            "fingerprint": fingerprint,
            "started_at": view.get("started_at"),
            "status": view.get("overall_status") or str(view.get("state") or "").upper(),
            "totals": {
                "pass": totals.get("pass"),
                "fail": totals.get("fail", total_failures),
                "skip": totals.get("skip"),
                "duration": totals.get("duration"),
            },
            "suites": [str(s.get("name") or "") for s in (view.get("suites") or []) if s.get("name")],
            "results_dirs": results_dirs,
            "triage_html": str(view.get("triage_html") or ""),
            "triage_source": "file" if triage_file else "computed_deterministic",
            "ai_configured": self._describe_ai(ai_cfg),
            "classifications": list(CLASSIFICATIONS),
            "verdict_statuses": list(VERDICT_STATUSES),
            "summary": summary,
            "groups": groups,
        }

    # ------------------------------------------------------------------ group context & discussion

    @staticmethod
    def _chunks_from_code_refs(refs: Sequence[str], lines_count: int = 45) -> List[Dict[str, Any]]:
        """Fast direct file:line read for pre-recorded AI ``code_refs`` (avoids tree walk)."""
        out: List[Dict[str, Any]] = []
        for ref in refs:
            path_str, _, line_str = str(ref).rpartition(":")
            if not path_str or not line_str.isdigit():
                continue
            p = Path(path_str)
            start = max(1, int(line_str))
            try:
                if p.is_file() and p.stat().st_size <= 2 * 1024 * 1024:
                    all_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                    chunk = all_lines[start - 1 : start - 1 + lines_count]
                    if chunk:
                        out.append({"file": str(p), "start_line": start, "text": "\n".join(chunk)})
            except OSError:
                continue
        return out

    def _build_group_evidence(
        self,
        triage_payload: Dict[str, Any],
        group_dict: Dict[str, Any],
        defaults: Dict[str, Any],
    ) -> Tuple[TriagedGroup, List[Dict[str, Any]], str, List[str], str]:
        _, _, ai_cfg = self._triage_configs(defaults)
        tg = _group_from_dict(group_dict)
        report = TriageReport(
            plan_name=triage_payload["plan_name"],
            profile=triage_payload["profile"],
            fingerprint=triage_payload["fingerprint"],
            groups=[_group_from_dict(g) for g in triage_payload["groups"]],
        )
        src_key = tuple(str(p) for p in (ai_cfg.source_code_paths or []))
        analyzer = GroupAIAnalyzer(
            provider=None,
            indexer=_SourcePathsAdapter(src_key),
            results_dirs=triage_payload.get("results_dirs") or [],
        )
        ai_meta = group_dict.get("ai") if isinstance(group_dict.get("ai"), dict) else {}
        pre_refs = list((ai_meta or {}).get("code_refs") or [])
        raw_chunks = self._chunks_from_code_refs(pre_refs) if pre_refs else []
        if not raw_chunks and src_key:
            with self._fmap_lock:
                cached_map = self._fmap_cache.get(src_key)
            if cached_map is not None:
                analyzer._file_map = cached_map
            raw_chunks = analyzer._resolve_exact_stack_chunks(tg)
            if cached_map is None and analyzer._file_map is not None:
                with self._fmap_lock:
                    self._fmap_cache[src_key] = analyzer._file_map
        logcat = analyzer.retrieve_logcat(tg)
        run_ctx = analyzer.build_run_context(report, current_sig=tg.group.signature)
        signals = analyzer._extract_structural_signals(tg, raw_chunks, run_ctx, logcat)
        return tg, raw_chunks, logcat, signals, run_ctx

    def group_detail(
        self, key: str, signature: str, runs_view: Any, defaults: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not _SIG_RE.match(signature):
            raise ValueError("invalid signature")
        payload = self.load_triage(key, runs_view, defaults)
        group = next((g for g in payload["groups"] if g.get("signature") == signature), None)
        if group is None:
            raise LookupError(f"no group {signature!r} in run {key!r}")
        _, raw_chunks, logcat, signals, _ = self._build_group_evidence(payload, group, defaults)
        disc_store = self.store(defaults)
        return {
            "run_key": payload["key"],
            "plan_name": payload["plan_name"],
            "fingerprint": payload["fingerprint"],
            "group": group,
            "signals": signals,
            "logcat": logcat,
            "snippets": [_format_numbered_snippet(c) for c in raw_chunks],
            "messages": disc_store.list_messages(signature),
            "verdict": disc_store.get_verdict(signature),
        }

    def analyze_group(
        self,
        key: str,
        signature: str,
        runs_view: Any,
        defaults: Dict[str, Any],
        force: bool = False,
    ) -> Dict[str, Any]:
        """Run structured JSON AI RCA on a single root-cause group on demand."""
        if not _SIG_RE.match(signature):
            raise ValueError("invalid signature")
        view = self._resolve_run_view(key, runs_view)
        payload = self.load_triage(key, runs_view, defaults)
        group = next((g for g in payload["groups"] if g.get("signature") == signature), None)
        if group is None:
            raise LookupError(f"no group {signature!r} in run {key!r}")

        _, _, ai_cfg = self._triage_configs(defaults)
        provider = self._resolve_provider(ai_cfg)
        tg, raw_chunks, logcat, signals, run_ctx = self._build_group_evidence(payload, group, defaults)
        db = self._database(defaults)
        analyzer = GroupAIAnalyzer(
            provider=provider,
            indexer=_SourcePathsAdapter(ai_cfg.source_code_paths),
            cache_db=db,
            results_dirs=payload.get("results_dirs") or [],
        )
        model = getattr(provider, "model_id", "") or type(provider).__name__
        if not force and analyzer.cache:
            cached = analyzer.cache.get(signature, model)
            if cached and cached.get("classification") != "UNKNOWN":
                group["ai"] = {**cached, "cached": True}
                return {"ai": group["ai"], "messages": self.store(defaults).list_messages(signature)}

        prompt = analyzer.prompt(tg, payload["fingerprint"], chunks=raw_chunks, run_context=run_ctx, logcat=logcat)
        # Serialize interactive LLM calls across office users and keep the model
        # warm across follow-up questions (Ollama auto-evicts after idle keep_alive).
        with self._llm_lock:
            raw = provider.generate(prompt, json_mode=True)
        if provider_failed(raw):
            raise RuntimeError(raw or "LLM provider unavailable; check Settings -> AI root-cause analysis")
        model = getattr(provider, "model_id", "") or model
        result = parse_ai_json(raw or "")
        if result is None:
            raise RuntimeError("LLM returned non-JSON output; try interactive Ask AI or another model")
        result["model"] = model
        result["prompt_v"] = analyzer.PROMPT_VERSION
        if signals:
            result["signals"] = signals
        result["code_refs"] = [f"{c['file']}:{c['start_line']}" for c in raw_chunks if c.get("file")]
        if analyzer.cache:
            analyzer.cache.put(signature, model, result)

        # Update triage_*.json on disk when available so static views also reflect it
        _, _, triage_file = self._raw_triage_and_dirs(view, defaults)
        if triage_file and triage_file.is_file():
            try:
                disk_data = json.loads(triage_file.read_text(encoding="utf-8"))
                for dg in disk_data.get("groups") or []:
                    if dg.get("signature") == signature:
                        dg["ai"] = result
                triage_file.write_text(json.dumps(disk_data, indent=2), encoding="utf-8")
            except OSError:
                pass

        disc_store = self.store(defaults)
        summary_msg = (
            f"**Structured AI RCA ({result['classification']} · {round(result['confidence'] * 100)}% confidence)**\n"
            f"{result['root_cause']}\n\n"
            + (f"**Suggested fix:** {result['suggested_fix']}\n" if result.get("suggested_fix") else "")
            + (f"**Evidence:** {result['evidence']}" if result.get("evidence") else "")
        ).strip()
        disc_store.add_message(
            signature=signature,
            run_key=payload["key"],
            role="ai",
            author=model,
            content=summary_msg,
            meta={"kind": "structured_rca", "classification": result["classification"],
                  "confidence": result["confidence"], "code_refs": result.get("code_refs") or []},
        )
        return {"ai": result, "messages": disc_store.list_messages(signature)}

    def discuss_group(
        self,
        key: str,
        signature: str,
        message: str,
        mode: str,
        author: str,
        runs_view: Any,
        defaults: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Add a human note or ask the grounded LLM a follow-up question on this root-cause group."""
        if not _SIG_RE.match(signature):
            raise ValueError("invalid signature")
        clean_msg = str(message or "").strip()
        if not clean_msg:
            raise ValueError("message cannot be empty")
        if len(clean_msg) > MAX_MESSAGE_LEN:
            raise ValueError(f"message too long (max {MAX_MESSAGE_LEN} chars)")
        clean_mode = str(mode or "ask").strip().lower()
        if clean_mode not in ("ask", "note"):
            raise ValueError("mode must be 'ask' or 'note'")
        who = str(author or "").strip()[:60] or getpass.getuser()

        payload = self.load_triage(key, runs_view, defaults)
        group = next((g for g in payload["groups"] if g.get("signature") == signature), None)
        if group is None:
            raise LookupError(f"no group {signature!r} in run {key!r}")

        disc_store = self.store(defaults)
        if clean_mode == "note":
            disc_store.add_message(signature, payload["key"], "human", who, clean_msg, {"kind": "note"})
            return {"messages": disc_store.list_messages(signature)}

        _, _, ai_cfg = self._triage_configs(defaults)
        provider = self._resolve_provider(ai_cfg)
        tg, raw_chunks, logcat, signals, _ = self._build_group_evidence(payload, group, defaults)
        prior_msgs = disc_store.list_messages(signature, limit=60)
        verdict = disc_store.get_verdict(signature)

        history_lines = []
        for m in prior_msgs[-MAX_CHAT_HISTORY_IN_PROMPT:]:
            tag = "AI" if m["role"] == "ai" else ("System" if m["role"] == "system" else f"Engineer ({m['author']})")
            history_lines.append(f"[{tag}]: {m['content'][:900]}")
        code_block = "\n".join(
            f"--- File: {c.get('file') or '?'} (line {c.get('start_line', 1)}) ---\n{c.get('text', '')}\n"
            for c in raw_chunks
        )
        ai_info = group.get("ai")
        prior_ai_text = (
            f"- Prior AI RCA: {ai_info.get('classification')} ({round(float(ai_info.get('confidence', 0)) * 100)}%): "
            f"{ai_info.get('root_cause', '')}"
            if isinstance(ai_info, dict) and ai_info.get("root_cause")
            else "- Prior AI RCA: none yet"
        )
        ki = group.get("known_issue")
        ki_text = f"{ki['id']}: {ki['title']} (waived={ki.get('waived')})" if isinstance(ki, dict) else "none"
        verdict_text = (
            f"{verdict['status']} / {verdict['classification'] or 'unset'} — {verdict['note']}"
            if verdict
            else "none yet"
        )
        prompt = DISCUSSION_PROMPT.format(
            signature=signature,
            count=tg.group.count,
            label=tg.label,
            exception=tg.group.exception or "-",
            message=tg.group.message or "-",
            frames=", ".join(tg.group.frames) or "-",
            modules=", ".join(tg.group.modules[:10]) or "-",
            tests="\n".join(f"  {t.test_id}" for t in tg.group.tests[:8]),
            fingerprint=payload["fingerprint"] or "unknown",
            rule_class=group.get("rule_classification") or tg.classification,
            team=tg.owner.team,
            signals=" | ".join(signals) if signals else "none",
            known_issue=ki_text,
            verdict=verdict_text,
            prior_ai=prior_ai_text,
            stack=(tg.group.sample_stack or "")[:3500] or "(empty)",
            logcat=(logcat or "")[:2000] or "(none captured)",
            code=code_block[:5500] or "(no matching local source file found)",
            history="\n".join(history_lines) if history_lines else "(start of discussion)",
            question=clean_msg,
        )
        with self._llm_lock:
            reply = provider.generate(prompt, json_mode=False)
        if provider_failed(reply):
            raise RuntimeError(reply or "LLM provider unavailable; check Settings -> AI root-cause analysis")
        model = getattr(provider, "model_id", "") or type(provider).__name__

        disc_store.add_message(signature, payload["key"], "human", who, clean_msg, {"kind": "question"})
        disc_store.add_message(
            signature,
            payload["key"],
            "ai",
            model,
            reply.strip(),
            {"kind": "answer", "code_refs": [f"{c['file']}:{c['start_line']}" for c in raw_chunks if c.get("file")]},
        )
        return {"messages": disc_store.list_messages(signature)}

    # ------------------------------------------------------------------ human verdict & known_issues.yaml promotion

    def record_verdict(self, body: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
        sig = str(body.get("signature") or "").strip()
        run_key = str(body.get("run_key") or "").strip()
        disc_store = self.store(defaults)
        verdict = disc_store.save_verdict(
            signature=sig,
            classification=str(body.get("classification") or ""),
            status=str(body.get("status") or "open"),
            jira_key=str(body.get("jira_key") or ""),
            owner_team=str(body.get("owner_team") or ""),
            note=str(body.get("note") or ""),
            author=str(body.get("author") or ""),
        )
        parts = [f"Verdict updated: status={verdict['status']}"]
        if verdict["classification"]:
            parts.append(f"class={verdict['classification']}")
        if verdict["jira_key"]:
            parts.append(f"jira={verdict['jira_key']}")
        if verdict["note"]:
            parts.append(f"— {verdict['note']}")
        disc_store.add_message(sig, run_key, "system", verdict["author"], " · ".join(parts), {"kind": "verdict"})
        return {"verdict": verdict, "messages": disc_store.list_messages(sig)}

    def promote_to_known_issues(self, body: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
        """Add or update a signature rule in ``config/known_issues.yaml`` so Level-1 deterministic triage matches it."""
        sig = str(body.get("signature") or "").strip()
        if not _SIG_RE.match(sig):
            raise ValueError("invalid signature")
        title = str(body.get("title") or "").strip()
        if not title or len(title) > 240:
            raise ValueError("title is required (max 240 chars)")
        cls = str(body.get("classification") or "ENVIRONMENT_ISSUE").strip().upper()
        if cls not in CLASSIFICATIONS:
            raise ValueError(f"classification must be one of {', '.join(CLASSIFICATIONS)}")
        jira = str(body.get("jira") or "").strip().upper()
        if jira and not _JIRA_RE.match(jira):
            raise ValueError("jira key must look like PROJECT-123")
        waive = bool(body.get("waive"))
        waiver_block: Optional[Dict[str, Any]] = None
        if waive:
            expires_raw = str(body.get("waiver_expires") or "").strip()
            if not expires_raw:
                raise ValueError("waiver requires an expiration date (YYYY-MM-DD)")
            try:
                exp_date = _dt.date.fromisoformat(expires_raw)
            except ValueError:
                raise ValueError("waiver_expires must be YYYY-MM-DD") from None
            reason = str(body.get("waiver_reason") or title).strip()[:300]
            profiles = [p for p in (body.get("waiver_profiles") or ["development"]) if p in ("development", "certification")]
            if not profiles:
                profiles = ["development"]
            waiver_block = {"reason": reason, "expires": exp_date.isoformat(), "profiles": profiles}
            builds_re = str(body.get("builds_regex") or "").strip()
            if builds_re:
                re.compile(builds_re)
                waiver_block["builds_regex"] = builds_re

        t_cfg, _, _ = self._triage_configs(defaults)
        ki_path = self._resolve_config_path(t_cfg.known_issues_file, "config/known_issues.yaml")

        with self._ki_lock:
            if ki_path.exists():
                raw_data = yaml.safe_load(ki_path.read_text(encoding="utf-8")) or {}
            else:
                ki_path.parent.mkdir(parents=True, exist_ok=True)
                raw_data = {}
            issues: List[Dict[str, Any]] = list(raw_data.get("issues") or [])
            existing = next(
                (
                    item for item in issues
                    if isinstance(item, dict)
                    and sig in (
                        [item.get("match", {}).get("signature")]
                        if isinstance((item.get("match") or {}).get("signature"), str)
                        else list((item.get("match") or {}).get("signature") or [])
                    )
                ),
                None,
            )
            if existing is not None:
                issue_id = str(existing.get("id") or "KI-0001")
                existing["title"] = title
                existing["jira"] = jira
                existing["classification"] = cls
                if waiver_block:
                    existing["waiver"] = waiver_block
                elif "waiver" in body and not waive:
                    existing.pop("waiver", None)
            else:
                nums = [
                    int(m.group(1))
                    for item in issues
                    if isinstance(item, dict)
                    for m in [re.match(r"^KI-(\d+)$", str(item.get("id") or ""))]
                    if m
                ]
                issue_id = f"KI-{(max(nums) if nums else 0) + 1:04d}"
                entry: Dict[str, Any] = {
                    "id": issue_id,
                    "title": title,
                    "jira": jira,
                    "classification": cls,
                    "match": {"signature": [sig]},
                }
                if waiver_block:
                    entry["waiver"] = waiver_block
                issues.append(entry)

            raw_data["issues"] = issues
            rendered = KNOWN_ISSUES_HEADER + yaml.safe_dump(raw_data, sort_keys=False, allow_unicode=True)
            tmp_path = ki_path.with_suffix(".yaml.tmp")
            tmp_path.write_text(rendered, encoding="utf-8")
            # Validate via KnownIssueDB before replacing
            loaded = KnownIssueDB.load(tmp_path)
            if not any(i.id == issue_id for i in loaded.issues):
                tmp_path.unlink(missing_ok=True)
                raise ValueError("generated known_issues entry failed validation")
            tmp_path.replace(ki_path)

        disc_store = self.store(defaults)
        verdict = disc_store.save_verdict(
            signature=sig,
            classification=cls,
            status="waived" if waive else "confirmed",
            jira_key=jira,
            owner_team=str(body.get("owner_team") or ""),
            note=f"Promoted to {ki_path.name} as {issue_id}: {title}",
            author=str(body.get("author") or ""),
        )
        disc_store.add_message(
            sig,
            str(body.get("run_key") or ""),
            "system",
            verdict["author"],
            f"Promoted signature `{sig}` to `{ki_path.name}` as **{issue_id}** ({cls}{', waived' if waive else ''}). "
            "Future runs will classify this root cause deterministically at Level 1.",
            {"kind": "known_issue", "issue_id": issue_id},
        )
        return {
            "issue_id": issue_id,
            "file": str(ki_path),
            "verdict": verdict,
            "messages": disc_store.list_messages(sig),
        }
