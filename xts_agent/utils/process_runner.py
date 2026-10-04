"""Subprocess execution wrapper."""

from __future__ import annotations
import subprocess
import asyncio
import time
from dataclasses import dataclass
from typing import Optional, List, Tuple
import logging

logger = logging.getLogger(__name__)

@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    duration: float

class ProcessRunner:
    """Wrapper for running subprocesses with timeouts and real-time streaming."""
    
    @staticmethod
    def run_command(cmd: List[str], timeout: Optional[int] = None) -> ProcessResult:
        """Runs a command synchronously."""
        start_time = time.time()
        logger.debug(f"Running command: {' '.join(cmd)}")
        
        try:
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            duration = time.time() - start_time
            return ProcessResult(
                returncode=process.returncode,
                stdout=process.stdout,
                stderr=process.stderr,
                duration=duration
            )
        except subprocess.TimeoutExpired as e:
            duration = time.time() - start_time
            logger.error(f"Command timed out after {timeout}s: {' '.join(cmd)}")
            return ProcessResult(
                returncode=-1,
                stdout=e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or ""),
                duration=duration
            )

    @staticmethod
    async def run_command_async(cmd: List[str], timeout: Optional[int] = None) -> ProcessResult:
        """Runs a command asynchronously."""
        start_time = time.time()
        logger.debug(f"Running async command: {' '.join(cmd)}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            duration = time.time() - start_time
            return ProcessResult(
                returncode=process.returncode or 0,
                stdout=stdout.decode(),
                stderr=stderr.decode(),
                duration=duration
            )
        except asyncio.TimeoutError:
            process.kill()
            duration = time.time() - start_time
            logger.error(f"Async command timed out after {timeout}s: {' '.join(cmd)}")
            return ProcessResult(
                returncode=-1,
                stdout="",
                stderr="Timeout",
                duration=duration
            )
