"""Command Line Interface for xTS Agent."""

from __future__ import annotations

import logging
import os
import sys
from typing import List, Optional

import click

from xts_agent.config_loader import ConfigError
from xts_agent.execution import cancel
from xts_agent.execution.test_plan_executor import PlanResult
from xts_agent.orchestrator import Orchestrator, PreflightError
from xts_agent.utils.logger import setup_logging

logger = logging.getLogger(__name__)


def _parse_formats(value: str) -> List[str]:
    parts = [p.strip().lower() for p in value.replace(";", ",").split(",") if p.strip()]
    allowed = {"html", "json", "junit"}
    unknown = set(parts) - allowed
    if unknown:
        raise click.BadParameter(f"Unknown format(s): {', '.join(sorted(unknown))}")
    return parts


def _build_orchestrator(plan: Optional[str], config: Optional[str]) -> Orchestrator:
    if not plan:
        # Some commands (report/analyze) may rely on prior in-process state; still require plan for init
        raise click.UsageError("--plan is required")
    return Orchestrator(plan_path=plan, defaults_path=config)


@click.group()
def main():
    """xTS Agent for AAOS test automation."""
    # Console only until a plan is loaded (the orchestrator adds the log file)
    # Console level until the plan's agent.log_level takes over (XTS_LOG_LEVEL=ERROR for quiet shells)
    setup_logging(getattr(logging, os.environ.get("XTS_LOG_LEVEL", "INFO").upper(), logging.INFO))


@main.command()
@click.option("--config", "config_path", default=None, help="Path to default_config.yaml")
def setup(config_path: Optional[str]):
    """Validate host environment and create result directories."""
    from pathlib import Path

    from xts_agent.config_loader import ConfigLoader
    from xts_agent.utils.env_validator import EnvironmentValidator

    click.echo("Setting up environment...")
    defaults = ConfigLoader(
        plan_path=config_path or "config/default_config.yaml",
        defaults_path=config_path,
    ).load_defaults()
    for key in ("log_dir", "results_dir"):
        path = Path(defaults.get("agent", {}).get(key, key.replace("_dir", "s")))
        path.mkdir(parents=True, exist_ok=True)
        click.echo(f"  ensured {path}")
    sdk = EnvironmentValidator.resolve_android_sdk()
    click.echo(f"  ANDROID SDK: {sdk or '(not found — set ANDROID_HOME)'}")
    click.echo("Setup complete.")


@main.command()
@click.option("--plan", required=True, help="Path to test plan YAML")
@click.option("--config", "config_path", default=None, help="Path to default_config.yaml")
@click.option("--auto-retry", is_flag=True, help="Automatically retry failures")
@click.option("--dry-run", is_flag=True, help="Dry run without executing tests")
@click.option(
    "--resume",
    is_flag=True,
    help="Continue an interrupted run of this plan: skip passed suites, "
    "retry incomplete ones from their last TradeFed session",
)
def run(plan: str, config_path: Optional[str], auto_retry: bool, dry_run: bool, resume: bool):
    """Run test plan."""
    orchestrator = _build_orchestrator(plan, config_path)
    cancel.install_signal_handlers()
    try:
        result = orchestrator.run_plan(auto_retry=auto_retry, dry_run=dry_run, resume=resume)
    except (ConfigError, PreflightError) as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(3)
    if result.cancelled:
        sys.exit(cancel.exit_code())
    if result.overall_status not in ("PASSED", "DRY_RUN"):
        sys.exit(1)


@main.command()
@click.option("--plan", required=True, help="Path to test plan YAML")
@click.option("--config", "config_path", default=None, help="Path to default_config.yaml")
@click.option("--max-retries", type=int, default=3, help="Maximum number of retries")
def retry(plan: str, config_path: Optional[str], max_retries: int):
    """Suite-level retry for the last run in this process (or re-init from plan)."""
    orchestrator = _build_orchestrator(plan, config_path)
    cancel.install_signal_handlers()
    # Warm-load plan; if no prior results, instruct user
    result = orchestrator.retry_plan(max_retries)
    if result is None:
        click.echo(
            "No in-memory prior results. For CI, prefer `run --auto-retry` in the execute stage.",
            err=True,
        )
        sys.exit(2)
    if result.cancelled:
        sys.exit(cancel.exit_code())
    if result.overall_status != "PASSED":
        sys.exit(1)


