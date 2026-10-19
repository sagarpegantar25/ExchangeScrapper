"""Logging setup: timestamped, daily-rotated log files plus console output."""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-16s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(logs_dir: Path, level: str = "INFO", retention_days: int = 90) -> logging.Logger:
    """Configure root logging once. Returns the application logger."""
    global _configured
    root = logging.getLogger()
    if _configured:
        return logging.getLogger("exch")

    logs_dir.mkdir(parents=True, exist_ok=True)
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # Main log: rotates at midnight, keeps `retention_days` files.
    main = TimedRotatingFileHandler(
        logs_dir / "exch.log", when="midnight", backupCount=retention_days, encoding="utf-8"
    )
    main.setLevel(getattr(logging, level, logging.INFO))
    main.setFormatter(fmt)
    root.addHandler(main)

    # Errors only: quick place to look when something failed.
    err = TimedRotatingFileHandler(
        logs_dir / "errors.log", when="midnight", backupCount=retention_days, encoding="utf-8"
    )
    err.setLevel(logging.WARNING)
    err.setFormatter(fmt)
    root.addHandler(err)

    # Windows consoles default to cp1252, which cannot print characters like "→" or "•"
    # used in log messages; switch stdout to UTF-8 so logging never crashes on them.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level, logging.INFO))
    console.setFormatter(fmt)
    root.addHandler(console)

    # Keep third-party libraries quiet unless debugging.
    for noisy in ("urllib3", "httpx", "httpx2", "httpcore", "anthropic", "pypdf", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    return logging.getLogger("exch")
