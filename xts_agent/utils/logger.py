"""Structured JSON logging utility."""

from __future__ import annotations
import logging
import json
from datetime import datetime
from rich.logging import RichHandler

class JSONFormatter(logging.Formatter):
    """Formatter that outputs JSON strings."""
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name
        }
        if hasattr(record, "suite"):
            log_data["suite"] = record.suite
        if hasattr(record, "device"):
            log_data["device"] = record.device
        if hasattr(record, "phase"):
            log_data["phase"] = record.phase
            
        return json.dumps(log_data)

def setup_logger(name: str, log_file: str = "xts_agent.log", level: int = logging.INFO) -> logging.Logger:
    """Sets up a logger with rich console and JSON file handlers."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    if not logger.handlers:
        # Rich console handler
        console_handler = RichHandler(rich_tracebacks=True)
        console_handler.setLevel(level)
        logger.addHandler(console_handler)
        
        # JSON file handler
        from pathlib import Path
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(JSONFormatter())
        logger.addHandler(file_handler)
        
    return logger
