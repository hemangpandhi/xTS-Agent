"""Configuration loader for xTS Agent."""

from __future__ import annotations
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

@dataclass
class RetryConfig:
    max_retries: int = 1
    retry_strategy: str = "RETRY_ANY_FAILURE"

@dataclass
class ShardingConfig:
    shard_count: int = 1

@dataclass
class DeviceRequirements:
    min_devices: int = 1
    properties: Dict[str, str] = field(default_factory=dict)

@dataclass
class PostExecutionConfig:
    rca_enabled: bool = False
    report_formats: List[str] = field(default_factory=lambda: ["html"])

@dataclass
class SuiteConfig:
    name: str
    plan: str = "default"
    enabled: bool = True
    exclude_filters: List[str] = field(default_factory=list)
    include_filters: List[str] = field(default_factory=list)
    retry: RetryConfig = field(default_factory=RetryConfig)
    sharding: ShardingConfig = field(default_factory=ShardingConfig)

@dataclass
class TestPlanConfig:
    name: str
    suites: List[SuiteConfig]
    devices: DeviceRequirements = field(default_factory=DeviceRequirements)
    post_execution: PostExecutionConfig = field(default_factory=PostExecutionConfig)

class ConfigLoader:
    """Loads and validates configuration from YAML files."""
    
    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)
        
    def load_plan(self) -> TestPlanConfig:
        """Loads a test plan configuration."""
        with open(self.config_path, 'r') as f:
            data = yaml.safe_load(f)
            
        suites = []
        for suite_data in data.get('suites', []):
            suites.append(SuiteConfig(
                name=suite_data.get('name', 'unknown'),
                plan=suite_data.get('plan', 'default'),
                enabled=suite_data.get('enabled', True),
                exclude_filters=suite_data.get('exclude_filters', []),
                include_filters=suite_data.get('include_filters', []),
                retry=RetryConfig(max_retries=suite_data.get('retry', {}).get('max_retries', 1), retry_strategy=suite_data.get('retry', {}).get('strategy', "RETRY_ANY_FAILURE")),
                sharding=ShardingConfig(shard_count=suite_data.get('sharding', {}).get('shard_count', 1))
            ))
            
        return TestPlanConfig(
            name=data.get('name', 'Unnamed Plan'),
            suites=suites,
            devices=DeviceRequirements(**data.get('devices', {})),
            post_execution=PostExecutionConfig()
        )
