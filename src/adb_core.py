"""
adb_core.py - Core ADB interface module.
Handles all direct communication with ADB binary:
  - Finding / downloading ADB platform-tools
  - Executing ADB commands (shell, push, pull, backup, restore, etc.)
  - Device enumeration, state monitoring, and event callbacks
"""

import subprocess
import shutil
import os
import re
import time
import logging
import zipfile
import threading
from pathlib import Path
from typing import Optional, List, Dict, Callable, Tuple

log = logging.getLogger("adb_toolkit.core")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PLATFORM_TOOLS_URL = {
    "win32": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    "darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
}

ADB_EXE = "adb.exe" if os.name == "nt" else "adb"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _find_adb_in_path() -> Optional[str]:
    """Return the full path to adb if it is already on PATH."""
    return shutil.which("adb")


def _find_adb_in_local(base_dir: Path) -> Optional[str]:
    """Look for adb inside a local platform-tools folder."""
    local = base_dir / "platform-tools" / ADB_EXE
    if local.is_file():
        return str(local)
    return None


# ---------------------------------------------------------------------------
# ADB Device Info
# ---------------------------------------------------------------------------
class DeviceInfo:
    """Represents a connected Android device."""

    def __init__(self, serial: str, state: str = "device"):
        self.serial = serial
        self.state = state  # device | unauthorized | offline | recovery | sideload
        self.model: str = ""
        self.manufacturer: str = ""
        self.android_version: str = ""
        self.sdk_version: str = ""
        self.product: str = ""
        self.storage_total: int = 0
        self.storage_free: int = 0
        self.battery_level: int = -1

    def __repr__(self):
        return (
            f"<Device {self.serial} state={self.state} "
            f"model={self.model} android={self.android_version}>"
        )

    def friendly_name(self) -> str:
        if self.manufacturer and self.model:
            return f"{self.manufacturer} {self.model}"
        if self.model:
            return self.model
        return self.serial

    def storage_summary(self) -> str:
        """Return human-readable storage summary, e.g. '12.3 GB / 64 GB'."""
        if self.storage_total <= 0:
            return ""
        return f"{_fmt_bytes(self.storage_free)} livre / {_fmt_bytes(self.storage_total)} total"

    def short_label(self) -> str:
        """Label for dropdown menus: name + storage."""
        name = self.friendly_name()
        stor = self.storage_summary()
        if stor:
            return f"{name}  [{stor}]"
        return name


