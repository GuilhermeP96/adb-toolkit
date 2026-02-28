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


# ---------------------------------------------------------------------------
# ADB PATH management
# ---------------------------------------------------------------------------
def is_adb_in_path() -> bool:
    """Check if ADB is already accessible from system PATH."""
    try:
        if is_windows():
            r = subprocess.run(
                ["where", "adb"], capture_output=True, text=True, timeout=5,
            )
        else:
            r = subprocess.run(
                ["which", "adb"], capture_output=True, text=True, timeout=5,
            )
        return r.returncode == 0 and r.stdout.strip() != ""
    except Exception:
        return False


def get_adb_dir(base_dir: Path) -> Optional[Path]:
    """Return the platform-tools directory if it exists."""
    pt = base_dir / "platform-tools"
    if pt.is_dir():
        return pt
    return None


def add_adb_to_path(adb_dir: Path) -> tuple[bool, str]:
    """Add the ADB directory to the system PATH permanently.

    On Windows: modifies the **system** PATH via the registry (requires admin).
    On Linux/macOS: appends to ~/.bashrc and ~/.profile.

    Returns (success, message).
    """
    adb_dir_str = str(adb_dir.resolve())

    if is_windows():
        return _add_to_path_windows(adb_dir_str)
    else:
        return _add_to_path_unix(adb_dir_str)


def remove_adb_from_path(adb_dir: Path) -> tuple[bool, str]:
    """Remove the ADB directory from the system PATH.

    Returns (success, message).
    """
    adb_dir_str = str(adb_dir.resolve())

    if is_windows():
        return _remove_from_path_windows(adb_dir_str)
    else:
        return _remove_from_path_unix(adb_dir_str)


def _add_to_path_windows(dir_path: str) -> tuple[bool, str]:
    """Add to Windows system PATH via registry + broadcast."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            0, winreg.KEY_READ | winreg.KEY_WRITE,
        )
        try:
            current, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current = ""

        # Check if already present
        entries = [e.strip() for e in current.split(";") if e.strip()]
        norm = os.path.normcase(os.path.normpath(dir_path))
        for e in entries:
            if os.path.normcase(os.path.normpath(e)) == norm:
                winreg.CloseKey(key)
                return True, "ADB já está no PATH do sistema."

        # Append
        new_path = current.rstrip(";") + ";" + dir_path
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
        winreg.CloseKey(key)

        # Broadcast change to all windows
        try:
            import ctypes
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            result = ctypes.c_long(0)
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0,
                "Environment", SMTO_ABORTIFHUNG, 5000,
                ctypes.byref(result),
            )
        except Exception:
            pass

        log.info("Added to system PATH: %s", dir_path)
        return True, (
            f"ADB adicionado ao PATH do sistema.\n"
            f"Diretório: {dir_path}\n\n"
            f"Abra um novo terminal e digite 'adb version' para verificar."
        )
    except PermissionError:
        return False, (
            "Permissão negada. Execute o app como Administrador\n"
            "para modificar o PATH do sistema."
        )
    except Exception as exc:
        log.error("Failed to add to PATH: %s", exc)
        return False, f"Erro ao modificar PATH: {exc}"


def _remove_from_path_windows(dir_path: str) -> tuple[bool, str]:
    """Remove from Windows system PATH via registry."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            0, winreg.KEY_READ | winreg.KEY_WRITE,
        )
        try:
            current, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            winreg.CloseKey(key)
            return True, "PATH vazio, nada a remover."

        norm = os.path.normcase(os.path.normpath(dir_path))
        entries = [e.strip() for e in current.split(";") if e.strip()]
        filtered = [e for e in entries
                    if os.path.normcase(os.path.normpath(e)) != norm]

        if len(filtered) == len(entries):
            winreg.CloseKey(key)
            return True, "ADB não estava no PATH do sistema."

        new_path = ";".join(filtered)
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
        winreg.CloseKey(key)

        # Broadcast
        try:
            import ctypes
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            result = ctypes.c_long(0)
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0,
                "Environment", SMTO_ABORTIFHUNG, 5000,
                ctypes.byref(result),
            )
        except Exception:
            pass

        log.info("Removed from system PATH: %s", dir_path)
        return True, "ADB removido do PATH do sistema."
    except PermissionError:
        return False, (
            "Permissão negada. Execute o app como Administrador\n"
            "para modificar o PATH do sistema."
        )
    except Exception as exc:
        return False, f"Erro ao modificar PATH: {exc}"


def _add_to_path_unix(dir_path: str) -> tuple[bool, str]:
    """Add to PATH via shell profile files on Linux/macOS."""
    line = f'\nexport PATH="$PATH:{dir_path}"  # ADB Toolkit\n'
    home = Path.home()
    targets = []

    for rc in [".bashrc", ".profile", ".zshrc"]:
        rc_path = home / rc
        if rc_path.exists():
            targets.append(rc_path)

    if not targets:
        targets = [home / ".bashrc"]

    already = False
    for rc_path in targets:
        try:
            content = rc_path.read_text(encoding="utf-8")
            if dir_path in content:
                already = True
                continue
            with open(rc_path, "a", encoding="utf-8") as f:
                f.write(line)
            log.info("Added ADB to %s", rc_path)
        except Exception as exc:
            log.warning("Failed to update %s: %s", rc_path, exc)

    if already and len(targets) == 1:
        return True, "ADB já está no PATH."

    files = ", ".join(t.name for t in targets)
    return True, (
        f"ADB adicionado ao PATH em: {files}\n"
        f"Diretório: {dir_path}\n\n"
        f"Execute 'source ~/{targets[0].name}' ou abra um novo terminal."
    )


def _remove_from_path_unix(dir_path: str) -> tuple[bool, str]:
    """Remove ADB Toolkit lines from shell profile files."""
    home = Path.home()
    removed = False
    for rc in [".bashrc", ".profile", ".zshrc"]:
        rc_path = home / rc
        if not rc_path.exists():
            continue
        try:
            lines = rc_path.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = [l for l in lines if dir_path not in l]
            if len(new_lines) < len(lines):
                rc_path.write_text("".join(new_lines), encoding="utf-8")
                removed = True
        except Exception:
            pass

    if removed:
        return True, "ADB removido do PATH. Abra um novo terminal."
    return True, "ADB não estava no PATH."