@main.command()
@click.option("--plan", default=None, help="Path to test plan YAML (for config/RCA settings)")
@click.option("--config", "config_path", default=None, help="Path to default_config.yaml")
@click.option("--rca/--no-rca", default=True, show_default=True, help="Run RCA")
@click.option(
    "--classify-failures/--no-classify-failures",
    default=True,
    show_default=True,
    help="Apply rule-based failure classification",
)
def analyze(plan: Optional[str], config_path: Optional[str], rca: bool, classify_failures: bool):
    """Run RCA analysis on the last plan result (in-memory or results/reports)."""
    if not plan:
        plan = "config/test_plans/full_certification.yaml"
    orchestrator = _build_orchestrator(plan, config_path)
    if not rca:
        click.echo("RCA disabled (--no-rca); nothing to analyze.")
        return
    report = orchestrator.analyze(enable_rca=True, classify_failures=classify_failures)
    if report is None:
        sys.exit(2)
    click.echo(f"RCA classified {len(report.failures)} failure(s): {dict(report.summary)}")


@main.command()
@click.option("--plan", default=None, help="Path to test plan YAML")
@click.option("--config", "config_path", default=None, help="Path to default_config.yaml")
@click.option(
    "--format",
    "fmt",
    default="html,json,junit",
    help="Comma-separated formats: html,json,junit",
)
def report(plan: Optional[str], config_path: Optional[str], fmt: str):
    """Generate reports from the latest plan result / JSON artifact."""
    formats = _parse_formats(fmt)
    if not plan:
        plan = "config/test_plans/full_certification.yaml"
    orchestrator = _build_orchestrator(plan, config_path)
    orchestrator._initialize()
    if orchestrator.last_plan_result is None:
        orchestrator.last_plan_result = orchestrator._load_latest_plan_result()
    if orchestrator.last_plan_result is None:
        click.echo("No prior results found under results/reports/", err=True)
        sys.exit(2)
    orchestrator.generate_reports(orchestrator.last_plan_result, formats=formats)
    click.echo(f"Generated formats: {', '.join(formats)}")


@main.command()
@click.option("--plan", default="config/test_plans/full_certification.yaml")
@click.option("--config", "config_path", default=None)
@click.option(
    "--results-dir",
    "results_dirs",
    multiple=True,
    help="Triage these TradeFed result dirs directly (repeatable)",
)
@click.option(
    "--import-history",
    "history_dirs",
    multiple=True,
    help="Only record these past TradeFed result dirs into failure history (repeatable)",
)
@click.option("--suite", default="CTS", show_default=True, help="Suite name for --results-dir/--import-history")
@click.option("--top", default=15, show_default=True, help="Groups to print")
@click.option(
    "--report",
    "with_report",
    is_flag=True,
    help="Also write the HTML/JSON/JUnit reports (with the triage section) for these results",
)
def triage(plan, config_path, results_dirs, history_dirs, suite, top, with_report):
    """Group failures by root cause with history, known issues and owners."""
    orchestrator = _build_orchestrator(plan, config_path)
    orchestrator._initialize()
    if history_dirs:
        from xts_agent.results.result_parser import ResultParser

        history = orchestrator.triage_engine().history
        # oldest first so NEW/PERSISTENT labels see runs in order
        dirs = sorted(
            history_dirs,
            key=lambda d: ResultParser().parse_xml(f"{d}/test_result.xml").start_ms,
        )
        for d in dirs:
            run_id = history.import_results_dir(suite, d, plan=orchestrator.plan.name)
            click.echo(f"{'recorded' if run_id else 'already recorded'}: {d}")
        return

    result: Optional[PlanResult]
    if results_dirs:
        result = orchestrator.plan_result_from_results_dirs(suite, list(results_dirs))
    else:
        result = orchestrator._load_latest_plan_result()
        if result is None:
            click.echo("No prior results; pass --results-dir", err=True)
            sys.exit(2)
    report = orchestrator._run_triage(result)
    if report is None:
        click.echo("Triage disabled or failed; see log", err=True)
        sys.exit(1)
    s = report.summary
    click.echo(
        f"{s['failures']} failures -> {s['groups']} groups ({s['actionable_groups']} actionable, "
        f"{s['known_groups']} known, {s['waived_groups']} waived); history: {s['by_label']}"
    )
    for g in report.groups[:top]:
        known = f" [{g.known.issue.id}{' waived' if g.waived else ''}]" if g.known else ""
        click.echo(f"{g.group.count:5d}  {g.label:<10} {g.owner.team:<22} {g.group.title[:70]}{known}")
    if orchestrator._last_triage_html:
        click.echo(f"triage page: {orchestrator._last_triage_html}")
    if with_report:
        orchestrator.generate_reports(result)
        for path in orchestrator._last_report_files:
            click.echo(f"report: {path}")


