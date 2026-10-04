with open('/mnt/xTS_Agent/xts_agent/execution/test_plan_executor.py', 'r') as f:
    content = f.read()

old_call = """        cmd = runner.build_run_command(
            plan=plan_name,
            shard_count=len(devices),
            retry_config=dataclasses.asdict(getattr(suite_config, 'retry')) if getattr(suite_config, 'retry', None) else {},
            exclude_filters=getattr(suite_config, 'exclude_filters', []),
            extra_args=extra_args
        )"""

new_call = """        cmd = runner.build_run_command(
            plan=plan_name,
            shard_count=len(devices),
            retry_config=dataclasses.asdict(getattr(suite_config, 'retry')) if getattr(suite_config, 'retry', None) else {},
            exclude_filters=getattr(suite_config, 'exclude_filters', []),
            include_filters=getattr(suite_config, 'include_filters', []),
            extra_args=extra_args
        )"""

content = content.replace(old_call, new_call)

with open('/mnt/xTS_Agent/xts_agent/execution/test_plan_executor.py', 'w') as f:
    f.write(content)
