"""Fix product bugs in OEM source: patch -> build -> flash -> re-run.

When the failure is a product bug (or a device-side workaround has proved
one), the local LLM is shown the failure, the root-cause analysis so far and
the OEM source it points at, and asked for the smallest source change. The
change is applied to an editable OEM source tree, built with the lab's build
command, flashed with its deploy command, and the CTS target is re-run. A patch
that does not build, or builds but does not make the test pass, is reverted.

Guard rails:

* Only files under ``oem_sources`` marked ``editable: true`` can change.
  Tests (CTS, unit tests) never can: a patch must fix the device, not the check.
* Each edit replaces one exact block that occurs once in the file, so the LLM
  cannot touch code it was not shown.
* Original file contents are kept in memory and restored on revert, so OEM
  work-in-progress in the same tree is never lost to a ``git checkout``.
* The loop never commits or pushes. A verified patch is written to
  ``<out_dir>/patches/`` for the owning team's review.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from xts_agent.triage.ai_rca import load_json_object

from .oem_sources import OemSourceConfig

logger = logging.getLogger(__name__)

PATCH_PROMPT = """You are an Android Automotive platform engineer at a car OEM. A CTS test fails on the
OEM build. Find the defect in the OEM source below and write the smallest source change that makes
the device behave as CTS / the CDD requires.

Failing tests:
{failures}

What is known so far:
{analysis}

OEM source (editable; each line is prefixed with its line number, which is NOT part of the file):
{code}
{resources}{previous}
Rules:
- Lines marked FACT, PROVEN, RESOURCE CHECK or "UI text check" are measured; trust them over guesses.
- A framework resource (config_xxx in frameworks/base/core/res) is changed for a product only in the
  product's framework-res overlay (.../overlay/frameworks/base/core/res/res/values/config.xml), never
  in an app's own res/values.
- Change only the files shown above. Never change CTS, tests or test configuration, never weaken a
  check, never disable the feature under test.
- Fix the cause, not the symptom: if the OEM changed a CDD-required string, behaviour or flag,
  restore what the requirement needs. When the test looks for a UI text, compare it with the
  resolved string values above: the text the test expects is the requirement.
- Each edit replaces one exact block of existing lines: "find" is copied verbatim from the file
  (without the line-number prefix) and must occur exactly once; "replace" is the new text. To add
  lines, include a neighbouring existing line in "find" and keep it in "replace".
Return only JSON:
{{"root_cause": "<2-3 sentences: what in the OEM code is wrong, cite file:line>",
 "edits": [{{"file": "<path exactly as shown>", "find": "<exact existing lines>", "replace": "<new lines>"}}],
 "explanation": "<why this change makes the test pass without weakening it>"}}
"""

REVIEW_PROMPT = """You are reviewing a proposed OEM source patch before it is built and flashed.

Failing tests:
{failures}

What is known. Lines marked FACT, PROVEN or "UI text check" are measured and authoritative; the
triage root cause is only a hypothesis from an earlier step and may be wrong:
{analysis}
{resources}
Proposed patch:
{diff}

Check each hunk:
1. Does it move the device toward the behaviour the test and the facts above require, or away from it?
2. Does it spread the defect, weaken a check, or change code unrelated to the failure?
A CTS/CDD requirement outranks an OEM feature: undoing an OEM change that breaks the requirement is
correct. Put the feature impact in "notes" for the owning team instead of rejecting the patch for it.
Return only JSON: {{"ok": true|false, "problems": ["<hunk/file>: <what is wrong>", ...],
 "notes": ["<impact the owning team should know about>", ...]}}
