"""OEM source access for root-causing failures in OEM-modified components.

OEMs ship their own CarLauncher, CarSettings, CarSystemUI, HALs and services,
and those changes cause many xTS failures. The fix loop looks up the OEM code
for a failure through one of two providers, both configured under
``fix_loop.oem_sources``:

* ``local``: a checked-out folder on this host (searched with grep).
* ``mcp``: an MCP server the OEM already runs in front of its code
  (Gerrit/GitLab/code search). Started over stdio; the loop calls its search
  and read tools. Nothing is sent anywhere else.

A source is consulted first when one of its ``packages`` shows up in the
failure (stack trace, message, focused window or top activity).
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Translated resources (values-de, values-en-rXC, values-b+sr+Latn) repeat the
# default-locale file; the fix and the cause live in res/values/
LOCALE_RES = re.compile(r"/res/values-(?!(?:car|tv|xr|vr)(?:/|-))(?:[a-z]{2,3}(?:-r[A-Z]{2})?|b\+[^/]+)(?:-[^/]+)?/")
SOURCE_EXTS = ("*.java", "*.kt", "*.xml", "*.cpp", "*.h", "*.cc", "*.aidl", "*.bp", "*.mk", "*.rc")
MAX_SNIPPET_LINES = 14


@dataclass
class OemSourceConfig:
    name: str
    kind: str = "local"  # local | mcp
    path: str = ""
    packages: List[str] = field(default_factory=list)
    description: str = ""
    # Code fixes (local sources only): the loop may patch files here and
    # builds these targets after a patch, e.g. [CarSettings]
    editable: bool = False
    build_targets: List[str] = field(default_factory=list)
    # mcp only
    command: str = ""
    search_tool: str = "search_code"
    read_tool: str = ""
    timeout_secs: int = 60


@dataclass
class CodeHit:
    source: str
    file: str
    line: int
    snippet: str
    term: str

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "file": self.file, "line": self.line, "snippet": self.snippet, "term": self.term}


class LocalSource:
    def __init__(self, cfg: OemSourceConfig):
        self.cfg = cfg
        self.root = Path(cfg.path)

    def available(self) -> bool:
        return self.root.is_dir()

    def search(self, term: str, limit: int = 3) -> List[CodeHit]:
        if not self.available() or len(term) < 4:
            return []
        argv = ["grep", "-rnIF", "-m", "2"]
        for ext in SOURCE_EXTS:
            argv.append(f"--include={ext}")
        argv += ["--exclude-dir=.git", "--exclude-dir=build", "--", term, str(self.root)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("OEM source %s: grep failed: %s", self.cfg.name, exc)
            return []
        hits: List[CodeHit] = []
        for line in proc.stdout.splitlines():
            path, _, rest = line.partition(":")
            num, _, _ = rest.partition(":")
            if not num.isdigit() or LOCALE_RES.search(path):
                continue
            hits.append(CodeHit(self.cfg.name, path, int(num), self._window(Path(path), int(num)), term))
            if len(hits) >= limit:
                break
        return hits

    @staticmethod
    def _window(path: Path, line: int) -> str:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        start = max(0, line - 1 - MAX_SNIPPET_LINES // 2)
        chunk = lines[start : start + MAX_SNIPPET_LINES]
        return "\n".join(f"{start + i + 1:5d}  {t}" for i, t in enumerate(chunk))


class McpSource:
    """Minimal MCP stdio client (JSON-RPC 2.0, newline-delimited)."""

    PROTOCOL_VERSION = "2025-06-18"

    def __init__(self, cfg: OemSourceConfig):
        self.cfg = cfg
        self._proc: Optional[subprocess.Popen] = None
        self._id = 0
        self._lock = threading.Lock()

    def available(self) -> bool:
        return bool(self.cfg.command)

    def _start(self) -> None:
        if self._proc and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            shlex.split(self.cfg.command),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        self._request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "xts-agent-fixloop", "version": "1"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: Dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._id += 1
        req_id = self._id
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        assert self._proc and self._proc.stdout
        result: Dict[str, Any] = {}

        def read() -> None:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == req_id:
                    result.update(msg)
                    return

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(self.cfg.timeout_secs)
        if not result:
            raise TimeoutError(f"MCP {self.cfg.name}: no reply to {method} in {self.cfg.timeout_secs}s")
        if "error" in result:
            raise RuntimeError(f"MCP {self.cfg.name}: {method} failed: {result['error']}")
        return result.get("result") or {}

    def call_tool(self, tool: str, arguments: Dict[str, Any]) -> str:
        with self._lock:
            self._start()
            res = self._request("tools/call", {"name": tool, "arguments": arguments})
        texts = [c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text"]
        return "\n".join(texts)

    def search(self, term: str, limit: int = 3) -> List[CodeHit]:
        if len(term) < 4:
            return []
        try:
            text = self.call_tool(self.cfg.search_tool, {"query": term, "limit": limit})
        except Exception as exc:  # an OEM server problem must not stop the loop
            logger.warning("OEM MCP source %s: %s", self.cfg.name, exc)
            return []
        if not text.strip():
            return []
        return [CodeHit(self.cfg.name, f"mcp:{self.cfg.name}", 0, text[:2000], term)]

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


# Terms worth searching for: class names from the stack, resource ids and
# quoted strings from the message, component names
CLASS_RE = re.compile(r"\b(?:[a-z][a-z0-9_]*\.)+([A-Z][A-Za-z0-9_]{3,})(?:\$[A-Za-z0-9_]+)?(?:\.[a-z][A-Za-z0-9_]*)?\(")
RES_RE = re.compile(r"\bRES=([A-Za-z0-9_.:/]+)")
COMPONENT_RE = re.compile(r"\b([a-z][a-z0-9_]*(?:\.[a-z0-9_]+){2,})/(\.?[A-Za-z0-9_.$]+)")
QUOTED_RE = re.compile(r"['\"]([A-Za-z][A-Za-z0-9_ ]{4,40})['\"]")


# Names that occur everywhere and point at nothing in particular
GENERIC_TERMS = {
    "setUp", "tearDown", "setup", "teardown", "run", "main", "test", "onCreate", "onStart", "onResume",
    "onDestroy", "init", "close", "start", "stop", "get", "set", "apply", "invoke", "execute", "call",
}


def search_terms(text: str, limit: int = 6) -> List[str]:
    terms: List[str] = []

    def add(t: str) -> None:
        t = t.strip().split(":id/")[-1]
        if t and t not in terms and len(t) >= 4 and t not in GENERIC_TERMS:
            terms.append(t)

    for m in COMPONENT_RE.finditer(text):
        add(m.group(2).lstrip(".").split(".")[-1].split("$")[0])
    for m in RES_RE.finditer(text):
        add(m.group(1))
    for m in CLASS_RE.finditer(text):
        if not m.group(0).startswith(("java.", "jdk.", "dalvik.", "org.junit", "junit.", "androidx.test", "com.android.compatibility")):
            add(m.group(1))
    for m in QUOTED_RE.finditer(text):
        add(m.group(1))
    return terms[:limit]


class OemCodeLocator:
    def __init__(self, configs: List[OemSourceConfig]):
        self.sources = []
        for cfg in configs:
            src = McpSource(cfg) if cfg.kind == "mcp" else LocalSource(cfg)
            if not src.available():
                logger.warning("OEM source %s (%s) not available: %s", cfg.name, cfg.kind, cfg.path or cfg.command)
            self.sources.append(src)

    @classmethod
    def from_config(cls, items: List[Dict[str, Any]]) -> "OemCodeLocator":
        known = set(OemSourceConfig.__dataclass_fields__)
        return cls([OemSourceConfig(**{k: v for k, v in it.items() if k in known}) for it in items or []])

    def implicated(self, text: str) -> List[str]:
        """Names of OEM sources whose packages appear in the failure context."""
        return [s.cfg.name for s in self.sources if any(p and p in text for p in s.cfg.packages)]

    def locate(self, failure_text: str, context_text: str = "", max_hits: int = 8,
               extra_terms: Optional[List[str]] = None, failure_terms: bool = True) -> Dict[str, Any]:
        """Find OEM code for a failure.

        Terms come from the failure itself (plus ``extra_terms`` such as
        config or resource names the LLM expects to be involved), never from
        ambient device state: the launcher is always on screen, so the snapshot
        would implicate it for every failure. ``context_text`` only implicates
        a source when the failure is about window focus. Each term is searched
        across all sources, implicated ones first, so one noisy source cannot
        crowd out the overlay or app that holds the answer.
        """
        focus_failure = bool(re.search(r"focus|on top|window", failure_text, re.I))
        implicated = self.implicated(failure_text + ("\n" + context_text if focus_failure else ""))
        ordered = sorted(self.sources, key=lambda s: s.cfg.name not in implicated)
        terms: List[str] = []
        for t in list(extra_terms or []) + (search_terms(failure_text) if failure_terms else []):
            if t and t not in terms and t not in GENERIC_TERMS:
                terms.append(t)
        terms = terms[:8]
        hits: List[CodeHit] = []
        seen = set()
        per_term = 2  # breadth: every term gets a chance before one fills the list
        for term in terms:
            found = 0
            for src in ordered:
                if found >= per_term:
                    break
                if not src.available():
                    continue
                found_files = {x.file for x in hits}
                # Files that already matched other terms first: that is where the terms meet
                for h in sorted(src.search(term, limit=10), key=lambda x: x.file not in found_files):
                    # A module name matching its own build file says nothing
                    if h.file.endswith((".bp", ".mk")) and term == src.cfg.name:
                        continue
                    if (h.file, h.line) not in seen and found < per_term:
                        seen.add((h.file, h.line))
                        hits.append(h)
                        found += 1
                if len(hits) >= max_hits:
                    break
            if len(hits) >= max_hits:
                break
        # A UI text the test looks for, found as <string name="family_suffix">: search the
        # family (e.g. *_recently_accessed) to reach the sibling a screen uses instead
        quoted = set(re.findall(r"'([^'\n]{3,80})'", failure_text))
        family: List[str] = []
        for h in hits:
            for m in re.finditer(r'<string name="([a-z0-9]+)_(\w+)"[^>]*>([^<]*)</string>', h.snippet):
                if m.group(3) in quoted and m.group(2) not in terms + family:
                    family.append(m.group(2))
        for term in family[:2]:
            added = 0
            for src in ordered:
                if not src.available():
                    continue
                for h in src.search(term, limit=12):
                    if added < 3 and (h.file, h.line) not in seen and not _value_is(h.snippet, h.line, quoted):
                        seen.add((h.file, h.line))
                        hits.append(h)
                        added += 1
            max_hits += added
            terms.append(term)
        return {"implicated": implicated, "terms": terms, "hits": [h.to_dict() for h in hits[:max_hits]]}

    def close(self) -> None:
        for src in self.sources:
            if isinstance(src, McpSource):
                src.close()


def _value_is(snippet: str, line: int, texts: set) -> bool:
    """The hit line itself is a string whose value is one of ``texts`` (a sibling we already know)."""
    for ln in snippet.splitlines():
        num, _, rest = ln.strip().partition("  ")
        if num == str(line):
            return any(f">{t}<" in rest for t in texts)
    return False