def _fmt_bytes(size: int) -> str:
    """Quick byte formatter for DeviceInfo (avoids circular import)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} PB"


# ---------------------------------------------------------------------------
# ADB Core
# ---------------------------------------------------------------------------
class ADBCore:
    """Low-level ADB wrapper."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent
        self.adb_path: Optional[str] = None
        self._lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_running = False
        self._device_callbacks: List[Callable] = []
        self._known_devices: Dict[str, DeviceInfo] = {}

    # ------------------------------------------------------------------
    # ADB binary management
    # ------------------------------------------------------------------
    def find_adb(self) -> Optional[str]:
        """Locate the adb binary (local then PATH)."""
        adb = _find_adb_in_local(self.base_dir) or _find_adb_in_path()
        if adb:
            self.adb_path = adb
            log.info("ADB found at %s", adb)
        return adb

    def download_platform_tools(self, progress_cb: Optional[Callable[[int, int], None]] = None) -> str:
        """Download Google platform-tools and extract to base_dir/platform-tools."""
        import sys
        import urllib.request

        key = sys.platform
        if key not in PLATFORM_TOOLS_URL:
            key = "linux"
        url = PLATFORM_TOOLS_URL[key]
        dest_zip = self.base_dir / "platform-tools.zip"

        log.info("Downloading platform-tools from %s", url)

        def _report(block_num, block_size, total_size):
            if progress_cb:
                progress_cb(block_num * block_size, total_size)

        urllib.request.urlretrieve(url, str(dest_zip), reporthook=_report)

        log.info("Extracting platform-tools …")
        with zipfile.ZipFile(dest_zip, "r") as zf:
            zf.extractall(str(self.base_dir))
        dest_zip.unlink(missing_ok=True)

        adb = _find_adb_in_local(self.base_dir)
        if not adb:
            raise RuntimeError("ADB not found after extraction")
        self.adb_path = adb
        log.info("ADB installed at %s", adb)
        return adb

    def ensure_adb(self, progress_cb=None) -> str:
        """Make sure adb is available — download if needed."""
        if self.find_adb():
            return self.adb_path
        return self.download_platform_tools(progress_cb)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------
    def run(
        self,
        args: List[str],
        serial: Optional[str] = None,
        timeout: int = 120,
        capture: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run an ADB command and return the CompletedProcess."""
        if not self.adb_path:
            raise RuntimeError("ADB binary not configured. Call ensure_adb() first.")

        cmd = [self.adb_path]
        if serial:
            cmd += ["-s", serial]
        cmd += args

        log.debug("Running: %s", " ".join(cmd))
        with self._lock:
            result = subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        # Guard: ensure stdout/stderr are never None
        if result.stdout is None:
            result.stdout = ""
        if result.stderr is None:
            result.stderr = ""
        if result.returncode != 0:
            log.warning("ADB returned %d: %s", result.returncode, result.stderr.strip())
        return result

    def run_shell(self, shell_cmd: str, serial: Optional[str] = None, timeout: int = 60) -> str:
        """Run `adb shell <cmd>` and return stdout."""
        try:
            r = self.run(["shell", shell_cmd], serial=serial, timeout=timeout)
            return (r.stdout or "").strip()
        except subprocess.TimeoutExpired:
            log.warning("Shell command timed out after %ds: %s", timeout, shell_cmd[:120])
            return ""
        except Exception as exc:
            log.warning("Shell command error: %s", exc)
            return ""

    def start_server(self):
        self.run(["start-server"])

    def kill_server(self):
        self.run(["kill-server"])

    # ------------------------------------------------------------------
    # Device enumeration
    # ------------------------------------------------------------------
    def list_devices(self) -> List[DeviceInfo]:
        """Return a list of connected devices (adb devices -l)."""
        result = self.run(["devices", "-l"])
        devices: List[DeviceInfo] = []
        for line in result.stdout.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial = parts[0]
            state = parts[1]
            dev = DeviceInfo(serial, state)
            # parse key:value pairs
            for p in parts[2:]:
                if ":" in p:
                    k, v = p.split(":", 1)
                    if k == "model":
                        dev.model = v
                    elif k == "product":
                        dev.product = v
                    elif k == "device":
                        pass  # codename
            devices.append(dev)
        return devices

    def get_device_details(self, serial: str) -> DeviceInfo:
        """Populate detailed info for a device."""
        dev = DeviceInfo(serial)
        try:
            dev.model = self.run_shell("getprop ro.product.model", serial)
            dev.manufacturer = self.run_shell("getprop ro.product.manufacturer", serial)
            dev.android_version = self.run_shell("getprop ro.build.version.release", serial)
            dev.sdk_version = self.run_shell("getprop ro.build.version.sdk", serial)
            dev.product = self.run_shell("getprop ro.product.name", serial)

            # Battery
            batt = self.run_shell("dumpsys battery", serial)
            m = re.search(r"level:\s*(\d+)", batt)
            if m:
                dev.battery_level = int(m.group(1))

            # Storage
            df_out = self.run_shell("df /data", serial)
            lines = df_out.splitlines()
            if len(lines) >= 2:
                cols = lines[1].split()
                if len(cols) >= 4:
                    dev.storage_total = int(cols[1]) * 1024  # KB -> bytes approx
                    dev.storage_free = int(cols[3]) * 1024
        except Exception as exc:
            log.warning("Failed to get details for %s: %s", serial, exc)
        return dev

    # ------------------------------------------------------------------
    # Device monitoring
    # ------------------------------------------------------------------
    def register_device_callback(self, cb: Callable[[str, Optional[DeviceInfo]], None]):
        """Register callback(event, device). Events: connected, disconnected, changed."""
        self._device_callbacks.append(cb)

    def start_device_monitor(self, interval: float = 2.0):
        """Start background thread that polls for device changes."""
        if self._monitor_running:
            return
        self._monitor_running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, args=(interval,), daemon=True
        )
        self._monitor_thread.start()

    def stop_device_monitor(self):
        self._monitor_running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)

    def _monitor_loop(self, interval: float):
        while self._monitor_running:
            try:
                current = {d.serial: d for d in self.list_devices()}
                # New devices
                for s, d in current.items():
                    if s not in self._known_devices:
                        self._fire_event("connected", d)
                    elif self._known_devices[s].state != d.state:
                        self._fire_event("changed", d)
                # Removed devices
                for s in list(self._known_devices):
                    if s not in current:
                        self._fire_event("disconnected", self._known_devices[s])
                self._known_devices = current
            except Exception as exc:
                log.debug("Monitor error: %s", exc)
            time.sleep(interval)

    def _fire_event(self, event: str, device: DeviceInfo):
        for cb in self._device_callbacks:
            try:
                cb(event, device)
            except Exception:
                log.exception("Callback error")

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------
    def push(self, local: str, remote: str, serial: Optional[str] = None) -> bool:
        r = self.run(["push", local, remote], serial=serial, timeout=600)
        return r.returncode == 0

    def pull(self, remote: str, local: str, serial: Optional[str] = None) -> bool:
        r = self.run(["pull", remote, local], serial=serial, timeout=600)
        return r.returncode == 0

    def list_dir(self, remote_path: str, serial: Optional[str] = None) -> List[str]:
        out = self.run_shell(f"ls -1 {remote_path}", serial)
        return [l for l in out.splitlines() if l.strip()]

    # ------------------------------------------------------------------
    # Package management
    # ------------------------------------------------------------------
    def list_packages(self, serial: Optional[str] = None, third_party: bool = True) -> List[str]:
        flag = "-3" if third_party else ""
        out = self.run_shell(f"pm list packages {flag}", serial)
        return [l.replace("package:", "") for l in out.splitlines() if l.startswith("package:")]

    def get_apk_path(self, package: str, serial: Optional[str] = None) -> Optional[str]:
        """Get the primary (base) APK path for a package.

        For split APKs, returns only the base.apk path.
        Use get_apk_paths() to get all split APK paths.
        """
        out = self.run_shell(f"pm path {package}", serial)
        if not out:
            return None
        # pm path returns one or more "package:<path>" lines
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                path = line[8:]  # len("package:") == 8
                # Prefer base.apk for split APKs
                if "base.apk" in path or "split" not in path:
                    return path
        # Fallback: return first package line
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                return line[8:]
        return None

    def get_apk_paths(self, package: str, serial: Optional[str] = None) -> List[str]:
        """Get ALL APK paths for a package (base + splits)."""
        out = self.run_shell(f"pm path {package}", serial)
        if not out:
            return []
        paths = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                paths.append(line[8:])
        return paths

    def install_apk(self, apk_path: str, serial: Optional[str] = None) -> bool:
        r = self.run(["install", "-r", apk_path], serial=serial, timeout=300)
        return r.returncode == 0

    def install_split_apks(self, apk_paths: List[str], serial: Optional[str] = None) -> bool:
        """Install multiple APKs (split APK bundles) via `adb install-multiple`."""
        if not apk_paths:
            return False
        args = ["install-multiple", "-r"] + apk_paths
        r = self.run(args, serial=serial, timeout=600)
        return r.returncode == 0

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------
    def get_app_cache_sizes(self, serial: Optional[str] = None, third_party: bool = True) -> List[Dict]:
        """Get cache size info for installed packages.

        Returns list of dicts: {package, cache_bytes, label}.
        Uses `dumpsys package` and `du` to estimate cache sizes.
        """
        packages = self.list_packages(serial, third_party=third_party)
        results: List[Dict] = []

        for pkg in packages:
            try:
                # Query storage stats via dumpsys
                out = self.run_shell(
                    f'dumpsys diskstats | grep -A2 "{pkg}" 2>/dev/null || '
                    f'du -s /data/data/{pkg}/cache 2>/dev/null || '
                    f'du -s /data/user/0/{pkg}/cache 2>/dev/null || echo "0"',
                    serial, timeout=5,
                )
                cache_bytes = 0
                for line in out.splitlines():
                    line = line.strip()
                    if line and line[0].isdigit():
                        try:
                            cache_bytes = int(line.split()[0]) * 1024  # du returns KB
                        except (ValueError, IndexError):
                            pass
                        break

                results.append({
                    "package": pkg,
                    "cache_bytes": cache_bytes,
                })
            except Exception:
                results.append({"package": pkg, "cache_bytes": 0})

        return results

    def get_total_cache_size(self, serial: Optional[str] = None) -> int:
        """Get estimated total cache size in bytes using dumpsys diskstats."""
        out = self.run_shell("dumpsys diskstats", serial, timeout=30)
        total = 0
        # Parse "Cache-bytes: NNN" or similar
        for line in out.splitlines():
            if "cache" in line.lower() and any(c.isdigit() for c in line):
                import re as _re
                m = _re.search(r'(\d+)', line)
                if m:
                    val = int(m.group(1))
                    if val > total:
                        total = val
        return total

    def clear_app_cache(self, package: str, serial: Optional[str] = None) -> bool:
        """Clear cache for a single app.

        Tries multiple strategies:
        1. `pm clear --cache-only` (Android 12+)
        2. `rm -rf` on accessible cache dirs
        """
        # Strategy 1: pm clear --cache-only (Android 12+)
        result = self.run(
            ["shell", "pm", "clear", "--cache-only", package],
            serial=serial, timeout=15,
        )
        if result.returncode == 0 and "Success" in result.stdout:
            return True

        # Strategy 2: rm -rf cache dirs (may need elevated permissions)
        for base in ("/data/data", "/data/user/0"):
            self.run_shell(
                f'rm -rf {base}/{package}/cache/* {base}/{package}/code_cache/* 2>/dev/null',
                serial, timeout=10,
            )

        return True

    def clear_all_cache(self, serial: Optional[str] = None) -> bool:
        """Clear cache for all apps using pm trim-caches."""
        # trim-caches accepts a very large value to clear everything
        result = self.run(
            ["shell", "pm", "trim-caches", "999999999999"],
            serial=serial, timeout=60,
        )
        return result.returncode == 0

    # ------------------------------------------------------------------
    # Reboot
    # ------------------------------------------------------------------
    def reboot(self, mode: str = "", serial: Optional[str] = None):
        """Reboot device. mode: '' (normal), 'recovery', 'bootloader'."""
        args = ["reboot"]
        if mode:
            args.append(mode)
        self.run(args, serial=serial)
