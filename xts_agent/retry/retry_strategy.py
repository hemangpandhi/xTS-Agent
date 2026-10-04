"""Strategy implementations for retries."""
from __future__ import annotations
from enum import Enum, auto
from dataclasses import dataclass

class RetryStrategy(Enum):
    RETRY_ANY_FAILURE = auto()
    RETRY_TEST_FAILURE_ONLY = auto()
    NO_RETRY = auto()

class IsolationGrade(Enum):
    NO_ISOLATION = auto()
    REBOOT_ISOLATED = auto()
    FULLY_ISOLATED = auto()

@dataclass
class IntraModuleRetryConfig:
    max_testcase_run_count: int
    retry_strategy: RetryStrategy

@dataclass
class SuiteRetryConfig:
    max_retries: int
    retry_type: str
    isolation_grade: IsolationGrade

@dataclass
class AgentRetryConfig:
    enabled: bool
    smart_retry: bool
