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
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Union

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

_LOGCAT_FAILED_RE = re.compile(r"TestRunner:\s*failed:\s*(\w+)\(([^)]+)\)")
_STACK_FRAME_RE = re.compile(
    r"at\s+([a-zA-Z0-9_.$]+)\.([a-zA-Z0-9_$<>]+)\(([a-zA-Z0-9_]+\.(?:java|kt)):(\d+)\)"
)
_EXTRA_MEMBER_RE = re.compile(
    r"extra_(?:field|method):\s+\S+\s+([a-zA-Z0-9_.]+)(?:\$([A-Za-z0-9_]+))?\.([A-Za-z0-9_<>]+)(?:\s|\(|$)"
)
_BOILERPLATE_PREFIXES = (
    "org.junit.",
    "junit.framework.",
    "java.lang.",
    "java.util.concurrent.",
    "jdk.internal.",
    "kotlin.test.",
    "com.google.common.truth.",
    "android.os.Parcel.createException",
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
- Pre-classification signal (rule/correlation): {rule_hint}
- Run-wide systemic context (other dominant failure patterns in this same run):
{run_context}

Sample stack trace:
```
{stack}
```

Device logcat excerpt around failure (may be empty):
```
{logcat}
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
Classification rules:
- Use ENVIRONMENT_ISSUE for lab/device setup problems (missing Wi-Fi/network, missing initial user, missing default dialer role causing getDefaultDialer IndexOutOfBoundsException in setUp, another system/debug app stealing window focus), for userdebug/aconfig @FlaggedApi build-skew issues (e.g. HiddenApiTest failing on @FlaggedApi fields enabled on a userdebug/trunk_staging build vs prebuilt CTS, or ListeningPortsTest failing on UID=2000 com.android.shell debug ports on userdebug), AND for downstream cascading failures caused by run-wide environment issues (e.g. IME bindInput/onStartInput timeouts, @After tearDown() NullPointerExceptions on mContext when @Before setUp() aborted waiting for focus, or UiObject2 NullPointerExceptions when the test UI was occluded by another window).
- Use INFRASTRUCTURE_FAILURE for adb/host/TradeFed problems.
- Use FLAKY_TEST for UI Automator waitForObject / timing / contention timeouts without a crash.
- Use PRODUCT_BUG only when platform/OEM code behaviour itself is wrong on a production user build (e.g. framework crash, wrong API return value, system feature or HAL compliance failure)."""


def load_json_object(text: str) -> Any:
    """The JSON object in an LLM reply, tolerating fences and \\' escapes; raises ValueError."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    body = text[start : end + 1]
    try:
        return json.loads(body)
    except ValueError:
        # \' is not a JSON escape, but models write it inside strings
        return json.loads(body.replace("\\'", "'"))


def provider_failed(raw: Optional[str]) -> bool:
    """True when the provider reported an error instead of an answer.

    Providers never raise: OpenAICompatibleProvider / GeminiProvider /
    FallbackProvider return a "Failed to generate ..." or "Error: ..." string.
    """
    return not raw or raw.startswith(("Failed to generate", "Error:"))


_CLASS_SYNONYMS = (
    # (substring of the normalised model answer, canonical class); first match wins
    ("FLAKY", "FLAKY_TEST"),
    ("INFRA", "INFRASTRUCTURE_FAILURE"),
    ("ENV", "ENVIRONMENT_ISSUE"),
    ("CONFIG", "ENVIRONMENT_ISSUE"),
    ("SETUP", "ENVIRONMENT_ISSUE"),
    ("TEST", "TEST_BUG"),          # TEST_ISSUE, TEST_CODE_BUG, CTS_TEST_BUG
    ("PRODUCT", "PRODUCT_BUG"),
    ("PLATFORM", "PRODUCT_BUG"),
    ("DEVICE", "PRODUCT_BUG"),
    ("BUG", "PRODUCT_BUG"),
)


def normalize_classification(value: Any) -> str:
    """Map the model's class to CLASSIFICATIONS; UNKNOWN when nothing fits."""
    cls = re.sub(r"[\s\-/]+", "_", str(value or "").strip().upper())
    if cls in CLASSIFICATIONS:
        return cls
    for needle, canonical in _CLASS_SYNONYMS:
        if needle in cls:
            return canonical
    return "UNKNOWN"


def parse_ai_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract and validate the model's JSON object; None if unusable."""
    try:
        data = load_json_object(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("root_cause"):
        return None
    raw_cls = str(data.get("classification", "")).strip()
    cls = normalize_classification(raw_cls)
    if cls != raw_cls.upper():
        logger.warning("AI classification %r mapped to %s", raw_cls[:40], cls)
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "root_cause": str(data["root_cause"])[:1000],
        "classification": cls,
        "raw_classification": raw_cls[:40],
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
    def __init__(
        self,
        provider: Any,
        indexer: Any = None,
        cache_db: Union[str, Database, None] = None,
        max_groups: int = 20,
        results_dirs: Optional[List[str]] = None,
    ):
        self.provider = provider
        self.indexer = indexer
        self.cache = _Cache(cache_db) if cache_db else None
        self.max_groups = max_groups
        self.results_dirs = list(results_dirs or [])
        self._logcat_index: Optional[Dict[str, str]] = None
        self._file_map: Optional[Dict[str, List[Path]]] = None

    MAX_CHUNKS = 4
    # Circuit breaker: a dead or hung LLM endpoint must not cost one timeout
    # per group (200 groups x 180 s). After this many consecutive provider
    # failures the remaining groups are skipped and reported in the stats.
    MAX_CONSECUTIVE_ERRORS = 3
    EVAL_RESERVE = 2  # known, human-classified groups analysed per run even when the cap is full
    PROMPT_VERSION = "v5"

    def _ensure_file_map(self) -> Dict[str, List[Path]]:
        """Lazily build a basename -> [Path] lookup map over the configured AOSP/OEM tree."""
        if self._file_map is not None:
            return self._file_map
        self._file_map = {}
        src_paths = getattr(self.indexer, "source_paths", None)
        if not isinstance(src_paths, (list, tuple)) or not src_paths:
            return self._file_map
        import os

        roots: List[Path] = []
        seen_roots = set()
        for sp in src_paths:
            p = Path(str(sp))
            if p.is_dir() and str(p) not in seen_roots:
                seen_roots.add(str(p))
                roots.append(p)
            # Also include broader AOSP subtrees if sp is inside an AOSP checkout
            for parent in p.parents:
                if (parent / "cts").is_dir() and (parent / "frameworks" / "base").is_dir():
                    for sub in (
                        "cts",
                        "frameworks/base/core/java",
                        "frameworks/base/services/core/java",
                        "packages/services/Car",
                    ):
                        cand = parent / sub
                        if cand.is_dir() and str(cand) not in seen_roots:
                            seen_roots.add(str(cand))
                            roots.append(cand)
                    break
        skip = {".git", ".repo", "out", "prebuilts", "node_modules", "__pycache__"}
        for root in roots:
            for dirpath, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in skip]
                for fname in files:
                    if fname.endswith((".java", ".kt", ".cpp", ".cc", ".c")):
                        self._file_map.setdefault(fname, []).append(Path(dirpath) / fname)
        return self._file_map

    def _resolve_exact_stack_chunks(self, tg: TriagedGroup) -> List[Dict[str, Any]]:
        """Extract exact source windows around stack trace (file:line) frames, test classes, base classes, and JNI."""
        fmap = self._ensure_file_map()
        if not fmap:
            return []
        g = tg.group
        stack = g.sample_stack or ""
        found: List[Dict[str, Any]] = []
        seen_files = set()

        def pick_best_path(fname: str, fqcn: str) -> Optional[Path]:
            cands = fmap.get(fname)
            if not cands:
                return None
            if len(cands) == 1:
                return cands[0]
            pkg_Rel = fqcn.split("$")[0].replace(".", "/")
            best, best_score = cands[0], -1
            for c in cands:
                s = str(c)
                score = 100 if pkg_Rel in s else sum(1 for seg in pkg_Rel.split("/") if f"/{seg}/" in s)
                if score > best_score:
                    best, best_score = c, score
            return best

        def read_window(path: Path, center_line: int, before: int = 30, after: int = 30) -> Optional[Dict[str, Any]]:
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                return None
            if not lines:
                return None
            start = max(1, min(center_line, len(lines)) - before)
            end = min(len(lines), max(center_line, 1) + after)
            snippet = "\n".join(lines[start - 1 : end])
            return {"file": str(path), "start_line": start, "text": snippet}

        # 1. Referenced extra_field / extra_method declarations (e.g. HiddenApiTest @FlaggedApi fields)
        for m in _EXTRA_MEMBER_RE.finditer(stack):
            outer_cls, inner_cls, member = m.group(1), m.group(2), m.group(3)
            fname = outer_cls.rsplit(".", 1)[-1] + ".java"
            path = pick_best_path(fname, outer_cls)
            if path and str(path) not in seen_files:
                try:
                    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
                    target_sym = inner_cls or member
                    line_no = next(
                        (i + 1 for i, ln in enumerate(lines) if target_sym in ln and not ln.strip().startswith("*")),
                        1,
                    )
                    chunk = read_window(path, line_no, before=15, after=25)
                    if chunk:
                        seen_files.add(str(path))
                        found.append(chunk)
                except OSError:
                    pass
            if len(found) >= 2:
                break

        # 2. Exact stack trace frames (innermost non-boilerplate first)
        frames = [
            m.groups()
            for m in _STACK_FRAME_RE.finditer(stack)
            if not any(m.group(1).startswith(bp) for bp in _BOILERPLATE_PREFIXES)
        ]
        resolved_paths: List[Path] = []
        for fqcn, _method, fname, line_str in frames:
            if len(found) >= self.MAX_CHUNKS:
                break
            path = pick_best_path(fname, fqcn)
            if not path:
                continue
            key = f"{path}:{int(line_str) // 30}"
            if key in seen_files:
                continue
            chunk = read_window(path, int(line_str), before=30, after=30)
            if chunk:
                seen_files.add(key)
                found.append(chunk)
                resolved_paths.append(path)

        # 3. Fallback when stack has no Java frames (e.g. native crash / tombstone)
        if not found and g.tests:
            tc0 = g.tests[0]
            cls_simple = tc0.class_name.rsplit(".", 1)[-1].split("$")[0]
            for ext in (".java", ".kt"):
                path = pick_best_path(cls_simple + ext, tc0.class_name)
                if path:
                    try:
                        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
                        line_no = next(
                            (i + 1 for i, ln in enumerate(lines) if tc0.test_name in ln),
                            len(lines) // 2,
                        )
                        chunk = read_window(path, line_no, before=30, after=30)
                        if chunk:
                            seen_files.add(str(path))
                            found.append(chunk)
                            resolved_paths.append(path)
                            break
                    except OSError:
                        pass

        # 4. Base-class helper or sibling JNI C++ resolution when slots remain
        if len(found) < self.MAX_CHUNKS and resolved_paths:
            primary = resolved_paths[0]
            try:
                full_src = primary.read_text(encoding="utf-8", errors="ignore")
                # Check for base class in same directory: "extends BaseClass" or ": BaseClass("
                base_m = re.search(r"(?:extends|:)\s+([A-Z][A-Za-z0-9_]*)", full_src)
                if base_m:
                    base_name = base_m.group(1)
                    for ext in (".java", ".kt"):
                        base_cand = primary.parent / (base_name + ext)
                        if not base_cand.exists():
                            base_cand = pick_best_path(base_name + ext, "")  # type: ignore[assignment]
                        if base_cand and base_cand.exists() and str(base_cand) not in seen_files:
                            base_lines = base_cand.read_text(encoding="utf-8", errors="ignore").splitlines()
                            # Find first helper method called in the primary snippet
                            called = set(re.findall(r"\b([a-z][A-Za-z0-9_]{3,})\s*\(", found[0]["text"]))
                            line_no = next(
                                (
                                    i + 1
                                    for i, ln in enumerate(base_lines)
                                    if any(f" {fn}(" in ln or f"fun {fn}(" in ln for fn in called)
                                ),
                                1,
                            )
                            if line_no > 1:
                                bchunk = read_window(base_cand, line_no, before=15, after=35)
                                if bchunk:
                                    seen_files.add(str(base_cand))
                                    found.append(bchunk)
                                    break
                # Check for native method JNI implementation in sibling jni/ directory
                if len(found) < self.MAX_CHUNKS and "native " in full_src:
                    jni_Sym = primary.stem
                    for mod_root in (primary.parents[3], primary.parents[4], primary.parents[5]):
                        jni_dir = mod_root / "jni"
                        if jni_dir.is_dir():
                            for cpp in sorted(jni_dir.rglob("*")):
                                if cpp.suffix in (".cpp", ".cc", ".c") and jni_Sym.lower() in cpp.stem.lower():
                                    cpp_lines = cpp.read_text(encoding="utf-8", errors="ignore").splitlines()
                                    cchunk = read_window(cpp, min(45, len(cpp_lines)), before=35, after=40)
                                    if cchunk:
                                        seen_files.add(str(cpp))
                                        found.append(cchunk)
                                    break
                            break
            except (OSError, IndexError):
                pass

        return found

    @staticmethod
    def _is_import_header_chunk(text: str) -> bool:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return True
        import_lines = sum(1 for ln in lines if ln.startswith(("import ", "package ", "*", "/*", "//")))
        return import_lines >= 5 and (import_lines / len(lines)) >= 0.55

    @staticmethod
    def _extract_structural_signals(
        tg: TriagedGroup,
        chunks: List[Dict[str, Any]],
        run_context: str,
        logcat: str,
    ) -> List[str]:
        """Derive generic, test-agnostic structural signals from stack, retrieved code, and run context."""
        signals: List[str] = []
        g = tg.group
        stack = g.sample_stack or ""
        msg = g.message or ""
        code_text = "\n".join(c.get("text", "") for c in chunks)
        combined = f"{msg}\n{stack}"
        has_focus_outage = any(
            k in run_context.lower()
            for k in ("focused instead", "window focus", "ime", "environment_issue")
        )
        has_net_outage = any(
            k in run_context.lower()
            for k in ("network", "wifi", "wi-fi", "environment_issue")
        )

        # 1. Generic Android Multi-User UID arithmetic (uid = userId * 100000 + appId)
        uid_m = re.search(
            r"expected\s*:?\s*<?(\d{5,8})>?\s*but was\s*:?\s*<?(\d{5,8})>?",
            combined,
            re.IGNORECASE,
        )
        if uid_m:
            a, b = int(uid_m.group(1)), int(uid_m.group(2))
            diff = abs(a - b)
            app_id = min(a, b) % 100000
            if diff >= 100000 and diff % 100000 == 0 and (a % 100000) == (b % 100000) and 1000 <= app_id <= 99999:
                u_exp, u_act = a // 100000, b // 100000
                signals.append(
                    f"MULTI_USER_UID_SKEW: Expected {a} (User {u_exp}, appId {app_id}) vs actual {b} "
                    f"(User {u_act}, appId {app_id}) differs by {diff // 100000} * 100000 "
                    "(Android HSUM multi-user UID offset) -> ENVIRONMENT_ISSUE."
                )

        # 2. Explicit @FlakyTest annotation in retrieved test source code
        if "@FlakyTest" in code_text:
            signals.append(
                "SOURCE_ANNOTATED_FLAKY: The retrieved test method/class is explicitly annotated with "
                "@FlakyTest in AOSP source -> FLAKY_TEST."
            )

        # 3. Exception masking in finally{} block after UI/dialog automation in try{} block
        if "finally" in code_text and any(
            tok in code_text for tok in ("doEscalation(", "UiDevice", "UiObject", "clickInstallerUIButton(")
        ):
            signals.append(
                "FINALLY_MASKED_UI_ESCALATION: The failing assertion is inside a finally{} block after a "
                "UI confirmation/escalation helper (e.g. doEscalation/UiObject) in the try{} block failed "
                "due to occluded/unfocused UI -> ENVIRONMENT_ISSUE."
            )

        # 4. Interactive UI / Key / Back / Installer dialog dependency during run-wide focus hijack
        if has_focus_outage and any(
            tok in code_text
            for tok in (
                "sendKeyDownUp(",
                "pressBack(",
                "goBack(",
                "clickInstallerUIButton(",
                "UiScrollable(",
                "waitFindObject",
            )
        ):
            signals.append(
                "UI_INPUT_FOCUS_DEPENDENCY: The retrieved test source injects key/back events or clicks "
                "system UI dialogs (sendKeyDownUp/goBack/pressBack/clickInstallerUIButton) which require "
                "window focus, while this run has an active foreground window-focus hijack -> ENVIRONMENT_ISSUE."
            )

        # 5. Network / JobScheduler constraint dependency during run-wide network outage
        if has_net_outage and any(
            tok in code_text
            for tok in ("mDownloadManager.enqueue(", "dumpsys jobscheduler", "ConnectivityManager", "WifiManager")
        ):
            signals.append(
                "NETWORK_JOB_DEPENDENCY: The retrieved test source enqueues network downloads or checks "
                "JobScheduler execution state while this run has no active Wi-Fi/internet network -> ENVIRONMENT_ISSUE."
            )

        # 6. Test-side static state leakage in CTS test/activity helper classes
        if re.search(
            r"Already have pending intent|Mock .* exists|Failed to call setUp\(\), or previous test failed to call tearDown\(\)",
            combined,
        ) or ("sPendingIntent != null" in code_text and "IllegalArgumentException" in combined):
            signals.append(
                "TEST_STATE_LEAK: Exception is thrown by a CTS test helper/activity guard checking uncleaned "
                "static state (sPendingIntent / Mock service) leaked from a prior test -> TEST_BUG."
            )

        return signals

    def _ensure_logcat_index(self) -> Dict[str, str]:
        """Lazily index per-test failure windows from TradeFed device_logcat_test_*.txt files."""
        if self._logcat_index is not None:
            return self._logcat_index
        self._logcat_index = {}
        log_files: List[Path] = []
        for rdir_str in self.results_dirs:
            rdir = Path(rdir_str)
            for candidate in (rdir, rdir.parent.parent / "logs" / rdir.name):
                if candidate.is_dir():
                    log_files.extend(sorted(candidate.rglob("device_logcat_test_*.txt")))
        for fpath in log_files:
            before: Deque[str] = deque(maxlen=15)
            try:
                with fpath.open("r", encoding="utf-8", errors="replace") as fp:
                    for line in fp:
                        if "TestRunner: failed:" in line:
                            m = _LOGCAT_FAILED_RE.search(line)
                            if m:
                                method, cls = m.group(1), m.group(2)
                                full_key = f"{cls}#{method}"
                                if full_key not in self._logcat_index:
                                    after = [line]
                                    for _ in range(14):
                                        nxt = next(fp, "")
                                        if not nxt or "TestRunner: finished:" in nxt:
                                            break
                                        after.append(nxt)
                                    excerpt = "".join(list(before) + after)
                                    self._logcat_index[full_key] = excerpt
                                    self._logcat_index.setdefault(method, excerpt)
                        before.append(line)
            except OSError:
                continue
        return self._logcat_index

    def retrieve_logcat(self, tg: TriagedGroup) -> str:
        """Return device logcat excerpt for the first matching test in ``tg``."""
        if not self.results_dirs:
            return ""
        idx = self._ensure_logcat_index()
        if not idx:
            return ""
        for tc in tg.group.tests[:10]:
            full_key = f"{tc.class_name}#{tc.test_name}"
            if full_key in idx:
                return idx[full_key][:2000]
            if tc.test_name in idx:
                return idx[tc.test_name][:2000]
        return ""

    @staticmethod
    def build_run_context(report: TriageReport, current_sig: str = "") -> str:
        """Summarise dominant failure groups and known issues in this run for cross-group correlation."""
        lines: List[str] = []
        for tg in report.groups[:6]:
            if tg.group.signature == current_sig:
                continue
            # Title only: the human classification of known issues is the eval
            # label, so it must not leak into prompts
            known = f" [Known: {tg.known.issue.title}]" if tg.known else ""
            lines.append(
                f"  * {tg.group.count} tests ({tg.classification}): {tg.group.title[:110]}{known}"
            )
        return "\n".join(lines[:4]) if lines else "  (none)"

    def retrieve(self, tg: TriagedGroup) -> List[Dict[str, Any]]:
        """Source chunks from the indexed AOSP/OEM tree that match this group's signature.

        Exact stack-frame (file:line) and extra_field symbol windows come first,
        followed by method-filtered and semantic vector matches (excluding pure
        import-header blocks).
        """
        if self.indexer is None:
            return []
        g = tg.group
        query = f"{g.exception} {g.message}\n" + "\n".join(g.frames)
        if not hasattr(self.indexer, "search_chunks"):
            text = self.indexer.search(query, top_k=3)  # older indexers: text only
            return [{"file": "", "start_line": 0, "text": text}] if text else []
        chunks: List[Dict[str, Any]] = []
        seen = set()
        has_file_map = bool(self._ensure_file_map())
        frame_tokens = {
            tok
            for frame in g.frames
            for tok in frame.replace("$", ".").split(".")[-2:]
            if len(tok) > 3 and tok not in ("run", "call", "invoke", "<init>", "fail", "assertTrue", "assertEquals")
        }

        def add(found: List[Dict[str, Any]], require_relevance: bool = False) -> None:
            for c in found:
                text = c.get("text") or ""
                fpath = str(c.get("file") or "")
                if has_file_map and self._is_import_header_chunk(text):
                    continue
                if has_file_map and require_relevance and frame_tokens:
                    if not any(tok in fpath or tok in text for tok in frame_tokens):
                        continue
                key = (fpath, c.get("start_line"))
                if key not in seen and len(chunks) < self.MAX_CHUNKS:
                    seen.add(key)
                    chunks.append(c)

        # 1. Exact stack-frame file:line & symbol declarations from disk
        for exact in self._resolve_exact_stack_chunks(tg):
            key = (str(exact.get("file") or ""), exact.get("start_line"))
            if key not in seen and len(chunks) < self.MAX_CHUNKS:
                seen.add(key)
                chunks.append(exact)

        if len(chunks) >= self.MAX_CHUNKS:
            return chunks

        # 2. Innermost frames first: "pkg.WindowUtil.waitForFocus" -> "waitForFocus("
        for frame in reversed(g.frames[-2:]):
            method = frame.rsplit(".", 1)[-1]
            if method and method not in ("run", "call", "invoke", "<init>"):
                add(self.indexer.search_chunks(query, top_k=1, contains=f"{method}("))
        add(self.indexer.search_chunks(query, top_k=3), require_relevance=True)
        return chunks

    def prompt(
        self,
        tg: TriagedGroup,
        fingerprint: str,
        chunks: Optional[List[Dict[str, Any]]] = None,
        run_context: str = "  (none)",
        logcat: str = "",
    ) -> str:
        g = tg.group
        if chunks is None:
            chunks = self.retrieve(tg)
        if not logcat:
            logcat = self.retrieve_logcat(tg)
        code = "\n".join(
            f"--- File: {c.get('file') or '?'} (line {c.get('start_line', 0)}) ---\n{c['text']}\n" for c in chunks
        )
        signals = self._extract_structural_signals(tg, chunks, run_context, logcat)
        if tg.known:
            rule_hint = "none (blind evaluation)"
        elif signals:
            rule_hint = " | ".join(signals)
        elif tg.classification != "PRODUCT_BUG":
            rule_hint = f"{tg.classification} (matched deterministic rule or cross-group contamination detector)"
        else:
            rule_hint = "none (default PRODUCT_BUG pending verification)"
        return PROMPT.format(
            count=g.count,
            label=tg.label,
            exception=g.exception or "-",
            message=g.message or "-",
            frames=", ".join(g.frames) or "-",
            modules=", ".join(g.modules[:10]),
            tests="\n".join(f"  {t.test_id}" for t in g.tests[:5]),
            fingerprint=fingerprint or "unknown",
            rule_hint=rule_hint,
            run_context=run_context,
            stack=g.sample_stack[:4000],
            logcat=logcat[:2000],
            code=code[:6000],
            classes="|".join(CLASSIFICATIONS),
        )

    def analyze(self, report: TriageReport) -> Dict[str, int]:
        stats = {"analyzed": 0, "cached": 0, "unparseable": 0, "provider_errors": 0, "skipped": 0}
        model = getattr(self.provider, "model_id", "") or type(self.provider).__name__
        consecutive_errors = 0
        # Actionable groups first. Known issues with a human classification are
        # the eval set for AI agreement, so at least EVAL_RESERVE of them (largest
        # first) are always analysed too; spare slots under the cap add more.
        targets = [tg for tg in report.groups if tg.actionable][: self.max_groups]
        eval_slots = max(self.max_groups - len(targets), self.EVAL_RESERVE)
        targets += [
            tg for tg in report.groups
            if not tg.actionable and tg.known and tg.known.issue.classification
        ][:eval_slots]
        try:
            for i, tg in enumerate(targets):
                sig = tg.group.signature
                cached = self.cache.get(sig, model) if self.cache else None
                if cached:
                    has_map = bool(self._ensure_file_map())
                    exact_chunks = self._resolve_exact_stack_chunks(tg) if has_map else []
                    new_refs = [f"{c['file']}:{c['start_line']}" for c in exact_chunks if c.get("file")]
                    run_ctx = self.build_run_context(report, current_sig=sig)
                    signals = self._extract_structural_signals(tg, exact_chunks, run_ctx, "") if has_map else []
                    needs_reanalysis = cached.get("classification") == "UNKNOWN" or (has_map and (
                        bool(signals and cached.get("signals") != signals)
                        or (cached.get("classification") == "PRODUCT_BUG" and cached.get("prompt_v") != self.PROMPT_VERSION)
                    ))
                    if not needs_reanalysis:
                        if has_map and new_refs != cached.get("code_refs", []):
                            cached["code_refs"] = new_refs
                            if self.cache:
                                self.cache.put(sig, model, cached)
                        tg.ai = {**cached, "cached": True}
                        stats["cached"] += 1
                        continue
                chunks = self.retrieve(tg)
                run_ctx = self.build_run_context(report, current_sig=sig)
                logcat = self.retrieve_logcat(tg)
                signals = self._extract_structural_signals(tg, chunks, run_ctx, logcat)
                prompt = self.prompt(tg, report.fingerprint, chunks, run_context=run_ctx, logcat=logcat)
                raw = self.provider.generate(prompt, json_mode=True)
                if provider_failed(raw):
                    stats["provider_errors"] += 1
                    consecutive_errors += 1
                    if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                        stats["skipped"] = len(targets) - i - 1
                        logger.error(
                            "LLM %s failed %d times in a row (%s); skipping the remaining %d group(s)",
                            model, consecutive_errors, (raw or "no response")[:120], stats["skipped"],
                        )
                        break
                    continue
                consecutive_errors = 0
                # The model that actually answered (FallbackProvider may have switched)
                model = getattr(self.provider, "model_id", "") or model
                result = parse_ai_json(raw or "")
                if result is not None and result["classification"] == "UNKNOWN":
                    # Sampling noise: the model occasionally leaves the class blank
                    # while the root cause is fine. One more draw is cheaper than
                    # an UNKNOWN row on the page; the first answer stays if it repeats.
                    logger.info("AI left group %s unclassified; asking once more", sig)
                    again = parse_ai_json(self.provider.generate(prompt, json_mode=True) or "")
                    if again is not None and again["classification"] != "UNKNOWN":
                        result = again
                if result is None:
                    logger.warning("AI output for group %s was not valid JSON; ignored", sig)
                    stats["unparseable"] += 1
                    continue
                result["model"] = model
                result["prompt_v"] = self.PROMPT_VERSION
                if signals:
                    result["signals"] = signals
                # Which indexed source files the answer was grounded on (file:line)
                result["code_refs"] = [
                    f"{c['file']}:{c['start_line']}" for c in chunks if c.get("file")
                ]
                # Advisory only: the rule classification stays as-is so the two
                # can be compared honestly (see TriageReport.ai_agreement)
                tg.ai = result
                if self.cache:
                    self.cache.put(sig, model, result)
                stats["analyzed"] += 1
        finally:
            unload_provider = getattr(self.provider, "unload", None)
            if callable(unload_provider):
                unload_provider()
            unload_indexer = getattr(self.indexer, "unload", None)
            if callable(unload_indexer):
                unload_indexer()
        logger.info("AI RCA over %s group(s): %s", len(targets), stats)
        return stats
