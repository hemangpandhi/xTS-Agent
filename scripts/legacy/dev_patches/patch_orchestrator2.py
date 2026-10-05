with open('/mnt/xTS_Agent/xts_agent/orchestrator.py', 'r') as f:
    lines = f.readlines()

new_lines = []
for l in lines:
    if "from __future__ import annotations" in l or "import shutil" in l or '"""Main' in l or 'from datetime import datetime' in l:
        continue
    new_lines.append(l)

final = ['from __future__ import annotations\n', '"""Main orchestration engine for xTS Agent."""\n', 'import shutil\n', 'from datetime import datetime\n'] + new_lines

with open('/mnt/xTS_Agent/xts_agent/orchestrator.py', 'w') as f:
    f.writelines(final)
