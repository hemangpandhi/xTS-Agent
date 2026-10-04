with open('/mnt/xTS_Agent/xts_agent/execution/test_plan_executor.py', 'r') as f:
    content = f.read()

# 1. Add `details: Any = None` to SuiteResult dataclass
content = content.replace("retry_count: int", "retry_count: int\n    details: Any = None")

# 2. Add `details=parsed if 'parsed' in locals() else None` to the SuiteResult instantiation
old_ret = """            session_id=exec_res.session_id or 0,
            results_dir=exec_res.results_dir or "",
            retry_count=0
        )"""
new_ret = """            session_id=exec_res.session_id or 0,
            results_dir=exec_res.results_dir or "",
            retry_count=0,
            details=parsed if 'parsed' in locals() else None
        )"""
content = content.replace(old_ret, new_ret)

with open('/mnt/xTS_Agent/xts_agent/execution/test_plan_executor.py', 'w') as f:
    f.write(content)