@main.command()
@click.option("--plan", default="config/test_plans/full_certification.yaml")
@click.option("--config", "config_path", default=None)
def dashboard(plan: str, config_path: Optional[str]):
    """Regenerate the trends dashboard (results/reports/dashboard.html)."""
    orchestrator = _build_orchestrator(plan, config_path)
    orchestrator._initialize()
    path = orchestrator.write_dashboard()
    if path is None:
        sys.exit(1)
    click.echo(f"Dashboard: {path}")


@main.command("index-code")
@click.option("--plan", default="config/test_plans/full_certification.yaml")
@click.option("--config", "config_path", default=None)
def index_code(plan: str, config_path: Optional[str]):
    """Build/update the local OEM source index used by AI RCA (needs the [ai] extra)."""
    orchestrator = _build_orchestrator(plan, config_path)
    orchestrator._initialize()
    paths = orchestrator.plan.ai_rca.source_code_paths
    if not paths:
        click.echo("ai_rca.source_code_paths is empty; nothing to index", err=True)
        sys.exit(2)
    total = orchestrator.code_indexer().index_codebase()
    click.echo(f"Indexed {total} chunks from {', '.join(paths)}")


@main.command("device-check")
@click.option("--plan", default="config/test_plans/smoke_test.yaml")
@click.option("--config", "config_path", default=None)
@click.option("--min-devices", type=int, default=1, help="Minimum required devices")
def device_check(plan: str, config_path: Optional[str], min_devices: int):
    """Check device availability."""
    orchestrator = _build_orchestrator(plan, config_path)
    orchestrator._initialize()
    ok = orchestrator.device_check(min_devices)
    if not ok:
        click.echo(f"Insufficient devices (need {min_devices})", err=True)
        sys.exit(1)
    click.echo(f"OK: at least {min_devices} device(s) available")


@main.command("health-check")
@click.option("--plan", default="config/test_plans/smoke_test.yaml")
@click.option("--config", "config_path", default=None)
@click.option("--reboot-unhealthy", is_flag=True, help="Reboot unhealthy devices")
def health_check(plan: str, config_path: Optional[str], reboot_unhealthy: bool):
    """Full device health check."""
    orchestrator = _build_orchestrator(plan, config_path)
    orchestrator._initialize()
    reports = orchestrator.health_check(reboot_unhealthy=reboot_unhealthy)
    unhealthy = [s for s, r in reports.items() if not r.healthy]
    for serial, report in reports.items():
        click.echo(
            f"{serial}: healthy={report.healthy} battery={report.battery_level} "
            f"free_mb={report.storage_free_mb} internet={report.has_internet}"
            + (f" problems={'; '.join(report.problems)}" if report.problems else "")
        )
    if unhealthy:
        click.echo(f"Unhealthy devices: {unhealthy}", err=True)
        sys.exit(1)


@main.command()
@click.option("--plan", default="config/test_plans/smoke_test.yaml")
@click.option("--config", "config_path", default=None)
@click.option("--release", "release_serial", default=None, help="Release this serial from quarantine")
def quarantine(plan: str, config_path: Optional[str], release_serial: Optional[str]):
    """List quarantined devices, or release one."""
    orchestrator = _build_orchestrator(plan, config_path)
    orchestrator._initialize()
    ledger = orchestrator.device_manager.ledger
    if release_serial:
        if ledger.release(release_serial):
            click.echo(f"Released {release_serial}")
        else:
            click.echo(f"{release_serial} is not quarantined", err=True)
            sys.exit(1)
        return
    entries = ledger.quarantined()
    if not entries:
        click.echo("No devices in quarantine.")
    for serial, reason in entries.items():
        click.echo(f"{serial}: {reason}")


