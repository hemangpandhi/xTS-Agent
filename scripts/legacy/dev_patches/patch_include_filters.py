with open('/mnt/xTS_Agent/xts_agent/execution/test_plan_executor.py', 'r') as f:
    content = f.read()

# We need to add include_filters to build_run_command call
old_call = "cmd = runner.build_run_command(suite_config.plan, shard_count, suite_config.retry, suite_config.exclude_filters, extra_args)"
new_call = """
        include_filters = getattr(suite_config, "include_filters", [])
        if type(suite_config) is dict and "include_filters" in suite_config:
            include_filters = suite_config["include_filters"]
        
        cmd = runner.build_run_command(suite_config.plan, shard_count, suite_config.retry, suite_config.exclude_filters, include_filters, extra_args)
"""
content = content.replace(old_call, new_call)

with open('/mnt/xTS_Agent/xts_agent/execution/test_plan_executor.py', 'w') as f:
    f.write(content)

with open('/mnt/xTS_Agent/xts_agent/execution/tradefed_runner.py', 'r') as f:
    tf_content = f.read()

old_tf_def = "def build_run_command(self, plan: str, shard_count: int, retry_config: dict, exclude_filters: List[str], extra_args: List[str]) -> List[str]:"
new_tf_def = "def build_run_command(self, plan: str, shard_count: int, retry_config: dict, exclude_filters: List[str], include_filters: List[str], extra_args: List[str]) -> List[str]:"
tf_content = tf_content.replace(old_tf_def, new_tf_def)

old_tf_loop = """        for f in exclude_filters:
            cmd.extend(["--exclude-filter", f])
            
        cmd.extend(extra_args)"""
new_tf_loop = """        for f in exclude_filters:
            cmd.extend(["--exclude-filter", f])
            
        for f in include_filters:
            cmd.extend(["--include-filter", f])
            
        cmd.extend(extra_args)"""
tf_content = tf_content.replace(old_tf_loop, new_tf_loop)

with open('/mnt/xTS_Agent/xts_agent/execution/tradefed_runner.py', 'w') as f:
    f.write(tf_content)
