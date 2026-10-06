"""Command Line Interface for xTS Agent."""

from __future__ import annotations

import logging
import sys
from typing import List, Optional

import click

from xts_agent.orchestrator import Orchestrator

logging.basicConfig(level=logging.INFO)
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
def run(plan: str, config_path: Optional[str], auto_retry: bool, dry_run: bool):
    """Run test plan."""
    orchestrator = _build_orchestrator(plan, config_path)
    result = orchestrator.run_plan(auto_retry=auto_retry, dry_run=dry_run)
    if result.overall_status not in ("PASSED", "DRY_RUN"):
        sys.exit(1)


@main.command()
@click.option("--plan", required=True, help="Path to test plan YAML")
@click.option("--config", "config_path", default=None, help="Path to default_config.yaml")
@click.option("--max-retries", type=int, default=3, help="Maximum number of retries")
def retry(plan: str, config_path: Optional[str], max_retries: int):
    """Suite-level retry for the last run in this process (or re-init from plan)."""
    orchestrator = _build_orchestrator(plan, config_path)
    # Warm-load plan; if no prior results, instruct user
    result = orchestrator.retry_plan(max_retries)
    if result is None:
        click.echo(
            "No in-memory prior results. For CI, prefer `run --auto-retry` in the execute stage.",
            err=True,
        )
        sys.exit(2)
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
        )
    if unhealthy:
        click.echo(f"Unhealthy devices: {unhealthy}", err=True)
        sys.exit(1)


@main.command()
@click.option("--plan", default="config/test_plans/smoke_test.yaml")
@click.option("--config", "config_path", default=None)
@click.option("--kill-tradefed", is_flag=True, help="Kill running TradeFed processes")
def cleanup(plan: str, config_path: Optional[str], kill_tradefed: bool):
    """Cleanup agent allocations and optional TradeFed processes."""
    orchestrator = _build_orchestrator(plan, config_path)
    try:
        orchestrator._initialize()
    except Exception:
        pass
    orchestrator.cleanup(kill_tradefed=kill_tradefed)
    click.echo("Cleanup complete.")


@main.command()
@click.option("--plan", default="config/test_plans/smoke_test.yaml")
@click.option("--config", "config_path", default=None)
def download(plan: str, config_path: Optional[str]):
    """Download xTS packages (delegates to ops script guidance)."""
    orchestrator = _build_orchestrator(plan, config_path)
    orchestrator._initialize()
    orchestrator.download_packages()


if __name__ == "__main__":
    main()
