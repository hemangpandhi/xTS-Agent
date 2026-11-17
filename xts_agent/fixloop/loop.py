"""The fix loop: run -> diagnose -> remediate -> re-run, until the target passes.

Every iteration is a real TradeFed invocation on the DUT and is recorded with
its result directory, so each claim in the report can be traced to a
test_result.xml. A target only counts as FIXED after the fix has passed in a
confirmation run as well. Remediations that do not help are reverted.
Product bugs go to the code-fix stage when ``fix_loop.code_fix`` is set up:
the LLM patches the editable OEM source, the loop builds and flashes it and
re-runs the target (FIXED_IN_CODE). Without it, or when no patch works, they
end as NEEDS_CODE_FIX with the root cause and OEM code references.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from xts_agent.execution import cancel

from .catalog import Remediation, UnsafeCaptureError, resolve_tool_dirs
from .codefix import CodeFixer, PatchError
from .device import DeviceSession
from .executor import RunOutcome, TargetExecutor, TestTarget
from .planner import Candidate, Planner

logger = logging.getLogger(__name__)

# Final states of a target
FIXED = "FIXED"
# Green after the loop patched OEM source, rebuilt and reflashed the DUT
FIXED_IN_CODE = "FIXED_IN_CODE"
# Green only with a device-side workaround that proves a product root cause;
# the permanent fix still has to ship in OEM code/config
WORKAROUND_VERIFIED = "WORKAROUND_VERIFIED"
ALREADY_PASSING = "ALREADY_PASSING"
FLAKY = "FLAKY"
NEEDS_CODE_FIX = "NEEDS_CODE_FIX"
NO_FIX_FOUND = "NO_FIX_FOUND"
DEVICE_LOST = "DEVICE_LOST"
ABORTED = "ABORTED"
ERROR = "ERROR"


class DevicePool:
    """Spare DUTs shared by all workers; a lost DUT's target moves to one."""

    def __init__(self, serials: List[str], factory: Callable[[str], DeviceSession]):
        self._spares = list(serials)
        self._factory = factory
        self._lock = threading.Lock()

    def take(self) -> Optional[DeviceSession]:
        while True:
            with self._lock:
                if not self._spares:
                    return None
                serial = self._spares.pop(0)
            dev = self._factory(serial)
            ok, why = dev.healthy()
            if ok:
                return dev
            logger.warning("spare %s skipped: %s", serial, why)


class _StopTarget(Exception):
    """The session already has its final status (e.g. DEVICE_LOST)."""


class _Failover(Exception):
    """The DUT was lost and the target moved to a spare; start over there."""


# Iterations that are bookkeeping, not TradeFed runs counted against the budget
NOT_A_RUN = ("failover", "not_applied", "device_lost", "code_fix", "restore")


@dataclass
class Iteration:
    n: int
    kind: str  # baseline | remediate | retry | confirm | failover
    started: str
    remediation: Optional[Dict[str, Any]] = None
    actions: List[str] = field(default_factory=list)
    outcome: Optional[Dict[str, Any]] = None
    verdict: str = ""  # green | improved | no_change | worse | not_applied
    note: str = ""
    diagnosis: Optional[Dict[str, Any]] = None
    # code_fix iterations: the patch, its build and the flash
    patch: Optional[Dict[str, Any]] = None
    build: Optional[Dict[str, Any]] = None
    deploy: Optional[Dict[str, Any]] = None