@main.command()
@click.option("--plan", default="config/test_plans/smoke_test.yaml")
@click.option("--config", "config_path", default=None)
@click.option(
    "--kill-tradefed",
    is_flag=True,
    help="Kill TradeFed processes started by this agent (recorded pid files only)",
)
@click.option(
    "--prune-results",
    is_flag=True,
    help="Delete TradeFed results/logs and agent TradeFed logs older than --keep-days",
)
@click.option("--keep-days", type=float, default=None, help="Default: ops.keep_results_days")
@click.option("--keep-latest", type=int, default=3, show_default=True, help="Sessions per suite always kept")
@click.option("--dry-run", is_flag=True, help="With --prune-results: only list what would be deleted")
def cleanup(plan: str, config_path: Optional[str], kill_tradefed: bool, prune_results: bool,
            keep_days: Optional[float], keep_latest: int, dry_run: bool):
    """Cleanup agent allocations, TradeFed processes and old results."""
    orchestrator = _build_orchestrator(plan, config_path)
    try:
        orchestrator._initialize()
    except Exception:
        if prune_results:
            raise
    orchestrator.cleanup(kill_tradefed=kill_tradefed)
    if prune_results:
        days = keep_days if keep_days is not None else orchestrator.plan.ops.keep_results_days
        if not days or days <= 0:
            raise click.UsageError("--prune-results needs --keep-days N (or ops.keep_results_days)")
        pruned = orchestrator.prune_results(days, keep_latest=keep_latest, dry_run=dry_run)
        for path in pruned.paths:
            click.echo(f"{'would delete' if dry_run else 'deleted'}: {path}")
        click.echo(
            f"{'Would free' if dry_run else 'Freed'} {(pruned.bytes if dry_run else pruned.freed) / 1024**3:.1f} GB "
            f"({len(pruned.paths)} path(s); {pruned.kept_sessions} session(s) kept)"
        )
    click.echo("Cleanup complete.")


@main.command("fix-loop")
@click.option("--plan", default="config/test_plans/full_cts.yaml", show_default=True)
@click.option("--config", "config_path", default=None, help="Path to default_config.yaml")
@click.option(
    "--test", "tests", multiple=True,
    help='Target: "<Module> [Class#method ...]" (repeatable), e.g. "CtsOsTestCases android.os.cts.StrictModeTest#testNetwork"',
)
@click.option("--from-results", "from_results", default=None, help="Take failing tests from a TradeFed results dir")
@click.option("--top", default=5, show_default=True, help="With --from-results: modules to fix (most failures first)")
@click.option("--serial", "serials", multiple=True, required=True, help="DUT serial (repeat to fix targets in parallel)")
@click.option("--spare", "spares", multiple=True, help="Spare DUT a target moves to if its DUT is lost (repeatable)")
@click.option("--max-iterations", type=int, default=None, help="TradeFed runs per target (default: fix_loop.max_iterations)")
@click.option("--until-pass", "rounds", flag_value=5, default=1, help="Keep cycling unfixed targets (up to 5 rounds)")
@click.option("--llm/--no-llm", "use_llm", default=None, help="Use the local LLM (default: fix_loop.use_llm)")
@click.option("--code-fix/--no-code-fix", "code_fix", default=None,
              help="Patch, build and flash OEM source for product bugs (default: fix_loop.code_fix.enabled)")
