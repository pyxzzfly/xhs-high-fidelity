import logging
import json
import sys
import os
from typing import Any, Dict
from datetime import datetime
import uuid

# Configure structlog or standard logging
# For simplicity, using standard logging with JSON formatting

class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }
        
        if hasattr(record, "trace_id"):
            log_record["trace_id"] = record.trace_id
            
        if hasattr(record, "props"):
            log_record.update(record.props)

        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_record)

def setup_logger(name: str = "xhs-high-fidelity"):
    logger = logging.getLogger(name)
    level_name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    
    if not logger.handlers:
        # Use stderr to align with uvicorn default logging stream in many setups.
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    
    return logger

logger = setup_logger()

class TaskLogger:
    def __init__(self, trace_id: str = None):
        self.trace_id = trace_id or str(uuid.uuid4())
        self.logger = logger

    def info(self, message: str, **kwargs):
        extra = {"trace_id": self.trace_id, "props": kwargs}
        self.logger.info(message, extra=extra)
        if (os.getenv("XHS_LOG_DUP_TO_UVICORN") or "1").strip().lower() not in {"0", "false", "no", "off"}:
            logging.getLogger("uvicorn.error").info(message, extra=extra)

    def error(self, message: str, **kwargs):
        extra = {"trace_id": self.trace_id, "props": kwargs}
        self.logger.error(message, extra=extra)
        if (os.getenv("XHS_LOG_DUP_TO_UVICORN") or "1").strip().lower() not in {"0", "false", "no", "off"}:
            logging.getLogger("uvicorn.error").error(message, extra=extra)
