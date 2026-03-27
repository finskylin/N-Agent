"""
Tracing Utility
用于请求链路追踪，使用 contextvars 管理 trace_id
"""
from contextvars import ContextVar
import uuid
import sys
from loguru import logger

# Context Variable to store the current Trace ID
# Default is "N/A" for contexts initiated outside of API requests (e.g. background tasks, startup)
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="N/A")

def generate_trace_id() -> str:
    """Generate a short unique Trace ID"""
    return str(uuid.uuid4())[:8]

def get_trace_id() -> str:
    """Get the current Trace ID"""
    return trace_id_var.get()

def set_trace_id(trace_id: str):
    """Set the current Trace ID"""
    trace_id_var.set(trace_id)

def filter_record(record):
    """Loguru filter to inject trace_id into the record['extra']"""
    record["extra"]["trace_id"] = get_trace_id()
    return True

def configure_logging():
    """Configure Loguru to include trace_id in logs"""
    logger.remove()
    
    # Define format with trace_id
    # Format: Time | Level | [TraceID] | Module:Line | Message
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>[{extra[trace_id]}]</cyan> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )
    
    logger.add(
        sys.stderr, 
        format=log_format, 
        filter=filter_record, 
        level="INFO"
    )
    
    logger.info("Logging configured with Tracing support")

# Auto-configure on import? 
# Better to let the app call it on startup, but for simplicity in this project context:
configure_logging()
