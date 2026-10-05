with open('/mnt/xTS_Agent/xts_agent/orchestrator.py', 'r') as f:
    content = f.read()

old_logic = "if suite_res.results_dir and Path(suite_res.results_dir).exists():"
new_logic = "if suite_res.results_dir and Path(suite_res.results_dir).is_dir():"

content = content.replace(old_logic, new_logic)

with open('/mnt/xTS_Agent/xts_agent/orchestrator.py', 'w') as f:
    f.write(content)
