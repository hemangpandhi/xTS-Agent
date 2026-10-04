from __future__ import annotations
"""
from __future__ import annotations
Parser for TradeFed test_result.xml.
"""
from dataclasses import dataclass, field
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

@dataclass
class TestCaseResult:
    class_name: str
    test_name: str
    result: str
    message: Optional[str] = None
    stack_trace: Optional[str] = None
    duration: Optional[int] = None
    rca_category: Optional[str] = None
    rca_recommendation: Optional[str] = None

@dataclass
class ModuleResult:
    name: str
    done: bool
    pass_count: int
    fail_count: int
    runtime: int
    test_cases: List[TestCaseResult] = field(default_factory=list)

@dataclass
class TestResults:
    suite_name: str
    device_info: Dict[str, str]
    start_time: str
    end_time: str
    modules: List[ModuleResult] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=lambda: {"pass": 0, "fail": 0, "skip": 0, "error": 0})

class ResultParser:
    """Parses TradeFed XML results."""
    
    def parse_xml(self, xml_path: str | Path) -> TestResults:
        """Parses a test_result.xml file."""
        xml_path = Path(xml_path)
        if not xml_path.exists():
            raise FileNotFoundError(f"Result file not found: {xml_path}")
            
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.error(f"Failed to parse XML {xml_path}: {e}")
            raise
            
        results = TestResults(
            suite_name=root.attrib.get('suite_name', 'Unknown'),
            device_info={},
            start_time=root.attrib.get('start_display', ''),
            end_time=root.attrib.get('end_display', '')
        )
        
        build_info = root.find('Build')
        if build_info is not None:
            for k, v in build_info.attrib.items():
                results.device_info[k] = v
                
        summary = root.find('Summary')
        if summary is not None:
            results.summary['pass'] = int(summary.attrib.get('pass', 0))
            results.summary['fail'] = int(summary.attrib.get('failed', 0))
            results.summary['skip'] = int(summary.attrib.get('modules_done', 0)) # approx
            
        for mod in root.findall('Module'):
            mod_res = ModuleResult(
                name=mod.attrib.get('name', 'Unknown'),
                done=mod.attrib.get('done', 'true').lower() == 'true',
                pass_count=int(mod.attrib.get('pass', 0)),
                fail_count=int(mod.attrib.get('fail', 0)),
                runtime=int(mod.attrib.get('runtime', 0))
            )
            for tc in mod.findall('.//TestCase'):
                for t in tc.findall('Test'):
                    status = t.attrib.get('result', 'PASS')
                    fail = t.find('Failure')
                    msg = None
                    stack = None
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
                    )
                    mod_res.test_cases.append(tc_res)
            results.modules.append(mod_res)
            
        return results

    def get_failed_tests(self, results: TestResults) -> List[TestCaseResult]:
        """Returns all failed test cases."""
        failed = []
        for mod in results.modules:
            for tc in mod.test_cases:
                if tc.result in ('FAIL', 'ERROR'):
                    failed.append(tc)
        return failed
        
    def get_summary(self, results: TestResults) -> Dict[str, float]:
        """Returns summary with counts and percentages."""
        total = sum(results.summary.values())
        res = dict(results.summary)
        res['total'] = total
        if total > 0:
            res['pass_rate'] = (results.summary['pass'] / total) * 100
        else:
            res['pass_rate'] = 0.0
        return res
