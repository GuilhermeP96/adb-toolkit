"""
ios_manager.py — iOS device management via libimobiledevice CLI tools.

Provides two connection modes:
 1. **Agent mode**: Connect to the iOS Agent app via HTTP/TCP (same as Android)
 2. **libimobiledevice mode**: Direct USB/WiFi access via idevice* tools
    (ideviceinfo, idevicebackup2, ifuse, ideviceinstaller, etc.)

The GUI uses these as complementary layers:
 - Agent mode: Contacts, photos, files (app sandbox), D2D pairing
 - libimobiledevice mode: Full backup, sideload .ipa, filesystem (AFC),
   device info, screenshots, syslog — all WITHOUT an agent app

Requirements (libimobiledevice):
 - Windows: choco install libimobiledevice / imobiledevice-net
 - macOS: brew install libimobiledevice
 - Linux: apt install libimobiledevice-utils ifuse
 - Termux: pkg install libimobiledevice libusbmuxd
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────
#  Data classes
# ─────────────────────────────────────────────────────────────────────


@dataclass
class IOSDevice:
    """Represents a connected iOS device."""
    udid: str
    name: str = ""
    model: str = ""
    ios_version: str = ""
    serial: str = ""
    connection_type: str = ""   # "USB" or "WiFi"
    is_paired: bool = False


@dataclass
class IOSBackupProgress:
    """Progress info during a backup/restore operation."""
    phase: str = ""         # "preparing", "backing_up", "finishing"
    percent: float = 0.0
    message: str = ""
    files_done: int = 0
    files_total: int = 0


@dataclass
class IOSAppInfo:
    """Info about an installed iOS app (via ideviceinstaller)."""
    bundle_id: str
    name: str = ""
    version: str = ""
    app_type: str = ""  # "User" or "System"


# Type alias for streaming output callback
OutputCallback = Callable[[str, str], None]  # (source, line)


# ─────────────────────────────────────────────────────────────────────
#  IOSManager — Main class for libimobiledevice operations
# ─────────────────────────────────────────────────────────────────────


class IOSManager:
    """
    Manages iOS devices via libimobiledevice CLI tools.

    All methods are safe to call from any thread.  Heavy operations
    (backup, restore, sideload) stream output line-by-line via the
    optional ``output_cb`` callback.
    """

    def __init__(self):
        self._tools_cache: Dict[str, Optional[str]] = {}
        self._lock = threading.Lock()
        self._pairing_records_dir: Optional[Path] = None

    # ──────────────────────────────────────────────────────────────
    #  TOOL DISCOVERY
    # ──────────────────────────────────────────────────────────────

    def check_tools(self) -> Dict[str, bool]:
        """
        Check which libimobiledevice tools are available.
        Returns a dict of {tool_name: is_available}.
        """
        tools = [
            "idevice_id",
            "ideviceinfo",
            "idevicepair",
            "idevicename",
            "idevicebackup2",
            "ideviceinstaller",
            "idevicesyslog",
            "idevicescreenshot",
            "idevicediagnostics",
            "iproxy",
            "ifuse",
        ]
        result = {}
        for tool in tools:
            path = shutil.which(tool)
            self._tools_cache[tool] = path
            result[tool] = path is not None
        return result

    def has_tool(self, name: str) -> bool:
        """Check if a specific tool is available."""
        if name not in self._tools_cache:
            self._tools_cache[name] = shutil.which(name)
        return self._tools_cache[name] is not None

    def get_tool_path(self, name: str) -> Optional[str]:
        if name not in self._tools_cache:
            self._tools_cache[name] = shutil.which(name)
        return self._tools_cache[name]

    @property
    def is_available(self) -> bool:
        """True if at least idevice_id is available."""
        return self.has_tool("idevice_id")

    # ──────────────────────────────────────────────────────────────
    #  DEVICE LISTING
    # ──────────────────────────────────────────────────────────────

    def list_devices(self) -> List[IOSDevice]:
        """
        List connected iOS devices.
        Uses ``idevice_id -l`` to enumerate, then ``ideviceinfo``
        for details on each.
        """
        if not self.has_tool("idevice_id"):
            return []

        try:
            result = subprocess.run(
                ["idevice_id", "-l"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return []

            udids = [line.strip() for line in result.stdout.strip().split("\n")
                     if line.strip()]

            devices = []
            for udid in udids:
                device = self._get_device_info(udid)
                if device:
                    devices.append(device)

            return devices

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    def _get_device_info(self, udid: str) -> Optional[IOSDevice]:
        """Get detailed info for a specific device."""
        if not self.has_tool("ideviceinfo"):
            return IOSDevice(udid=udid)

        try:
            result = subprocess.run(
                ["ideviceinfo", "-u", udid, "-s"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return IOSDevice(udid=udid)

            info = self._parse_plist_output(result.stdout)
            return IOSDevice(
                udid=udid,
                name=info.get("DeviceName", ""),
                model=info.get("ProductType", ""),
                ios_version=info.get("ProductVersion", ""),
                serial=info.get("SerialNumber", ""),
                connection_type=info.get("ConnectionType", "USB"),
                is_paired=True,
            )
        except Exception:
            return IOSDevice(udid=udid)

    # ──────────────────────────────────────────────────────────────
    #  DEVICE INFO
    # ──────────────────────────────────────────────────────────────

    def device_info(self, udid: str) -> Dict[str, Any]:
        """Get full device info as a dict."""
        if not self.has_tool("ideviceinfo"):
            return {"error": "ideviceinfo not found"}

        try:
            result = subprocess.run(
                ["ideviceinfo", "-u", udid],
                capture_output=True, text=True, timeout=15,
            )
            return self._parse_plist_output(result.stdout)
        except Exception as exc:
            return {"error": str(exc)}

    def device_info_domain(self, udid: str, domain: str) -> Dict[str, Any]:
        """Get device info for a specific domain (e.g., 'com.apple.disk_usage')."""
        if not self.has_tool("ideviceinfo"):
            return {}

        try:
            result = subprocess.run(
                ["ideviceinfo", "-u", udid, "-q", domain],
                capture_output=True, text=True, timeout=15,
            )
            return self._parse_plist_output(result.stdout)
        except Exception:
            return {}

    # ──────────────────────────────────────────────────────────────
    #  PAIRING
    # ──────────────────────────────────────────────────────────────

    def pair_device(self, udid: str,
                    output_cb: Optional[OutputCallback] = None) -> Tuple[bool, str]:
        """
        Pair with an iOS device (user must tap Trust on the device).
        Returns (success, message).
        """
        if not self.has_tool("idevicepair"):
            return False, "idevicepair not found"

        if output_cb:
            output_cb("pair", f"Pairing with {udid}...")
            output_cb("pair", "Please tap 'Trust' on the iOS device when prompted.")

        try:
            result = subprocess.run(
                ["idevicepair", "-u", udid, "pair"],
                capture_output=True, text=True, timeout=60,
            )
            output = result.stdout.strip() + result.stderr.strip()
            if output_cb:
                output_cb("pair", output)

            if "SUCCESS" in output.upper() or result.returncode == 0:
                return True, "Device paired successfully"
            return False, output
        except subprocess.TimeoutExpired:
            return False, "Pairing timed out — did you tap Trust?"
        except Exception as exc:
            return False, str(exc)

    def validate_pair(self, udid: str) -> bool:
        """Check if the device is paired."""
        if not self.has_tool("idevicepair"):
            return False
        try:
            result = subprocess.run(
                ["idevicepair", "-u", udid, "validate"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────────
    #  BACKUP / RESTORE  (streamed progress)
    # ──────────────────────────────────────────────────────────────

    def backup(
        self,
        udid: str,
        backup_dir: str,
        encrypted: bool = False,
        password: str = "",
        output_cb: Optional[OutputCallback] = None,
        progress_cb: Optional[Callable[[IOSBackupProgress], None]] = None,
    ) -> Tuple[bool, str]:
        """
        Create a full iOS backup using idevicebackup2.

        Streams progress line-by-line. This captures contacts, SMS,
        call history, WhatsApp data, photos, app data, etc.
        """
        if not self.has_tool("idevicebackup2"):
            return False, "idevicebackup2 not found"

        os.makedirs(backup_dir, exist_ok=True)

        cmd = ["idevicebackup2", "-u", udid]
        if encrypted and password:
            cmd += ["encryption", "on", password]
            # Need to run encryption setup first, then backup
            try:
                enc_result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30,
                )
                if output_cb:
                    output_cb("backup", f"Encryption: {enc_result.stdout.strip()}")
            except Exception:
                pass
            cmd = ["idevicebackup2", "-u", udid]

        cmd += ["backup", "--full", backup_dir]

        if output_cb:
            output_cb("backup", f"$ {' '.join(cmd)}")

        return self._run_streaming_with_progress(
            cmd, "backup", output_cb, progress_cb, timeout=3600,
        )

    def restore(
        self,
        udid: str,
        backup_dir: str,
        output_cb: Optional[OutputCallback] = None,
        progress_cb: Optional[Callable[[IOSBackupProgress], None]] = None,
    ) -> Tuple[bool, str]:
        """Restore an iOS backup using idevicebackup2."""
        if not self.has_tool("idevicebackup2"):
            return False, "idevicebackup2 not found"

        cmd = ["idevicebackup2", "-u", udid, "restore", "--system",
               "--reboot", backup_dir]

        if output_cb:
            output_cb("restore", f"$ {' '.join(cmd)}")

        return self._run_streaming_with_progress(
            cmd, "restore", output_cb, progress_cb, timeout=3600,
        )

    # ──────────────────────────────────────────────────────────────
    #  APP MANAGEMENT (ideviceinstaller)
    # ──────────────────────────────────────────────────────────────

    def list_apps(self, udid: str, app_type: str = "user") -> List[IOSAppInfo]:
        """List installed apps."""
        if not self.has_tool("ideviceinstaller"):
            return []

        flag = "-l" if app_type == "user" else "-l -o list_all"
        try:
            result = subprocess.run(
                ["ideviceinstaller", "-u", udid] + flag.split(),
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return []

            apps = []
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line or line.startswith("Total") or line.startswith("CFBundle"):
                    continue
                # Format: com.example.app, "App Name", "1.0"
                parts = line.split(",", maxsplit=2)
                if len(parts) >= 1:
                    bundle_id = parts[0].strip().strip('"')
                    name = parts[1].strip().strip('"') if len(parts) > 1 else ""
                    version = parts[2].strip().strip('"') if len(parts) > 2 else ""
                    apps.append(IOSAppInfo(
                        bundle_id=bundle_id, name=name, version=version,
                        app_type=app_type,
                    ))
            return apps
        except Exception:
            return []

    def install_ipa(
        self,
        udid: str,
        ipa_path: str,
        output_cb: Optional[OutputCallback] = None,
    ) -> Tuple[bool, str]:
        """Sideload an .ipa file."""
        if not self.has_tool("ideviceinstaller"):
            return False, "ideviceinstaller not found"
        if not Path(ipa_path).exists():
            return False, f"IPA not found: {ipa_path}"

        cmd = ["ideviceinstaller", "-u", udid, "-i", ipa_path]
        if output_cb:
            output_cb("install", f"$ {' '.join(cmd)}")

        return self._run_streaming(cmd, "install", output_cb, timeout=120)

    def uninstall_app(
        self,
        udid: str,
        bundle_id: str,
        output_cb: Optional[OutputCallback] = None,
    ) -> Tuple[bool, str]:
        """Uninstall an app by bundle ID."""
        if not self.has_tool("ideviceinstaller"):
            return False, "ideviceinstaller not found"

        cmd = ["ideviceinstaller", "-u", udid, "-U", bundle_id]
        return self._run_streaming(cmd, "uninstall", output_cb, timeout=30)

    # ──────────────────────────────────────────────────────────────
    #  FILE SYSTEM (AFC via ifuse or afc client)
    # ──────────────────────────────────────────────────────────────

    def mount_afc(self, udid: str, mount_point: str) -> Tuple[bool, str]:
        """Mount the device's media directory via ifuse (AFC)."""
        if not self.has_tool("ifuse"):
            return False, "ifuse not found"

        os.makedirs(mount_point, exist_ok=True)
        try:
            result = subprocess.run(
                ["ifuse", "-u", udid, mount_point],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                return True, f"Mounted at {mount_point}"
            return False, result.stderr.strip()
        except Exception as exc:
            return False, str(exc)

    def unmount_afc(self, mount_point: str) -> Tuple[bool, str]:
        """Unmount an ifuse mount point."""
        try:
            if platform.system() == "Windows":
                # On Windows, ifuse uses dokany; just remove
                result = subprocess.run(
                    ["fusermount", "-u", mount_point],
                    capture_output=True, text=True, timeout=10,
                )
            else:
                result = subprocess.run(
                    ["fusermount", "-u", mount_point],
                    capture_output=True, text=True, timeout=10,
                )
            return result.returncode == 0, result.stderr.strip()
        except Exception as exc:
            return False, str(exc)

    # ──────────────────────────────────────────────────────────────
    #  SCREENSHOT
    # ──────────────────────────────────────────────────────────────

    def screenshot(self, udid: str, output_path: str) -> Tuple[bool, str]:
        """Take a screenshot and save to output_path."""
        if not self.has_tool("idevicescreenshot"):
            return False, "idevicescreenshot not found"

        try:
            result = subprocess.run(
                ["idevicescreenshot", "-u", udid, output_path],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and Path(output_path).exists():
                return True, output_path
            return False, result.stderr.strip()
        except Exception as exc:
            return False, str(exc)

    # ──────────────────────────────────────────────────────────────
    #  SYSLOG (streamed)
    # ──────────────────────────────────────────────────────────────

    def start_syslog(
        self,
        udid: str,
        output_cb: OutputCallback,
    ) -> Optional[subprocess.Popen]:
        """
        Start streaming syslog from the device.
        Returns the Popen object (caller can call .kill() to stop).
        """
        if not self.has_tool("idevicesyslog"):
            return None

        proc = subprocess.Popen(
            ["idevicesyslog", "-u", udid],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )

        def _reader():
            while proc.poll() is None:
                line = proc.stdout.readline()  # type: ignore[union-attr]
                if line:
                    output_cb("syslog", line.rstrip())
            proc.wait()

        threading.Thread(target=_reader, daemon=True, name="ios-syslog").start()
        return proc

    # ──────────────────────────────────────────────────────────────
    #  USB PORT FORWARDING (iproxy)
    # ──────────────────────────────────────────────────────────────

    def start_iproxy(
        self,
        udid: str,
        local_port: int,
        device_port: int,
    ) -> Optional[subprocess.Popen]:
        """
        Start iproxy for USB port forwarding.
        Returns the Popen object (caller manages lifecycle).
        """
        if not self.has_tool("iproxy"):
            return None

        proc = subprocess.Popen(
            ["iproxy", str(local_port), str(device_port), "-u", udid],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        # Give it a moment to start
        time.sleep(0.5)
        if proc.poll() is not None:
            return None  # failed to start
        return proc

    # ──────────────────────────────────────────────────────────────
    #  PAIRING RECORD MANAGEMENT (for WiFi access from Android)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_pairing_records_dir() -> Path:
        """Get the system pairing records directory."""
        system = platform.system()
        if system == "Windows":
            return Path(os.environ.get("ALLUSERSPROFILE", r"C:\ProgramData")) / "Apple" / "Lockdown"
        elif system == "Darwin":
            return Path("/var/db/lockdown")
        else:
            return Path("/var/lib/lockdown")

    def list_pairing_records(self) -> List[Dict[str, str]]:
        """List available pairing records (UDID, path)."""
        records_dir = self.get_pairing_records_dir()
        if not records_dir.is_dir():
            return []

        records = []
        for f in records_dir.iterdir():
            if f.suffix == ".plist" and len(f.stem) > 20:
                records.append({
                    "udid": f.stem,
                    "path": str(f),
                    "size": f.stat().st_size,
                })
        return records

    def export_pairing_record(self, udid: str, dest_path: str) -> Tuple[bool, str]:
        """Export a pairing record (for use on Android via WiFi)."""
        records_dir = self.get_pairing_records_dir()
        src = records_dir / f"{udid}.plist"

        if not src.exists():
            return False, f"No pairing record for {udid}"

        try:
            shutil.copy2(str(src), dest_path)
            return True, f"Exported to {dest_path}"
        except Exception as exc:
            return False, str(exc)

    # ──────────────────────────────────────────────────────────────
    #  INSTALL libimobiledevice
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_install_command() -> Optional[str]:
        """Get the command to install libimobiledevice on this system."""
        system = platform.system()
        if system == "Windows":
            if shutil.which("choco"):
                return "choco install -y libimobiledevice"
            if shutil.which("winget"):
                return "winget install libimobiledevice"
            if shutil.which("scoop"):
                return "scoop install libimobiledevice"
            return None
        elif system == "Darwin":
            if shutil.which("brew"):
                return "brew install libimobiledevice ifuse"
            return None
        else:  # Linux
            if shutil.which("apt-get"):
                return "sudo apt-get install -y libimobiledevice-utils ifuse"
            if shutil.which("dnf"):
                return "sudo dnf install -y libimobiledevice-utils ifuse"
            if shutil.which("pacman"):
                return "sudo pacman -S --noconfirm libimobiledevice ifuse"
            return None

    def install_tools(
        self,
        output_cb: Optional[OutputCallback] = None,
    ) -> Tuple[bool, str]:
        """Auto-install libimobiledevice tools."""
        cmd_str = self.get_install_command()
        if not cmd_str:
            return False, "No supported package manager found. Install manually."

        cmd = cmd_str.split()
        if output_cb:
            output_cb("install", f"$ {cmd_str}")

        return self._run_streaming(cmd, "install", output_cb, timeout=180)

    # ──────────────────────────────────────────────────────────────
    #  INTERNAL HELPERS
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_plist_output(text: str) -> Dict[str, Any]:
        """Parse ideviceinfo key: value output into a dict."""
        result = {}
        for line in text.strip().split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
        return result

    @staticmethod
    def _run_streaming(
        cmd: List[str],
        source: str,
        output_cb: Optional[OutputCallback],
        timeout: int = 120,
    ) -> Tuple[bool, str]:
        """Run a subprocess with streamed output."""
        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )

            full_output = []
            start = time.monotonic()

            while True:
                if time.monotonic() - start > timeout:
                    proc.kill()
                    proc.wait()
                    return False, "Process timed out"

                line = proc.stdout.readline()  # type: ignore[union-attr]
                if not line and proc.poll() is not None:
                    break
                if line:
                    stripped = line.rstrip()
                    full_output.append(stripped)
                    if output_cb:
                        output_cb(source, stripped)

            proc.wait(timeout=30)
            exit_code = proc.returncode

            if output_cb:
                output_cb(source, f"Exited with code {exit_code}")

            if exit_code == 0:
                return True, ""
            return False, f"Exit code {exit_code}: {chr(10).join(full_output[-5:])}"

        except FileNotFoundError:
            return False, f"Command not found: {cmd[0]}"
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def _run_streaming_with_progress(
        cmd: List[str],
        source: str,
        output_cb: Optional[OutputCallback],
        progress_cb: Optional[Callable[[IOSBackupProgress], None]],
        timeout: int = 3600,
    ) -> Tuple[bool, str]:
        """Run a subprocess with streamed output and progress parsing."""
        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )

            full_output = []
            start = time.monotonic()

            while True:
                if time.monotonic() - start > timeout:
                    proc.kill()
                    proc.wait()
                    return False, "Process timed out"

                line = proc.stdout.readline()  # type: ignore[union-attr]
                if not line and proc.poll() is not None:
                    break
                if line:
                    stripped = line.rstrip()
                    full_output.append(stripped)
                    if output_cb:
                        output_cb(source, stripped)

                    # Parse backup progress from idevicebackup2 output
                    if progress_cb:
                        progress = IOSBackupProgress(message=stripped)
                        # idevicebackup2 emits lines like:
                        # "Receiving files... (42/1234)"
                        match = re.search(r'\((\d+)/(\d+)\)', stripped)
                        if match:
                            done, total = int(match.group(1)), int(match.group(2))
                            progress.files_done = done
                            progress.files_total = total
                            progress.percent = (done / total * 100) if total > 0 else 0
                            progress.phase = "backing_up"
                        elif "Receiving" in stripped:
                            progress.phase = "backing_up"
                        elif "Finished" in stripped or "Backup Successful" in stripped:
                            progress.phase = "finishing"
                            progress.percent = 100
                        progress_cb(progress)

            proc.wait(timeout=30)
            exit_code = proc.returncode

            if exit_code == 0:
                return True, ""
            return False, f"Exit code {exit_code}"

        except FileNotFoundError:
            return False, f"Command not found: {cmd[0]}"
        except Exception as exc:
            return False, str(exc)