@dataclass
class FixSession:
    id: str
    target: Dict[str, Any]
    serial: str
    started: str
    status: str = "RUNNING"
    fingerprint: str = ""
    finished: str = ""
    iterations: List[Iteration] = field(default_factory=list)
    applied: List[Dict[str, Any]] = field(default_factory=list)  # remediations kept
    summary: str = ""
    patch_file: str = ""  # verified OEM source patch, for review

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class FixLoop:
    # One build tree: code fixes from parallel workers take turns
    _build_lock = threading.Lock()

    def __init__(
        self,
        executor: TargetExecutor,
        device: DeviceSession,
        planner: Planner,
        out_dir: str | Path = "results/fixloop",
        max_iterations: int = 6,
        confirm_runs: int = 1,
        flaky_retries: int = 1,
        variables: Optional[Dict[str, str]] = None,
        pool: Optional[DevicePool] = None,
        max_device_losses: int = 3,
        code_fixer: Optional[CodeFixer] = None,
    ):
        self.executor = executor
        self.device = device
        self.planner = planner
        self.out_dir = Path(out_dir)
        self.max_iterations = max_iterations
        self.confirm_runs = confirm_runs
        self.flaky_retries = flaky_retries
        self.variables = dict(variables or {})
        self.pool = pool
        self.max_device_losses = max_device_losses
        self.code_fixer = code_fixer if code_fixer is not None and code_fixer.available else None

    # -- persistence -------------------------------------------------------
    def _save(self, session: FixSession) -> Path:
        path = self.out_dir / "sessions" / f"{session.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(session.to_dict(), indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
        return path

    # -- one target --------------------------------------------------------
    def run(self, target: TestTarget) -> FixSession:
        session = FixSession(
            id=f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}",
            target=dict(asdict(target), name=target.name),
            serial=self.device.serial,
            started=_now(),
        )
        try:
            while True:
                try:
                    self._run(session, target)
                    break
                except _Failover:
                    continue  # re-baseline on the spare DUT
                except _StopTarget:
                    break
        except cancel.RunCancelled:
            session.status, session.summary = ABORTED, "cancelled by operator"
        except Exception as exc:  # one bad target must not stop a queue
            logger.exception("fix loop failed for %s", target.name)
            session.status, session.summary = ERROR, f"{type(exc).__name__}: {exc}"
        session.finished = _now()
        self._save(session)
        return session

    def _runs_used(self, session: FixSession) -> int:
        return sum(1 for i in session.iterations if i.kind not in NOT_A_RUN and i.verdict not in NOT_A_RUN)

    def _device_lost(self, session: FixSession, why: str) -> None:
        """Move to a spare DUT (raises _Failover) or end the session as DEVICE_LOST."""
        losses = sum(1 for i in session.iterations if i.kind == "failover" or i.verdict == "device_lost")
        spare = self.pool.take() if self.pool is not None and losses <= self.max_device_losses else None
        if spare is None:
            session.status, session.summary = DEVICE_LOST, f"{self.device.serial}: {why}"
            return
        old = self.device.serial
        session.iterations.append(Iteration(
            n=len(session.iterations), kind="failover", started=_now(), verdict="failover",
            note=f"{old} lost ({why}); target moved to spare {spare.serial} and re-baselined",
        ))
        # Device-side fixes stay on the old DUT; host-side ones (TradeFed PATH) carry over
        session.applied = [a for a in session.applied if a.get("scope") == "host_tool"]
        self.device = spare
        session.serial = spare.serial
        self._save(session)
        logger.warning("[%s] %s lost (%s); moved to %s", session.target.get("name"), old, why, spare.serial)
        raise _Failover()

    def _execute(self, session: FixSession, target: TestTarget, kind: str, **extra: Any) -> Optional[RunOutcome]:
        for _ in range(self.max_device_losses + 1):
            cancel.check()
            ok, why = self.device.recover()
            if not ok:
                self._device_lost(session, why)
                return None
            it = Iteration(n=len(session.iterations), kind=kind, started=_now(), **extra)
            logger.info("[%s] iteration %d (%s) on %s", target.name, it.n, kind, self.device.serial)
            outcome = self.executor.run(target, self.device.serial)
            it.outcome = outcome.to_dict()
            cancel.check()
            healthy, why = self.device.healthy()
            if not healthy:
                # A DUT that died mid-run produced no trustworthy result
                it.verdict = "device_lost"
                it.note = f"DUT went away during the run ({why}); result discarded, recovering"
                session.iterations.append(it)
                self._save(session)
                continue
            it.verdict = "green" if outcome.green else it.verdict
            session.iterations.append(it)
            self._save(session)
            return outcome
        session.status, session.summary = DEVICE_LOST, f"{self.device.serial} kept dropping during runs"
        return None

    def _run(self, session: FixSession, target: TestTarget) -> None:
        session.fingerprint = self.device.shell("getprop ro.build.fingerprint").out.strip()
        current = self._execute(session, target, "baseline")
        if current is None:
            return
        if current.green:
            session.status = ALREADY_PASSING
            session.summary = f"{current.passed} test(s) passed on the first run; nothing to fix"
            return

        tried: List[str] = []
        retried = 0
        while self._runs_used(session) < self.max_iterations:
            cancel.check()
            diag = self.planner.diagnose(current, self.device, tried)
            last_run = next(i for i in reversed(session.iterations) if i.outcome and i.verdict != "device_lost")
            last_run.diagnosis = diag.to_dict()
            self._save(session)

            if not diag.candidates:
                if retried < self.flaky_retries:
                    retried += 1
                    nxt = self._execute(session, target, "retry", note="no remediation matched; plain re-run to check for flakiness")
                    if nxt is None:
                        return
                    if nxt.green:
                        failed = self._confirm(session, target)
                        if failed is None:
                            session.status = FLAKY
                            session.summary = "failed, then passed on an unchanged re-run (and its confirmation): flaky"
                            return
                        nxt = failed
                    current = nxt
                    continue
                if self._code_fix_warranted(diag.to_dict()) and self._code_fix(session, target, current, diag.to_dict()):
                    return
                self._finish_unfixed(session, diag.to_dict())
                return

            cand = diag.candidates[0]
            tried.append(cand.remediation.id)
            actions, applied_ok = self._apply(cand)
            if not applied_ok:
                session.iterations.append(Iteration(
                    n=len(session.iterations), kind="not_applied", started=_now(),
                    remediation=cand.to_dict(), actions=actions, verdict="not_applied",
                    note="remediation could not be applied",
                ))
                continue
            nxt = self._execute(session, target, "remediate", remediation=cand.to_dict(), actions=actions)
            if nxt is None:
                return
            it = session.iterations[-1]
            if nxt.green:
                session.applied.append(self._applied_record(cand, actions))
                failed = self._confirm(session, target)
                if failed is None:
                    workaround = any(a.get("workaround") for a in session.applied)
                    session.status = WORKAROUND_VERIFIED if workaround else FIXED
                    session.summary = self._fixed_summary(session, target)
                    if workaround and self.code_fixer is not None:
                        # The workaround proved the root cause; now ship it in code
                        self._code_fix(session, target, current, diag.to_dict(), workaround=cand)
                    return
                it.note = "passed once but not in the confirmation run"
                current = failed
                continue
            if self._improved(current, nxt):
                it.verdict = "improved"
                it.note = f"problems {current.problems} -> {nxt.problems}; signatures changed: next layer reached"
                session.applied.append(self._applied_record(cand, actions))
            else:
                it.verdict = "no_change"
                it.note = "no improvement; reverted" + self._revert(cand)
            self._save(session)
            current = nxt if it.verdict == "improved" else current

        if session.status == "RUNNING":
            last = next((i.diagnosis for i in reversed(session.iterations) if i.diagnosis), None)
            if last and self._is_product(last) and self._code_fix(session, target, current, last):
                return
            self._finish_unfixed(session, last or {}, budget=True)

    # -- code fix ----------------------------------------------------------
    @staticmethod
    def _is_product(diag: Dict[str, Any]) -> bool:
        ai = diag.get("ai") or {}
        return ai.get("classification") == "PRODUCT_BUG" or (not ai.get("classification") and diag.get("rule_category") == "PRODUCT_BUG")

    def _code_fix_warranted(self, diag: Dict[str, Any]) -> bool:
        """Product bug, or: nothing left to try on the lab side and the failure leads into editable OEM code."""
        if self._is_product(diag):
            return True
        fixer = self.code_fixer
        oem = diag.get("oem") or {}
        return fixer is not None and bool(fixer.context_files(oem.get("hits") or [], []))

    def _code_fix(self, session: FixSession, target: TestTarget, failing: RunOutcome, diag: Dict[str, Any],
                  workaround: Optional[Candidate] = None) -> bool:
        """Patch OEM source, build, flash, re-run. True when the target is green in code."""
        fixer = self.code_fixer
        if fixer is None:
            return False
        oem = diag.get("oem") or {}
        files = fixer.context_files(oem.get("hits") or [], oem.get("terms") or [])
        if not files:
            session.iterations.append(Iteration(
                n=len(session.iterations), kind="code_fix", started=_now(), verdict="no_patch",
                note="no editable OEM source matched the failure; nothing to patch",
            ))
            self._save(session)
            return False
        failures = _failure_brief(failing)
        analysis = _analysis_brief(diag, workaround, failures)
        fixer.device_shell = lambda cmd: self.device.shell(cmd, timeout=30).out
        facts = fixer.resource_facts(oem.get("terms") or [], oem.get("hits") or [])
        ruled_out = [i.remediation["title"] for i in session.iterations
                     if i.kind == "remediate" and i.verdict == "no_change" and i.remediation]
        if ruled_out:
            analysis += "\nPROVEN not the cause (applied on the device, no effect, reverted): " + "; ".join(ruled_out)
        previous, deployed = "", False
        reviewed_out: set = set()
        with self._build_lock:
            for attempt in range(1, fixer.cfg.max_attempts + 1):
                cancel.check()
                it = Iteration(n=len(session.iterations), kind="code_fix", started=_now(),
                               note=f"attempt {attempt}/{fixer.cfg.max_attempts}")
                patch = fixer.propose(failures, analysis, files, previous, facts)
                it.patch = patch.to_dict()
                if patch.error:
                    it.verdict, it.note = "no_patch", patch.error
                    session.iterations.append(it)
                    self._save(session)
                    break
                try:
                    fixer.apply(patch, list(files))
                except PatchError as exc:
                    it.verdict, it.note = "patch_rejected", f"patch rejected before build: {exc}"
                    it.patch = patch.to_dict()
                    previous = f"Your patch was rejected: {exc}"
                    session.iterations.append(it)
                    self._save(session)
                    continue
                # The review is a cheap filter, not the judge: a diff proposed again after a
                # rejection goes to the DUT, where CTS decides
                insisted = patch.diff in reviewed_out
                problems = [] if insisted else fixer.review(patch, failures, analysis)
                if insisted:
                    it.note += "; proposed again after review rejection, so built and tested on the DUT"
                if problems and attempt < fixer.cfg.max_attempts:
                    reviewed_out.add(patch.diff)
                    # Cheaper than a build: let the LLM fix what its own review found
                    fixer.revert()
                    it.verdict, it.note = "patch_rejected", "self-review before build: " + "; ".join(problems)
                    it.patch = patch.to_dict()
                    previous = f"Your patch:\n{patch.diff}\nwas rejected in review:\n" + "\n".join(problems)
                    session.iterations.append(it)
                    self._save(session)
                    continue
                it.patch = patch.to_dict()
                session.iterations.append(it)
                self._save(session)
                logs = self.out_dir / "builds" / f"{session.id}_{it.n}"
                it.build = fixer.build(patch.modules, logs.with_suffix(".build.log"))
                self._save(session)
                if not it.build["ok"]:
                    fixer.revert()
                    it.verdict, it.note = "build_failed", f"build failed (rc={it.build['rc']}); patch reverted"
                    previous = f"This patch did not build:\n{patch.diff}\nErrors:\n" + "\n".join(it.build["errors"])
                    self._save(session)
                    continue
                if workaround is not None:
                    self._revert(workaround)
                it.deploy = fixer.deploy(self.device.serial, self.device.cvd_instance(), logs.with_suffix(".flash.log"))
                deployed = True
                if not it.deploy["ok"]:
                    fixer.revert()
                    it.verdict, it.note = "deploy_failed", f"flash failed (rc={it.deploy['rc']}); patch reverted"
                    self._save(session)
                    break
                it.verdict = "flashed"
                self._save(session)
                nxt = self._execute(session, target, "verify_fix",
                                    note=f"first run on the patched build (attempt {attempt})")
                if nxt is None:
                    fixer.revert()
                    raise _StopTarget()
                if nxt.green and self._confirm(session, target) is None:
                    session.patch_file = str(fixer.keep(session.id, patch))
                    session.applied = [a for a in session.applied if not a.get("workaround")]
                    session.status = FIXED_IN_CODE
                    session.summary = (
                        f"{target.name}: green on a rebuilt image with the OEM source patch "
                        f"({', '.join(Path(f).name for f in patch.files)}); "
                        f"{self._runs_used(session)} TradeFed runs, confirmed. Patch: {session.patch_file}"
                    )
                    self._save(session)
                    return True
                last = session.iterations[-1]
                last.note = (last.note + "; still failing, patch reverted").strip("; ")
                fixer.revert()
                previous = (f"This patch built and was flashed, but the test still fails:\n{patch.diff}\n"
                            f"Failures now:\n{_failure_brief(nxt)[:1500]}")
                self._save(session)
            if deployed and fixer.cfg.restore_on_failure:
                self._restore_build(session)
        return False

    def _restore_build(self, session: FixSession) -> None:
        """Put the DUT back on the unpatched source so it matches the tree."""
        fixer = self.code_fixer
        assert fixer is not None
        it = Iteration(n=len(session.iterations), kind="restore", started=_now(),
                       note="no patch fixed the target; rebuilding and reflashing the original source")
        logs = self.out_dir / "builds" / f"{session.id}_{it.n}"
        it.build = fixer.build([], logs.with_suffix(".build.log"))
        if it.build["ok"]:
            it.deploy = fixer.deploy(self.device.serial, self.device.cvd_instance(), logs.with_suffix(".flash.log"))
        it.verdict = "restored" if it.build["ok"] and (it.deploy or {}).get("ok") else "restore_failed"
        session.iterations.append(it)
        self._save(session)

    # -- helpers -----------------------------------------------------------
    def _confirm(self, session: FixSession, target: TestTarget) -> Optional[RunOutcome]:
        """None when every confirmation run is green, else the failing outcome."""
        for _ in range(self.confirm_runs):
            out = self._execute(session, target, "confirm")
            if out is None:
                raise _StopTarget()
            if not out.green:
                return out
        return None

    @staticmethod
    def _improved(before: RunOutcome, after: RunOutcome) -> bool:
        if after.problems < before.problems:
            return True
        # A module that could not even start now runs its tests: next layer
        more_executed = (after.passed + after.failed) > (before.passed + before.failed)
        return more_executed and after.signatures() != before.signatures()

    def _apply(self, cand: Candidate) -> tuple:
        rem = cand.remediation
        actions: List[str] = []
        try:
            if rem.scope == "host_tool":
                return self._apply_host_tool(rem)
            if rem.scope == "reboot":
                ok = self.device.reboot()
                return [f"adb -s {self.device.serial} reboot (waited for boot: {ok})"], ok
            if rem.scope == "retry":
                return ["(no change; re-run)"], True
            for cmd in rem.apply:
                rendered = rem.render(cmd, cand.match, self.variables)
                res = self.device.shell(rendered, timeout=60)
                actions.append(f"adb shell {rendered}  -> rc={res.rc} {res.out.strip()[:120]}")
            if rem.settle_secs:
                time.sleep(rem.settle_secs)
                actions.append(f"waited {rem.settle_secs}s to settle")
            return actions, True
        except UnsafeCaptureError as exc:
            actions.append(f"refused: {exc}")
            return actions, False

    def _apply_host_tool(self, rem: Remediation) -> tuple:
        actions = []
        for d in resolve_tool_dirs(rem):
            if d in self.executor.path_prepend:
                continue
            exe = str(Path(d) / rem.tool)
            argv = [exe] + [self._host_arg(a) for a in rem.verify_args]
            rc, out = self.device._runner(argv, 30)
            actions.append(f"{' '.join(argv)} -> rc={rc} {out.strip().splitlines()[0][:100] if out.strip() else ''}")
            if rc == 0:
                self.executor.path_prepend.insert(0, d)
                actions.append(f"TradeFed PATH now starts with {d} (host system untouched)")
                return actions, True
        actions.append(f"no working {rem.tool} found in {rem.tool_candidates}")
        return actions, False

    def _host_arg(self, arg: str) -> str:
        if arg == "{any_test_apk}":
            testcases = Path(self.executor.runner.suite_path) / "testcases"
            return str(next(testcases.rglob("*.apk"), ""))
        return arg

    def _revert(self, cand: Candidate) -> str:
        rem = cand.remediation
        if rem.scope == "host_tool":
            dirs = resolve_tool_dirs(rem)
            self.executor.path_prepend = [d for d in self.executor.path_prepend if d not in dirs]
            return " (PATH restored)"
        if rem.scope == "device" and rem.revert:
            for cmd in rem.revert:
                try:
                    self.device.shell(rem.render(cmd, cand.match, self.variables), timeout=60)
                except UnsafeCaptureError:
                    pass
            return f" ({len(rem.revert)} revert command(s))"
        return ""

    @staticmethod
    def _applied_record(cand: Candidate, actions: List[str]) -> Dict[str, Any]:
        rem = cand.remediation
        return {**cand.to_dict(), "why": rem.why, "actions": actions, "prep_hint": rem.prep_hint,
                "references": rem.references, "workaround": rem.workaround, "permanent_fix": rem.permanent_fix}

    def _fixed_summary(self, session: FixSession, target: TestTarget) -> str:
        steps = ", ".join(a["id"] for a in session.applied)
        moved = [i.note for i in session.iterations if i.kind == "failover"]
        tail = f"; {len(moved)} DUT failover(s)" if moved else ""
        return f"{target.name}: green after {steps} ({self._runs_used(session)} TradeFed runs, confirmed{tail})"

    @staticmethod
    def _finish_unfixed(session: FixSession, diag: Dict[str, Any], budget: bool = False) -> None:
        ai = diag.get("ai") or {}
        if ai.get("classification") == "PRODUCT_BUG" or (not ai and diag.get("rule_category") == "PRODUCT_BUG"):
            session.status = NEEDS_CODE_FIX
            session.summary = (
                "no environment remediation helped; root cause points at product code. "
                + (ai.get("suggested_fix") or "See AI root cause and OEM code references.")
            )[:1500]
        else:
            session.status = NO_FIX_FOUND
            session.summary = "iteration budget used up" if budget else "no allow-listed remediation matched"


