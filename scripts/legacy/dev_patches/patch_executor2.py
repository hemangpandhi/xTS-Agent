import re
with open('/mnt/xTS_Agent/xts_agent/execution/test_plan_executor.py', 'r') as f:
    content = f.read()

content = "import dataclasses\n" + content
content = content.replace("retry_config=getattr(suite_config, 'retry', None),", "retry_config=dataclasses.asdict(getattr(suite_config, 'retry')) if getattr(suite_config, 'retry', None) else {},")

with open('/mnt/xTS_Agent/xts_agent/execution/test_plan_executor.py', 'w') as f:
    f.write(content)