"""

# Paths a patch may never touch, whatever the source config says
PROTECTED = re.compile(r"(^|/)(cts|tests?|androidTest|testcases)(/|$)|Test\.(java|kt)$", re.IGNORECASE)
MAX_EDITS = 4
MAX_CHANGED_LINES = 60
WINDOW = 40  # lines of context on each side of a hit
FULL_FILE_LINES = 160  # smaller files are shown whole


@dataclass
class CodeFixConfig:
    enabled: bool = False
    # Shell commands; {modules} = build targets of the touched sources,
    # {serial}/{cvd_instance} = the DUT being flashed
    build_command: str = ""
    deploy_command: str = ""
    workdir: str = ""
    build_timeout_secs: int = 5400
    deploy_timeout_secs: int = 1800
    max_attempts: int = 2  # patches tried per target (a build-error repair counts as one)
    restore_on_failure: bool = True  # rebuild + reflash the original source if no patch worked

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "CodeFixConfig":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class Edit:
    file: str
    find: str
    replace: str


@dataclass
class Patch:
    root_cause: str = ""
    explanation: str = ""
    edits: List[Edit] = field(default_factory=list)
    model: str = ""
    diff: str = ""
    files: List[str] = field(default_factory=list)
    modules: List[str] = field(default_factory=list)
    error: str = ""
    review_notes: List[str] = field(default_factory=list)  # side effects the owning team should weigh

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root_cause": self.root_cause, "explanation": self.explanation, "model": self.model,
            "diff": self.diff, "files": self.files, "modules": self.modules, "error": self.error,
            "review_notes": self.review_notes,
        }


class PatchError(Exception):
    pass


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _replace_by_lines(text: str, find: str, replace: str) -> Optional[str]:
    """Replace a block that matches ``find`` line by line, ignoring indentation only.

    LLMs often get the leading whitespace of copied lines wrong. The block
    must still match exactly once; ``replace`` is re-indented by the same
    offset so the file keeps its own indentation.
    """
    want = [ln.strip() for ln in find.splitlines()]
    lines = text.splitlines(keepends=True)
    starts = [i for i in range(len(lines) - len(want) + 1)
              if all(lines[i + k].strip() == want[k] for k in range(len(want)))]
    if len(starts) != 1 or not any(want):
        return _replace_reflowed(text, find, replace)
    i = starts[0]
    first = next(k for k, w in enumerate(want) if w)
    delta = _indent(lines[i + first]) - _indent(find.splitlines()[first])
    out = []
    for ln in replace.splitlines():
        out.append(" " * max(0, _indent(ln) + delta) + ln.lstrip(" ") if ln.strip() else "")
    end_nl = "\n" if lines[i + len(want) - 1].endswith("\n") else ""
    return "".join(lines[:i]) + "\n".join(out) + end_nl + "".join(lines[i + len(want):])


def _replace_reflowed(text: str, find: str, replace: str) -> Optional[str]:
    """Last resort: the block was re-wrapped (lines joined or split). Match on
    whole lines whose whitespace-collapsed text equals the collapsed ``find``."""
    target = " ".join(find.split())
    if len(target) < 20:
        return None
    lines = text.splitlines(keepends=True)
    hits = []
    for i in range(len(lines)):
        acc = ""
        for j in range(i, min(len(lines), i + 40)):
            acc = " ".join((acc + " " + lines[j]).split())
            if acc == target:
                hits.append((i, j))
                break
            if not target.startswith(acc):
                break
    if len(hits) != 1:
        return None
    i, j = hits[0]
    pad = " " * _indent(lines[i])
    body = "\n".join(pad + ln.strip() if ln.strip() else "" for ln in replace.splitlines()) if "\n" not in replace.strip() \
        else replace
    end_nl = "\n" if lines[j].endswith("\n") else ""
    return "".join(lines[:i]) + body + end_nl + "".join(lines[j + 1:])


def _numbered(lines: List[str], start: int) -> str:
    return "\n".join(f"{start + i + 1:5d}  {t}" for i, t in enumerate(lines))


class CodeFixer:
    def __init__(
        self,
        cfg: CodeFixConfig,
        sources: List[OemSourceConfig],
        provider: Any,
        llm_lock: Any,
        out_dir: Path,
        runner: Optional[Callable[[str, str, int, Path], Tuple[int, str]]] = None,
    ):
        self.cfg = cfg
        self.sources = [s for s in sources if s.editable and s.kind == "local" and s.path]
        # Read-only sources (AOSP defaults) are evidence, never patched
        self.reference = [s for s in sources if not s.editable and s.kind == "local" and s.path]
        # Set by the loop: run a read-only shell command on the DUT
        self.device_shell: Optional[Callable[[str], str]] = None
        self.provider = provider
        self.llm_lock = llm_lock
        self.out_dir = Path(out_dir)
        self._runner = runner or self._shell
        self._originals: Dict[Path, str] = {}
        self._last_checks = ""  # deterministic findings from the last propose(), shown to the reviewer

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and self.sources and self.cfg.build_command and self.cfg.deploy_command
                    and self.provider is not None)

    # -- context -----------------------------------------------------------
    def _source_for(self, path: Path) -> Optional[OemSourceConfig]:
        rp = path.resolve()
        for s in self.sources:
            root = Path(s.path).resolve()
            if rp == root or root in rp.parents:
                return s
        return None

    def context_files(self, hits: List[Dict[str, Any]], terms: List[str]) -> Dict[str, List[int]]:
        """Editable files to show the LLM, with the lines that matter in each."""
        files: Dict[str, List[int]] = {}
        for h in hits:
            p = Path(h["file"])
            src = self._source_for(p)
            # A package name matches the manifest and build files, never the defect
            if src and h.get("term") in src.packages:
                continue
            if src and not PROTECTED.search(str(p)):
                files.setdefault(str(p), []).append(int(h["line"]))
        # A framework resource (config_xxx, a string) that the OEM build does
        # not override yet is fixed in the product's resource overlay, which a
        # grep for the name cannot find because the name is not there
        overlays: Dict[str, List[int]] = {}
        if any(t.startswith("config_") for t in terms):
            for s in self.sources:
                for f in sorted(Path(s.path).glob("**/overlay/frameworks/base/core/res/res/values/config.xml")):
                    overlays[str(f)] = []
        # The overlays are where such a fix goes: they must not be crowded out
        return dict(list(overlays.items()) + [kv for kv in files.items() if kv[0] not in overlays][:max(1, 5 - len(overlays))])

    def resource_facts(self, terms: List[str], hits: List[Dict[str, Any]]) -> str:
        """Framework config defaults named by the failure, and whether this product overrides them."""
        facts = []
        names = sorted({t for t in terms if re.fullmatch(r"config_\w+", t)} |
                       {m for h in hits for m in re.findall(r'name="(config_\w+)"', h.get("snippet", ""))})
        overlays = [f for s in self.sources
                    for f in sorted(Path(s.path).glob("**/overlay/frameworks/base/core/res/res/values/config.xml"))]
        # Only resources about what the failure is about (e.g. "Dialer"), not every neighbour in the snippet
        generic = {"config", "default", "show", "enable", "android", "permission", "package", "enabled"}
        words = {w.lower() for t in terms for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", t)} - generic
        words = {w for w in words if len(w) >= 4}
        names = [n for n in names if n in terms or any(w in n.lower() for w in words)]
        for name in names:
            default = None
            for s in self.reference:
                for f in sorted(Path(s.path).glob("**/config.xml")):
                    m = re.search(rf'<(\w+) name="{name}"[^>]*>([^<]*)</\1>', f.read_text(encoding="utf-8", errors="replace"))
                    if m:
                        default = (str(f), m.group(2).strip())
                        break
                if default:
                    break
            if default is None:
                continue
            over = [str(f) for f in overlays if f'name="{name}"' in f.read_text(encoding="utf-8", errors="replace")]
            line = (f'  {name}: framework default "{default[1]}" ({default[0]}); overridden by this product\'s '
                    f'editable overlays: {", ".join(over) if over else "NONE"} (checked: {", ".join(map(str, overlays)) or "-"})')
            pkg = default[1]
            if self.device_shell and re.fullmatch(r"[a-z][\w]*(\.[\w]+)+", pkg):
                installed = "package:" in (self.device_shell(f"pm path {pkg}") or "")
                line += f'; on the DUT, package {pkg} is {"installed" if installed else "NOT installed"}'
            facts.append(line)
        return ("RESOURCE CHECK (measured):\n" + "\n".join(facts) + "\n") if facts else ""

    @staticmethod
    def render_file(path: str, lines_of_interest: List[int]) -> str:
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        if len(lines) <= FULL_FILE_LINES or not lines_of_interest:
            body = _numbered(lines[:FULL_FILE_LINES], 0)
            if len(lines) > FULL_FILE_LINES:
                body += f"\n      ... ({len(lines) - FULL_FILE_LINES} more lines)"
            return body
        chunks, last_end = [], 0
        for ln in sorted(set(lines_of_interest)):
            start, end = max(last_end, ln - 1 - WINDOW // 2), min(len(lines), ln + WINDOW // 2)
            if start >= end:
                continue
            if start > last_end:
                chunks.append("      ...")
            chunks.append(_numbered(lines[start:end], start))
            last_end = end
        return "\n".join(chunks)

    @staticmethod
    def _strings(files: List[str]) -> Dict[str, Tuple[str, str]]:
        """name -> (file:line, value) for default-locale strings of the shown res trees."""
        values: Dict[str, Tuple[str, str]] = {}
        for root in sorted({Path(f).parent.parent for f in files if "/res/" in f}):
            for sf in sorted(root.glob("values/*.xml")):
                try:
                    text = sf.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for i, ln in enumerate(text.splitlines(), 1):
                    m = re.search(r'<string name="(\w+)"[^>]*>(.*?)</string>', ln)
                    if m and m.group(1) not in values:
                        values[m.group(1)] = (f"{sf}:{i}", m.group(2))
        return values

    def resolve_strings(self, code: str, files: List[str], failures: str = "") -> str:
        """Values of the @string resources the shown code uses, plus a UI-text check.

        The check is what an engineer does by eye: the test looks for a text;
        resources in the same family (same name suffix) carry that text, but
        one that the shown screen uses does not. That one was changed.
        """
        used = sorted(set(re.findall(r"@string/(\w+)", code)))
        values = self._strings(files)
        out = [f'  @string/{n} = "{values[n][1]}"   ({values[n][0]})' for n in used if n in values]
        text = ("Resolved string resources (default locale, what the UI shows):\n" + "\n".join(out) + "\n") if out else ""
        findings = []
        for expected in sorted(set(re.findall(r"'([^'\n]{3,80})'", failures))):
            carriers = [n for n, (_, v) in values.items() if v == expected]
            suffixes = {n.split("_", 1)[1] for n in carriers if "_" in n}
            for n in used:
                if n in values and values[n][1] != expected and "_" in n and n.split("_", 1)[1] in suffixes:
                    sib = ", ".join(c for c in carriers if c.split("_", 1)[-1] == n.split("_", 1)[1])
                    findings.append(f'  The test expects the text "{expected}". The shown screen uses @string/{n} = '
                                    f'"{values[n][1]}" ({values[n][0]}), while its siblings {sib} still say "{expected}".')
        if findings:
            text += "UI text check:\n" + "\n".join(findings) + "\n"
        return text

    # -- LLM ---------------------------------------------------------------
    def propose(self, failures: str, analysis: str, files: Dict[str, List[int]], previous: str = "",
                facts: str = "") -> Patch:
        code = "\n\n".join(f"=== {f}\n{self.render_file(f, lines)}" for f, lines in files.items())
        resources = self.resolve_strings(code, list(files), failures)[:4000] + facts
        checks = resources[resources.find("UI text check:"):] if "UI text check:" in resources else ""
        self._last_checks = checks + (facts if facts not in checks else "")
        prompt = PATCH_PROMPT.format(
            failures=failures[:5000], analysis=analysis[:3000], code=code[:14000], resources=resources,
            previous=f"\nA previous attempt did not work:\n{previous[:3000]}\n" if previous else "",
        )
        model = getattr(self.provider, "model_id", "") or getattr(self.provider, "model", "") or type(self.provider).__name__
        raw, data, err = "", None, ""
        for ask in (prompt, prompt + "\nYour previous reply was not valid JSON. Reply with the JSON object only; "
                                     "keep root_cause and explanation to 2-3 sentences."):
            try:
                with self.llm_lock:
                    raw = self.provider.generate(ask, json_mode=True) or ""
                data = load_json_object(raw)
                break
            except Exception as exc:
                err = f"LLM patch step failed: {exc}; output started {raw[:200]!r}"
        if not isinstance(data, dict):
            return Patch(model=model, error=err)
        edits = [
            Edit(str(e.get("file", "")), str(e.get("find", "")), str(e.get("replace", "")))
            for e in data.get("edits") or [] if isinstance(e, dict)
        ]
        return Patch(root_cause=str(data.get("root_cause", "")), explanation=str(data.get("explanation", "")),
                     edits=edits, model=model)

    def review(self, patch: Patch, failures: str, analysis: str) -> List[str]:
        """Second opinion on an applied patch; problems found (empty = looks right)."""
        prompt = REVIEW_PROMPT.format(failures=failures[:3000], analysis=analysis[:3000], diff=patch.diff[:6000],
                                      resources=self._last_checks)
        try:
            with self.llm_lock:
                raw = self.provider.generate(prompt, json_mode=True) or ""
            data = load_json_object(raw)
        except Exception as exc:
            logger.warning("patch review failed: %s", exc)
            return []
        patch.review_notes = [str(n) for n in data.get("notes") or []][:4]
        if data.get("ok", True) is True:
            return []
        return [str(p) for p in data.get("problems") or []][:6] or ["reviewer rejected the patch"]

    # -- source edits ------------------------------------------------------
    def apply(self, patch: Patch, shown: List[str]) -> None:
        """Validate every edit first, then write; raises PatchError and changes nothing on a bad edit."""
        if not patch.edits:
            raise PatchError("the LLM proposed no edits")
        if len(patch.edits) > MAX_EDITS:
            raise PatchError(f"{len(patch.edits)} edits; at most {MAX_EDITS} allowed")
        staged: Dict[Path, str] = {}
        changed = 0
        for e in patch.edits:
            path = Path(e.file)
            if str(path) not in shown:
                raise PatchError(f"{e.file}: not one of the files the LLM was shown")
            if PROTECTED.search(str(path)):
                raise PatchError(f"{e.file}: tests and CTS are never edited")
            if self._source_for(path) is None:
                raise PatchError(f"{e.file}: outside every editable OEM source")
            text = staged[path] if path in staged else path.read_text(encoding="utf-8")
            find = e.find.strip("\n")
            if not find.strip():
                raise PatchError(f"{e.file}: empty 'find' block")
            count = text.count(find)
            if count == 1:
                new = text.replace(find, e.replace.strip("\n"), 1)
            else:
                by_lines = _replace_by_lines(text, find, e.replace.strip("\n"))
                if by_lines is None:
                    raise PatchError(f"{e.file}: 'find' block occurs {count} times (must be exactly once)")
                new = by_lines
            changed += max(len(find.splitlines()), len(e.replace.splitlines()))
            staged[path] = new
        if changed > MAX_CHANGED_LINES:
            raise PatchError(f"patch changes ~{changed} lines; at most {MAX_CHANGED_LINES} allowed")
        for path, text in staged.items():
            self._originals.setdefault(path, path.read_text(encoding="utf-8"))
            path.write_text(text, encoding="utf-8")
        patch.files = [str(p) for p in staged]
        patch.modules = sorted({m for p in staged for m in getattr(self._source_for(p), "build_targets", None) or []})
        patch.diff = self.diff()

    def diff(self) -> str:
        """git diff of every touched file, paths relative to the source tree root."""
        out = []
        for path in self._originals:
            repo = self._git_root(path.parent)
            if repo is None:
                continue
            top = self._tree_root(repo)
            prefix = f"{repo.relative_to(top)}/" if top and top != repo else ""
            proc = subprocess.run(
                ["git", "-C", str(repo), "diff", "--no-color", f"--src-prefix=a/{prefix}", f"--dst-prefix=b/{prefix}",
                 "--", str(path.resolve().relative_to(repo))],
                capture_output=True, text=True, check=False,
            )
            out.append(proc.stdout)
        return "".join(out)

    @staticmethod
    def _git_root(path: Path) -> Optional[Path]:
        proc = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, check=False)
        return Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None

    @staticmethod
    def _tree_root(repo: Path) -> Optional[Path]:
        """Top of a repo-managed tree (the dir holding .repo), else None."""
        for p in [repo, *repo.parents]:
            if (p / ".repo").is_dir():
                return p
        return None

    def revert(self) -> List[str]:
        reverted = []
        for path, text in self._originals.items():
            path.write_text(text, encoding="utf-8")
            reverted.append(str(path))
        self._originals.clear()
        return reverted

    def keep(self, session_id: str, patch: Patch) -> Path:
        """The verified patch stays in the tree and is saved for review."""
        out = self.out_dir / "patches" / f"{session_id}.patch"
        out.parent.mkdir(parents=True, exist_ok=True)
        header = f"# xts-agent fix-loop patch (verified on DUT, not committed)\n# root cause: {patch.root_cause}\n"
        out.write_text(header + patch.diff, encoding="utf-8")
        self._originals.clear()
        return out

    # -- build / flash -----------------------------------------------------
    @staticmethod
    def _shell(cmd: str, cwd: str, timeout: int, log: Path) -> Tuple[int, str]:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w", encoding="utf-8") as fh:
            try:
                proc = subprocess.run(["bash", "-c", cmd], cwd=cwd or None, stdout=fh, stderr=subprocess.STDOUT,
                                      timeout=timeout, check=False, start_new_session=True)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc = 124
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        return rc, "\n".join(tail)

    def build(self, modules: List[str], log: Path) -> Dict[str, Any]:
        cmd = self.cfg.build_command.replace("{modules}", " ".join(modules))
        t0 = time.time()
        rc, tail = self._runner(cmd, self.cfg.workdir, self.cfg.build_timeout_secs, log)
        errors = [ln for ln in tail.splitlines() if re.search(r"\berror\b|FAILED:", ln)][:20]
        return {"ok": rc == 0, "rc": rc, "secs": round(time.time() - t0), "command": cmd, "log": str(log),
                "errors": errors}

    def deploy(self, serial: str, cvd_instance: str, log: Path) -> Dict[str, Any]:
        cmd = self.cfg.deploy_command.replace("{serial}", serial).replace("{cvd_instance}", cvd_instance)
        t0 = time.time()
        rc, tail = self._runner(cmd, self.cfg.workdir, self.cfg.deploy_timeout_secs, log)
        return {"ok": rc == 0, "rc": rc, "secs": round(time.time() - t0), "command": cmd, "log": str(log),
                "tail": tail.splitlines()[-5:]}