GREEN_STATES = (FIXED, FIXED_IN_CODE, WORKAROUND_VERIFIED, ALREADY_PASSING, FLAKY)


def _failure_brief(outcome: RunOutcome) -> str:
    text = "\n\n".join(
        f"[{f.kind}] {f.test_id}\n{f.message[:600]}\n{f.stack[:1200]}" for f in outcome.failures[:4]
    )
    if outcome.setup_errors:
        text += "\n" + "\n".join(outcome.setup_errors[:5])
    return text


def _analysis_brief(diag: Dict[str, Any], workaround: Optional[Candidate], failures: str = "") -> str:
    ai = diag.get("ai") or {}
    parts = []
    if ai.get("root_cause"):
        parts.append(f"Hypothesis from triage (unverified): {ai['root_cause']}")
    if ai.get("suggested_fix"):
        parts.append(f"Suggested fix: {ai['suggested_fix']}")
    if ai.get("evidence"):
        parts.append(f"Evidence: {ai['evidence']}")
    dev = diag.get("device") or {}
    for key in ("focus", "top_activity", "focused_window"):
        if dev.get(key):
            parts.append(f"Device {key}: {dev[key][:600]}")
    fw = dev.get("focused_window", "")
    ty = re.search(r"\bty=([A-Z_]+)", fw)
    if re.search(r"focus", failures, re.I) and ty and ty.group(1) not in ("BASE_APPLICATION", "APPLICATION") \
            and re.search(r"\bfl=", fw) and "NOT_FOCUSABLE" not in fw:
        owner = re.search(r"package=(\S+)", fw)
        title = re.search(r"Window\{\S+ u\d+ ([^}]+)\}", fw)
        parts.append(
            f"FACT (from the device): input focus is held by the system window "
            f"'{title.group(1) if title else '?'}' of {owner.group(1) if owner else 'a system app'} "
            f"(type {ty.group(1)}), whose flags do NOT include FLAG_NOT_FOCUSABLE. A window with "
            f"FLAG_NOT_FOCUSABLE can never take input focus. Expected behaviour: system bar / overlay "
            f"windows keep FLAG_NOT_FOCUSABLE so that the app under test gets focus. Find where this "
            f"window's flags are built and why FLAG_NOT_FOCUSABLE is missing for it, and make it set "
            f"FLAG_NOT_FOCUSABLE again (do not remove the flag from other windows)."
        )
    if workaround is not None:
        rem = workaround.remediation
        parts.append(
            f"PROVEN: the device-side workaround '{rem.title}' made the tests pass "
            f"({'; '.join(rem.apply)}). {rem.why} The permanent fix must ship in the build: {rem.permanent_fix}"
        )
    return "\n".join(parts) or "(no analysis)"


def run_queue(loop: FixLoop, targets: List[TestTarget], max_rounds: int = 1, pause_secs: int = 0) -> List[FixSession]:
    """Run targets one by one; later rounds retry only the ones still red.

    Remediations applied in one round stay on the device, so a later round
    starts from the improved state (``--until-pass`` uses several rounds).
    """
    sessions: List[FixSession] = []
    pending = list(targets)
    for round_no in range(1, max(1, max_rounds) + 1):
        still: List[TestTarget] = []
        for target in pending:
            session = loop.run(target)
            sessions.append(session)
            if session.status == ABORTED:
                return sessions
            if session.status not in GREEN_STATES:
                still.append(target)
        pending = still
        if not pending or round_no == max_rounds:
            break
        logger.info("round %d done; %d target(s) still failing", round_no, len(pending))
        if pause_secs and cancel.wait(pause_secs):
            break
    return sessions
