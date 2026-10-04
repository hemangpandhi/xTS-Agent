import re

with open('/mnt/xTS_Agent/xts_agent/results/result_parser.py', 'r') as f:
    content = f.read()

# Add rca fields to TestCaseResult
new_dataclass = """@dataclass
class TestCaseResult:
    class_name: str
    test_name: str
    result: str
    message: Optional[str] = None
    stack_trace: Optional[str] = None
    duration: Optional[int] = None
    rca_category: Optional[str] = None
    rca_recommendation: Optional[str] = None"""

content = re.sub(r'@dataclass\nclass TestCaseResult:.*?duration: Optional\[int\] = None', new_dataclass, content, flags=re.DOTALL)

# Add RCA inference logic
new_parse_logic = """                    stack = None
                    rca_category = None
                    rca_recommendation = None
                    if fail is not None:
                        status = 'FAIL'
                        msg = fail.attrib.get('message')
                        stack_elem = fail.find('StackTrace')
                        if stack_elem is not None:
                            stack = stack_elem.text
                            stack_lower = stack.lower()
                            if "nullpointerexception" in stack_lower:
                                rca_category = "Null Pointer Exception"
                                rca_recommendation = "Check for uninitialized variables or missing null checks in the OS framework layer."
                            elif "permissiondenied" in stack_lower or "securityexception" in stack_lower:
                                rca_category = "Permission/Security Exception"
                                rca_recommendation = "Verify SELinux policies (dmesg | grep denied) or Android Manifest hardware permissions."
                            elif "timeout" in stack_lower or "deadlinedelayed" in stack_lower:
                                rca_category = "Timeout"
                                rca_recommendation = "Device hung or ADB disconnected. Check logcat for ANRs, tombstone crashes, or system server watchdog."
                            elif "assertionerror" in stack_lower or "expectation failed" in stack_lower:
                                rca_category = "Assertion Failed"
                                rca_recommendation = "Test logically failed. The OS behavior deviated from CTS specifications."
                            elif "aaptparser" in stack_lower or "targetsetuperror" in stack_lower:
                                rca_category = "Environment Setup Error"
                                rca_recommendation = "TradeFed failed to push or parse the APK. Check if SDK build-tools are in PATH and device supports the ABI."
                            else:
                                rca_category = "Native Crash / Unhandled Error"
                                rca_recommendation = "Review logcat stack trace directly. Look for tombstone logs in /data/tombstones."
                    
                    tc_res = TestCaseResult(
                        class_name=tc.attrib.get('name', ''),
                        test_name=t.attrib.get('name', ''),
                        result=status,
                        message=msg,
                        stack_trace=stack,
                        rca_category=rca_category,
                        rca_recommendation=rca_recommendation
                    )"""

content = re.sub(r'                    stack = None.*?stack_trace=stack\n                    \)', new_parse_logic, content, flags=re.DOTALL)

with open('/mnt/xTS_Agent/xts_agent/results/result_parser.py', 'w') as f:
    f.write(content)

