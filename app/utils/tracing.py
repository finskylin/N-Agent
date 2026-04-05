"""
Tracing Utility
用于请求链路追踪，使用 contextvars 管理 trace_id
"""
from contextvars import ContextVar
import uuid
import sys
from pathlib import Path
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
    """
    配置 Loguru 日志：
    1. stderr 实时输出（彩色，供 docker logs 查看）
    2. 文件滚动输出（app/logs/app.log，按天轮转，保留 30 天）
       格式不含 ANSI 颜色，便于 grep/tail
    """
    logger.remove()

    _fmt = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <8} | "
        "[{extra[trace_id]}] | "
        "{name}:{line} - "
        "{message}"
    )
    _fmt_color = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>[{extra[trace_id]}]</cyan> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    # 1. stderr（docker logs 可见，彩色）
    logger.add(
        sys.stderr,
        format=_fmt_color,
        filter=filter_record,
        level="INFO",
        colorize=True,
    )

    # 2. 文件（按天轮转，保留 30 天，压缩旧文件）
    # 使用 __file__ 推算绝对路径，避免相对路径随 CWD 变化
    log_dir = Path(__file__).parent.parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_dir / "app_{time:YYYY-MM-DD}.log"),
        format=_fmt,
        filter=filter_record,
        level="DEBUG",
        rotation="00:00",       # 每天午夜轮转
        retention="1 days",     # 保留 1 天
        compression="gz",       # 旧文件压缩
        encoding="utf-8",
        colorize=False,
        enqueue=True,           # 异步写，不阻塞主线程
    )

    logger.info("Logging configured: stderr + file(app/logs/app_YYYY-MM-DD.log)")


configure_logging()