@click.option("--report", "report_path", default=None, help="HTML output (default: <out_dir>/fixloop_<stamp>.html)")
def fix_loop(plan, config_path, tests, from_results, top, serials, spares, max_iterations, rounds, use_llm, code_fix,
             report_path):
    """Run, diagnose, remediate and re-run failing xTS tests until they pass."""
    import shlex
    import threading
    from datetime import datetime
    from pathlib import Path

    from xts_agent.config_loader import ConfigLoader
    from xts_agent.fixloop import report as fl_report
    from xts_agent.fixloop.catalog import RemediationCatalog
    from xts_agent.fixloop.codefix import CodeFixConfig, CodeFixer
    from xts_agent.fixloop.device import DeviceSession
    from xts_agent.fixloop.executor import TargetExecutor, TestTarget
    from xts_agent.fixloop.loop import DevicePool, FixLoop, run_queue
    from xts_agent.fixloop.oem_sources import OemCodeLocator
    from xts_agent.fixloop.planner import Planner

    # Exact invocation, quoted in reports and the demo video
    click.echo("$ xts-agent " + " ".join(shlex.quote(a) for a in sys.argv[1:]))
    cfg = ConfigLoader(plan, config_path).load_plan()
    fl = cfg.fix_loop
    targets = [TestTarget.parse(t) for t in tests]
    if from_results:
        targets += _targets_from_results(from_results, top)
    if not targets:
        raise click.UsageError("give --test and/or --from-results")

    provider = None
    if fl.use_llm if use_llm is None else use_llm:
        import dataclasses

        from xts_agent.rca.llm_provider import get_llm_provider

        ai_cfg = dataclasses.replace(cfg.ai_rca, request_timeout_secs=fl.llm_timeout_secs)
        if fl.llm_base_url or fl.llm_model:
            ai_cfg = dataclasses.replace(
                ai_cfg, provider="openai_compatible",
                openai_base_url=fl.llm_base_url or ai_cfg.openai_base_url,
                openai_model=fl.llm_model or ai_cfg.openai_model,
                openai_extra_body=fl.llm_extra_body or ai_cfg.openai_extra_body,
            )
        try:
            provider = get_llm_provider(ai_cfg)
            if fl.llm_fallbacks:
                from xts_agent.rca.llm_provider import FallbackProvider

                chain = [provider] + [
                    get_llm_provider(dataclasses.replace(
                        ai_cfg, provider="openai_compatible",
                        openai_base_url=fb.get("base_url", ai_cfg.openai_base_url),
                        openai_model=fb.get("model", ""), openai_extra_body=fb.get("extra_body") or {},
                    ))
                    for fb in fl.llm_fallbacks
                ]
                provider = FallbackProvider(chain)
        except Exception as exc:
            click.echo(f"LLM unavailable ({exc}); continuing with rules only", err=True)
    catalog = RemediationCatalog.load(fl.remediations_file)
    locator = OemCodeLocator.from_config(fl.oem_sources)
    cf_cfg = CodeFixConfig.from_dict(fl.code_fix)
    if code_fix is not None:
        cf_cfg.enabled = code_fix
    if cf_cfg.enabled and provider is None:
        click.echo("code fixes need the LLM; product bugs will end as NEEDS_CODE_FIX", err=True)
    cancel.install_signal_handlers()

    def device(serial: str) -> DeviceSession:
        return DeviceSession(serial, adb=fl.adb_path, restart_command=fl.restart_command)

    pool = DevicePool(list(spares), device) if spares else None
    # One worker per DUT; targets are dealt round-robin
    buckets = {s: targets[i::len(serials)] for i, s in enumerate(serials)}
    sessions = []
    lock = threading.Lock()

    def work(serial: str, items) -> None:
        loop = FixLoop(
            TargetExecutor(fl.suite_path, fl.command_name, fl.plan, Path(fl.out_dir) / "logs", fl.run_timeout_hours),
            device(serial),
            Planner(catalog, locator, provider),
            out_dir=fl.out_dir,
            max_iterations=max_iterations or fl.max_iterations,
            confirm_runs=fl.confirm_runs,
            flaky_retries=fl.flaky_retries,
            variables=fl.variables,
            pool=pool,
            code_fixer=CodeFixer(cf_cfg, [s.cfg for s in locator.sources], provider, Planner._llm_lock,
                                 Path(fl.out_dir)),
        )
        for session in run_queue(loop, items, max_rounds=rounds):
            with lock:
                sessions.append(session)
                click.echo(f"[{session.status:<19}] {serial}  {session.target['name']}\n    {session.summary}")

    threads = [threading.Thread(target=work, args=(s, b), daemon=True) for s, b in buckets.items() if b]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    locator.close()

    out = report_path or str(Path(fl.out_dir) / f"fixloop_{datetime.now():%Y%m%d_%H%M%S}.html")
    model = getattr(provider, "model", "") if provider else ""
    html = fl_report.render([s.to_dict() for s in sessions], out, model=model)
    click.echo(f"fix-loop page: {html}")
    sys.exit(cancel.exit_code() if cancel.cancelled() else 0)


def _targets_from_results(results_dir: str, top: int):
    """Failing tests in a TradeFed results dir, grouped per module, worst first."""
    from collections import defaultdict
    from pathlib import Path

    from xts_agent.fixloop.executor import TestTarget
    from xts_agent.results.result_parser import ResultParser

    xml = Path(results_dir) / "test_result.xml"
    parsed = ResultParser().parse_xml(xml)
    by_module = defaultdict(list)
    for mod in parsed.modules:
        failed = [f"{t.class_name}#{t.test_name}" for t in mod.test_cases if t.result.upper() in ("FAIL", "ERROR")]
        if failed or (not mod.done and mod.reason):
            by_module[(mod.abi, mod.name)].extend(failed)
    ranked = sorted(by_module.items(), key=lambda kv: -len(kv[1]))[:top]
    # Cap tests per target so each iteration stays a short, focused run
    return [TestTarget(module=name, tests=tests[:15], abi=abi) for (abi, name), tests in ranked]


if __name__ == "__main__":
    main()
