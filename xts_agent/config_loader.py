"""Configuration loader for xTS Agent.

Supports:
- Merging ``config/default_config.yaml`` with a test plan
- Both simple plans (smoke/full_cts) and rich certification plans
- ``exclude_filters`` as a list or ``{file: path}``
- ``shard_count: auto`` (resolved at execution time)
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

logger = logging.getLogger(__name__)

PROFILES = ("certification", "development")


class ConfigError(ValueError):
    """Raised when a test plan is invalid for its declared profile."""


DEFAULT_CONFIG_CANDIDATES = (
    Path("config/default_config.yaml"),
    Path(__file__).resolve().parent.parent / "config" / "default_config.yaml",
)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _filter_dataclass_kwargs(cls: type, data: dict) -> dict:
    """Keep only fields that exist on the dataclass (ignore unknown YAML keys)."""
    valid = {f.name for f in fields(cls)}
    return {k: v for k, v in (data or {}).items() if k in valid}


def _load_filter_list(raw: Any, plan_dir: Path) -> List[str]:
    """Normalize exclude/include filters from list, dict-with-file, or empty."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    if isinstance(raw, dict):
        file_ref = raw.get("file")
        if not file_ref:
            return []
        path = Path(file_ref)
        if not path.is_absolute():
            # Prefer CWD-relative, then relative to plan file
            candidates = [path, plan_dir / path]
            path = next((c for c in candidates if c.exists()), candidates[0])
        if not path.exists():
            logger.warning("Filter file not found: %s", path)
            return []
        filters: List[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            filters.append(line)
        return filters
    if isinstance(raw, str):
        return [raw] if raw else []
    logger.warning("Unsupported filter format: %r", raw)
    return []


@dataclass
class RetryConfig:
    """Intra-module / suite retry settings passed through to TradeFed where applicable."""

    max_retries: int = 1
    retry_strategy: str = "RETRY_ANY_FAILURE"
    max_testcase_run_count: int = 3
    isolation_grade: str = "REBOOT_ISOLATED"
    reboot_at_last_retry: bool = True
    retry_type: str = "FAILED"


@dataclass
class ShardingConfig:
    enabled: bool = True
    shard_count: Union[int, str] = 1  # int or "auto"
    dynamic_sharding: bool = True
    intra_module_sharding: bool = True
    max_shards: int = 16


@dataclass
class DeviceRequirements:
    min_devices: int = 1
    device_type: str = "any"
    health_check: bool = True
    reboot_between_suites: bool = False
    min_battery_level: int = 20
    properties: Dict[str, str] = field(default_factory=dict)


@dataclass
class SuiteRetryPostConfig:
    enabled: bool = True
    max_suite_retries: int = 2
    retry_type: str = "FAILED"
    cooldown_secs: int = 60


@dataclass
class AgentRetryPostConfig:
    enabled: bool = True
    max_retries: int = 2
    classify_before_retry: bool = True
    rules: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RCAPostConfig:
    enabled: bool = True
    ai_powered: bool = False
    failure_classification: bool = True
    generate_report: bool = True
    baseline_path: str = ""
    patterns_file: str = "config/known_failures/patterns.yaml"
    ai_api_key: str = ""
    ai_model: str = "gemini-2.0-flash"


@dataclass
class ReportingPostConfig:
    formats: List[str] = field(default_factory=lambda: ["html", "junit", "json"])
    html_report: bool = True
    junit_xml: bool = True
    json_summary: bool = True
    regression_report: bool = False
    notifications: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PostExecutionConfig:
    suite_retry: SuiteRetryPostConfig = field(default_factory=SuiteRetryPostConfig)
    agent_retry: AgentRetryPostConfig = field(default_factory=AgentRetryPostConfig)
    rca: RCAPostConfig = field(default_factory=RCAPostConfig)
    reporting: ReportingPostConfig = field(default_factory=ReportingPostConfig)


@dataclass
class ATS2Config:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    timeout_secs: int = 30


@dataclass
class PathsConfig:
    xts_packages_dir: str = "/opt/xts"
    tools_dir: str = "/opt/xts/tools"
    java_home: str = ""
    android_sdk: str = ""
    adb_path: str = ""


@dataclass
class AgentSettings:
    name: str = "xTS Agent"
    version: str = "1.0.0"
    log_level: str = "INFO"
    log_dir: str = "logs"
    results_dir: str = "results"
    database_path: str = "results/xts_agent.db"


@dataclass
class SuiteConfig:
    name: str
    plan: str = "default"
    enabled: bool = True
    priority: int = 100
    package_path: str = ""
    command: str = ""
    timeout_hours: float = 24.0
    description: str = ""
    exclude_filters: List[str] = field(default_factory=list)
    include_filters: List[str] = field(default_factory=list)
    extra_args: List[str] = field(default_factory=list)
    modules: List[str] = field(default_factory=list)
    retry: RetryConfig = field(default_factory=RetryConfig)
    sharding: ShardingConfig = field(default_factory=ShardingConfig)



@dataclass
class AiRcaConfig:
    enabled: bool = False
    # On-prem by default: failure logs and OEM source never leave the host
    provider: str = "llama_cpp"
    # Must be explicitly true to send logs/stack traces/OEM source to a
    # third-party API (e.g. gemini)
    allow_external_providers: bool = False
    request_timeout_secs: int = 120
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-pro"
    llama_model_path: str = ""
    llama_n_ctx: int = 16384
    llama_n_gpu_layers: int = -1
    index_db_path: str = "config/known_failures/chroma_db"
    source_code_paths: List[str] = field(default_factory=list)

@dataclass
class TestPlanConfig:

    name: str
    suites: List[SuiteConfig]
    description: str = ""
    # "certification" runs must execute every module; "development" may filter
    profile: str = "development"
    devices: DeviceRequirements = field(default_factory=DeviceRequirements)
    post_execution: PostExecutionConfig = field(default_factory=PostExecutionConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    agent: AgentSettings = field(default_factory=AgentSettings)
    ats2: ATS2Config = field(default_factory=ATS2Config)
    ai_rca: AiRcaConfig = field(default_factory=AiRcaConfig)
    raw_defaults: Dict[str, Any] = field(default_factory=dict, repr=False)


class ConfigLoader:
    """Loads and validates configuration from YAML files."""

    def __init__(
        self,
        plan_path: str | Path,
        defaults_path: str | Path | None = None,
    ):
        self.plan_path = Path(plan_path)
        self.defaults_path = Path(defaults_path) if defaults_path else self._find_defaults()

    @staticmethod
    def _find_defaults() -> Optional[Path]:
        for candidate in DEFAULT_CONFIG_CANDIDATES:
            if candidate.exists():
                return candidate
        return None

    def load_defaults(self) -> Dict[str, Any]:
        if not self.defaults_path or not self.defaults_path.exists():
            logger.warning("No default_config.yaml found; using built-in defaults")
            return {}
        with open(self.defaults_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        logger.info("Loaded defaults from %s", self.defaults_path)
        return data

    def load_plan(self) -> TestPlanConfig:
        if not self.plan_path.exists():
            raise FileNotFoundError(f"Test plan not found: {self.plan_path}")

        defaults = self.load_defaults()
        with open(self.plan_path, encoding="utf-8") as f:
            plan_data = yaml.safe_load(f) or {}

        # Support nested `plan:` metadata block from certification YAML
        plan_meta = plan_data.get("plan") if isinstance(plan_data.get("plan"), dict) else {}
        name = (
            plan_data.get("name")
            or plan_meta.get("name")
            or "Unnamed Plan"
        )
        description = plan_data.get("description") or plan_meta.get("description") or ""
        profile = str(plan_data.get("profile") or plan_meta.get("profile") or "development").lower()
        if profile not in PROFILES:
            raise ConfigError(f"Unknown profile {profile!r} in {self.plan_path}; use one of {PROFILES}")

        # Device requirements: accept `devices` or `device_requirements`
        device_raw = plan_data.get("devices") or plan_data.get("device_requirements") or {}
        if isinstance(device_raw, dict):
            # Merge with default device section where sensible
            default_device = defaults.get("device", {})
            merged_devices = {
                "min_battery_level": default_device.get("min_battery_level", 20),
                **device_raw,
            }
        else:
            merged_devices = {}
        devices = DeviceRequirements(**_filter_dataclass_kwargs(DeviceRequirements, merged_devices))

        paths = PathsConfig(**_filter_dataclass_kwargs(PathsConfig, defaults.get("paths", {})))
        agent = AgentSettings(**_filter_dataclass_kwargs(AgentSettings, defaults.get("agent", {})))
        ats2 = ATS2Config(**_filter_dataclass_kwargs(ATS2Config, defaults.get("ats2", {})))

        post_execution = self._parse_post_execution(defaults, plan_data.get("post_execution") or {})
        ai_rca = self._parse_ai_rca(defaults, plan_data.get("ai_rca") or {}, post_execution.rca)

        default_retry = defaults.get("retry", {})
        default_sharding = defaults.get("sharding", {})
        default_timeout = float(defaults.get("execution", {}).get("suite_timeout_hours", 24))

        suites: List[SuiteConfig] = []
        for suite_data in plan_data.get("suites", []):
            suites.append(
                self._parse_suite(
                    suite_data,
                    plan_dir=self.plan_path.parent,
                    packages_dir=paths.xts_packages_dir,
                    default_retry=default_retry,
                    default_sharding=default_sharding,
                    default_timeout=default_timeout,
                )
            )

        if profile == "certification":
            self._validate_certification(suites)

        return TestPlanConfig(
            name=name,
            description=description,
            profile=profile,
            suites=suites,
            devices=devices,
            post_execution=post_execution,
            paths=paths,
            agent=agent,
            ats2=ats2,
            ai_rca=ai_rca,
            raw_defaults=defaults,
        )

    def _validate_certification(self, suites: List[SuiteConfig]) -> None:
        """Filtered or partial runs are not valid certification results."""
        problems = []
        for suite in suites:
            if not suite.enabled:
                continue
            for attr in ("exclude_filters", "include_filters", "modules"):
                values = getattr(suite, attr)
                if values:
                    problems.append(f"{suite.name}.{attr} has {len(values)} entr(y/ies)")
        if problems:
            raise ConfigError(
                f"{self.plan_path} is profile: certification but restricts the test set "
                f"({'; '.join(problems)}). Move filters to a development plan and track "
                "known failures as waivers instead."
            )

    @staticmethod
    def _parse_ai_rca(defaults: dict, plan_ai_rca: dict, rca: RCAPostConfig) -> AiRcaConfig:
        """Plan ``ai_rca`` overrides defaults; legacy ``rca.ai_powered`` still enables it."""
        merged = _deep_merge(defaults.get("ai_rca") or {}, plan_ai_rca)
        ai_rca = AiRcaConfig(**_filter_dataclass_kwargs(AiRcaConfig, merged))
        if rca.ai_powered and not ai_rca.enabled:
            ai_rca.enabled = True
        if not ai_rca.gemini_api_key and rca.ai_api_key:
            ai_rca.gemini_api_key = rca.ai_api_key
        return ai_rca

    def _parse_post_execution(self, defaults: dict, plan_post: dict) -> PostExecutionConfig:
        default_retry = defaults.get("retry", {})
        default_rca = defaults.get("rca", {})
        default_reporting = defaults.get("reporting", {})

        suite_retry_src = _deep_merge(
            {
                "enabled": default_retry.get("suite_retry", {}).get("enabled", True),
                "max_suite_retries": default_retry.get("suite_retry", {}).get("max_retries", 2),
                "retry_type": default_retry.get("suite_retry", {}).get("retry_type", "FAILED"),
                "cooldown_secs": default_retry.get("suite_retry", {}).get("cooldown_secs", 60),
            },
            plan_post.get("suite_retry") or {},
        )
        agent_retry_src = _deep_merge(
            {
                "enabled": default_retry.get("agent_retry", {}).get("enabled", True),
                "max_retries": default_retry.get("agent_retry", {}).get("max_retries", 2),
                "classify_before_retry": default_retry.get("agent_retry", {}).get(
                    "classify_before_retry", True
                ),
                "rules": {},
            },
            plan_post.get("agent_retry") or {},
        )
        rca_src = _deep_merge(
            {
                "enabled": default_rca.get("enabled", True),
                "ai_powered": default_rca.get("ai_powered", False),
                "failure_classification": default_rca.get("classify_failures", True),
                "generate_report": default_rca.get("generate_report", True),
                "baseline_path": "",
                "patterns_file": default_rca.get(
                    "patterns_file", "config/known_failures/patterns.yaml"
                ),
                "ai_api_key": default_rca.get("ai_api_key", ""),
                "ai_model": default_rca.get("ai_model", "gemini-2.0-flash"),
            },
            plan_post.get("rca") or {},
        )
        reporting_src = plan_post.get("reporting") or {}
        formats = list(default_reporting.get("formats") or ["html", "junit", "json"])
        if reporting_src:
            # Build formats from boolean flags if present
            fmt_flags = []
            if reporting_src.get("html_report", default_reporting.get("html_report", True)):
                fmt_flags.append("html")
            if reporting_src.get("junit_xml", default_reporting.get("junit_xml", True)):
                fmt_flags.append("junit")
            if reporting_src.get("json_summary", default_reporting.get("json_summary", True)):
                fmt_flags.append("json")
            if fmt_flags:
                formats = fmt_flags

        reporting = ReportingPostConfig(
            formats=formats,
            html_report="html" in formats,
            junit_xml="junit" in formats,
            json_summary="json" in formats,
            regression_report=bool(reporting_src.get("regression_report", False)),
            notifications=reporting_src.get("notifications")
            or default_reporting.get("notifications")
            or {},
        )

        return PostExecutionConfig(
            suite_retry=SuiteRetryPostConfig(
                **_filter_dataclass_kwargs(SuiteRetryPostConfig, suite_retry_src)
            ),
            agent_retry=AgentRetryPostConfig(
                **_filter_dataclass_kwargs(AgentRetryPostConfig, agent_retry_src)
            ),
            rca=RCAPostConfig(**_filter_dataclass_kwargs(RCAPostConfig, rca_src)),
            reporting=reporting,
        )

    def _parse_suite(
        self,
        suite_data: dict,
        plan_dir: Path,
        packages_dir: str,
        default_retry: dict,
        default_sharding: dict,
        default_timeout: float,
    ) -> SuiteConfig:
        name = str(suite_data.get("name", "unknown"))
        name_lower = name.lower()

        package_path = suite_data.get("package_path") or str(
            Path(packages_dir) / f"android-{name_lower}"
        )
        command = suite_data.get("command") or f"{name_lower}-tradefed"

        # Retry: accept max_retries or max_attempts; merge intra_module defaults
        intra = default_retry.get("intra_module", {})
        retry_raw = suite_data.get("retry") or {}
        max_retries = retry_raw.get(
            "max_retries",
            retry_raw.get("max_attempts", 1),
        )
        retry = RetryConfig(
            max_retries=int(max_retries),
            retry_strategy=str(
                retry_raw.get("strategy")
                or retry_raw.get("retry_strategy")
                or intra.get("strategy")
                or "RETRY_ANY_FAILURE"
            ),
            max_testcase_run_count=int(
                retry_raw.get(
                    "max_testcase_run_count",
                    intra.get("max_testcase_run_count", 3),
                )
            ),
            isolation_grade=str(
                retry_raw.get("isolation_grade")
                or intra.get("isolation_grade")
                or "REBOOT_ISOLATED"
            ),
            reboot_at_last_retry=bool(
                retry_raw.get(
                    "reboot_at_last_retry",
                    intra.get("reboot_at_last_retry", True),
                )
            ),
            retry_type=str(retry_raw.get("retry_type", "FAILED")),
        )

        sharding_raw = suite_data.get("sharding") or {}
        shard_count = sharding_raw.get("shard_count", 1)
        if isinstance(shard_count, str) and shard_count.lower() != "auto":
            try:
                shard_count = int(shard_count)
            except ValueError:
                logger.warning(
                    "Invalid shard_count %r for suite %s; defaulting to auto",
                    shard_count,
                    name,
                )
                shard_count = "auto"

        sharding = ShardingConfig(
            enabled=bool(sharding_raw.get("enabled", True)),
            shard_count=shard_count if shard_count is not None else 1,
            dynamic_sharding=bool(
                sharding_raw.get(
                    "dynamic_sharding",
                    default_sharding.get("dynamic_sharding", True),
                )
            ),
            intra_module_sharding=bool(
                sharding_raw.get(
                    "intra_module_sharding",
                    default_sharding.get("intra_module_sharding", True),
                )
            ),
            max_shards=int(
                sharding_raw.get(
                    "max_shards",
                    default_sharding.get("max_shard_count", 16),
                )
            ),
        )

        return SuiteConfig(
            name=name,
            plan=str(suite_data.get("plan", "default")),
            enabled=bool(suite_data.get("enabled", True)),
            priority=int(suite_data.get("priority", 100)),
            package_path=package_path,
            command=command,
            timeout_hours=float(suite_data.get("timeout_hours", default_timeout)),
            description=str(suite_data.get("description", "")),
            exclude_filters=_load_filter_list(suite_data.get("exclude_filters"), plan_dir),
            include_filters=_load_filter_list(suite_data.get("include_filters"), plan_dir),
            extra_args=list(suite_data.get("extra_args") or []),
            modules=list(suite_data.get("modules") or []),
            retry=retry,
            sharding=sharding,
        )
