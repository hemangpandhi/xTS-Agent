"""Command Line Interface for xTS Agent."""

from __future__ import annotations
import click
import logging
from xts_agent.orchestrator import Orchestrator

# Configure root logger temporarily until utils logger is set up
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@click.group()
def main():
    """xTS Agent for AAOS test automation."""
    pass

@main.command()
def setup():
    """Setup environment."""
    click.echo("Setting up environment...")

@main.command()
@click.option('--plan', required=True, help='Path to test plan YAML')
@click.option('--auto-retry', is_flag=True, help='Automatically retry failures')
@click.option('--dry-run', is_flag=True, help='Dry run without executing tests')
def run(plan: str, auto_retry: bool, dry_run: bool):
    """Run test plan."""
    orchestrator = Orchestrator(plan)
    orchestrator.run_plan(auto_retry, dry_run)

@main.command()
@click.option('--plan', required=True, help='Path to test plan YAML')
@click.option('--max-retries', type=int, default=3, help='Maximum number of retries')
def retry(plan: str, max_retries: int):
    """Suite-level retry."""
    orchestrator = Orchestrator(plan)
    orchestrator.retry_plan(max_retries)

@main.command()
@click.option('--rca', is_flag=True, help='Enable RCA')
@click.option('--classify-failures', is_flag=True, help='Classify failures')
def analyze(rca: bool, classify_failures: bool):
    """Run RCA analysis."""
    click.echo("Running analysis...")

@main.command()
@click.option('--format', type=click.Choice(['html', 'json', 'junit']), default='html', help='Report format')
def report(format: str):
    """Generate reports."""
    click.echo(f"Generating {format} report...")

@main.command()
@click.option('--min-devices', type=int, default=1, help='Minimum required devices')
def device_check(min_devices: int):
    """Check device health."""
    click.echo(f"Checking for at least {min_devices} devices...")

@main.command()
@click.option('--reboot-unhealthy', is_flag=True, help='Reboot unhealthy devices')
def health_check(reboot_unhealthy: bool):
    """Full health check."""
    click.echo("Running health check...")

@main.command()
@click.option('--kill-tradefed', is_flag=True, help='Kill running TradeFed processes')
def cleanup(kill_tradefed: bool):
    """Cleanup."""
    click.echo("Cleaning up...")

@main.command()
def download():
    """Download xTS packages."""
    click.echo("Downloading packages...")

if __name__ == '__main__':
    main()
