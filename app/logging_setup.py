"""Logging configuration based on loguru.

Application logs use ``from loguru import logger as log`` directly.  Uvicorn,
httpx and other stdlib-logging libraries are intercepted and forwarded to the
same loguru sinks.
"""

from __future__ import annotations

import logging
import sys

from loguru import logger

from .config import Settings

_CONFIGURED = False
_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} - {message}"
)


class _InterceptHandler(logging.Handler):
    """Forward stdlib logging records to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except (ValueError, TypeError):
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def configure_logging(settings: Settings) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    # Chinese log lines must not crash on consoles with a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass

    level = settings.log_level.upper()
    if level not in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}:
        level = "INFO"

    logger.remove()
    logger.add(
        sys.stdout,
        level=level,
        format=_FORMAT,
        colorize=True,
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )

    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            settings.logs_dir / "receiver.log",
            level=level,
            format=_FORMAT,
            rotation="5 MB",
            retention=5,
            encoding="utf-8",
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )
    except OSError:
        logger.warning("无法创建日志文件，仅输出到控制台")

    # Route uvicorn / httpx / other stdlib logging through loguru.
    root = logging.getLogger()
    root.handlers = [_InterceptHandler()]
    root.setLevel(logging.DEBUG)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        std_logger = logging.getLogger(name)
        std_logger.handlers = [_InterceptHandler()]
        std_logger.propagate = False

    # httpx logs every request at INFO which is noisy for a long-running service.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _CONFIGURED = True
