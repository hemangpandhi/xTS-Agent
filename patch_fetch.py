import re

with open('/home/hemang/android_internals/orchestrator/dist/services/provision.js', 'r') as f:
    content = f.read()

# Replace fetch(url) with fetch(url, { signal: AbortSignal.timeout(2000) })
content = content.replace('const res = await fetch(`http://127.0.0.1:${port}/`);', 'const res = await fetch(`http://127.0.0.1:${port}/`, { signal: AbortSignal.timeout(2000) });')

with open('/home/hemang/android_internals/orchestrator/dist/services/provision.js', 'w') as f:
    f.write(content)
