with open('/mnt/xTS_Agent/xts_agent/config_loader.py', 'r') as f:
    content = f.read()

old_suite_dataclass = """@dataclass
class SuiteConfig:
    name: str
    retry: RetryConfig = field(default_factory=RetryConfig)
    sharding: ShardingConfig = field(default_factory=ShardingConfig)"""

new_suite_dataclass = """@dataclass
class SuiteConfig:
    name: str
    plan: str = "default"
    enabled: bool = True
    exclude_filters: List[str] = field(default_factory=list)
    include_filters: List[str] = field(default_factory=list)
    retry: RetryConfig = field(default_factory=RetryConfig)
    sharding: ShardingConfig = field(default_factory=ShardingConfig)"""

content = content.replace(old_suite_dataclass, new_suite_dataclass)

old_load_suites = """        for suite_data in data.get('suites', []):
            suites.append(SuiteConfig(
                name=suite_data.get('name', 'unknown'),
                retry=RetryConfig(max_retries=suite_data.get('retry', {}).get('max_retries', 1), retry_strategy=suite_data.get('retry', {}).get('strategy', "RETRY_ANY_FAILURE")),
                sharding=ShardingConfig(shard_count=suite_data.get('sharding', {}).get('shard_count', 1))
            ))"""

new_load_suites = """        for suite_data in data.get('suites', []):
            suites.append(SuiteConfig(
                name=suite_data.get('name', 'unknown'),
                plan=suite_data.get('plan', 'default'),
                enabled=suite_data.get('enabled', True),
                exclude_filters=suite_data.get('exclude_filters', []),
                include_filters=suite_data.get('include_filters', []),
                retry=RetryConfig(max_retries=suite_data.get('retry', {}).get('max_retries', 1), retry_strategy=suite_data.get('retry', {}).get('strategy', "RETRY_ANY_FAILURE")),
                sharding=ShardingConfig(shard_count=suite_data.get('sharding', {}).get('shard_count', 1))
            ))"""

content = content.replace(old_load_suites, new_load_suites)

with open('/mnt/xTS_Agent/xts_agent/config_loader.py', 'w') as f:
    f.write(content)
