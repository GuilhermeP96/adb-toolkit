"""
log_setup.py - Logging configuration for ADB Toolkit.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path


def setup_logging(
    log_dir: Path = None,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """Configure application-wide logging."""
    log_dir = log_dir or Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"adb_toolkit_{datetime.now():%Y%m%d}.log"

    root = logging.getLogger("adb_toolkit")
    root.setLevel(level)

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    # Console handler
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(level)
        ch.setFormatter(logging.Formatter(
            "[%(levelname)-7s] %(message)s"
        ))
        root.addHandler(ch)

    root.info("Logging initialized → %s", log_file)
    return root
