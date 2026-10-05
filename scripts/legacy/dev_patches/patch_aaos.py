with open('/mnt/xTS_Agent/xts_agent/execution/test_plan_executor.py', 'r') as f:
    content = f.read()

old = 'extra_args.extend(["--include-filter", "*", "--user", "10"])'
new = 'extra_args.extend(["--user", "10"])'

content = content.replace(old, new)

with open('/mnt/xTS_Agent/xts_agent/execution/test_plan_executor.py', 'w') as f:
    f.write(content)
