"""
utils.py - Utility helpers for ADB Toolkit.
"""

import os
import sys
import ctypes
import platform
import subprocess
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("adb_toolkit.utils")


def is_windows() -> bool:
    return os.name == "nt"


def is_admin() -> bool:
    """Check if current process has admin/root privileges."""
    if is_windows():
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    return os.geteuid() == 0


def format_bytes(size: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def format_duration(seconds: float) -> str:
    """Human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def open_folder(path: str):
    """Open a folder in the system file manager."""
    if is_windows():
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path])
    else:
        subprocess.run(["xdg-open", path])


def get_system_info() -> dict:
    """Return basic system information."""
    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
    }


def ensure_directory(path: Path) -> Path:
    """Create directory if it doesn't exist."""
    path.mkdir(parents=True, exist_ok=True)
    return path
