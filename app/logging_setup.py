"""Logging configuration.

Logs go to stdout and to ``data/logs/receiver.log`` (rotating).
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import Settings

_CONFIGURED = False
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


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

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.logs_dir / "receiver.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        logging.getLogger(__name__).warning("无法创建日志文件，仅输出到控制台", exc_info=True)

    # httpx logs every request at INFO which is noisy for a long-running service.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = True
