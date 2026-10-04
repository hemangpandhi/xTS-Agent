import os
import ast

def patch_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    if "from __future__ import annotations" in content:
        return
        
    lines = content.splitlines()
    insert_idx = 0
    
    # Try to parse to find module docstring
    try:
        tree = ast.parse(content)
        if ast.get_docstring(tree):
            # docstring is present, find where it ends
            for i, line in enumerate(lines):
                if line.strip().endswith('"""') or line.strip().endswith("'''"):
                    if i > 0 or len(line.strip()) > 3: # rough heuristic
                        insert_idx = i + 1
                        # make sure it's actually the end of the docstring by just appending after the first few lines
                        pass
    except Exception:
        pass
        
    # simpler approach: find first import or code
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and not stripped.startswith('"""') and not stripped.startswith("'''") and not stripped.endswith('"""') and not stripped.endswith("'''"):
            insert_idx = i
            break
            
    lines.insert(insert_idx, "from __future__ import annotations")
    
    with open(filepath, 'w') as f:
        f.write("\n".join(lines) + "\n")

for root, _, files in os.walk('xts_agent'):
    for f in files:
        if f.endswith('.py'):
            patch_file(os.path.join(root, f))
