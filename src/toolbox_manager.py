"""
toolbox_manager.py — Device management & optimisation toolkit via ADB.

Provides a collection of utility operations for Android device management:
  • Device info (battery, OS, CPU, RAM, display)
  • Storage analysis
  • App management (list, uninstall, force-stop, clear data/cache)
  • Screen capture & recording
  • Reboot modes (normal / recovery / bootloader / fastboot)
  • WiFi ADB toggle
  • Performance optimisation helpers (kill background, TRIM, battery stats)
  • Logcat quick capture
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .adb_core import ADBCore

log = logging.getLogger("adb_toolkit.toolbox")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class DeviceOverview:
    """Aggregated device information."""
    model: str = ""
    manufacturer: str = ""
    brand: str = ""
    android_version: str = ""
    sdk_level: str = ""
    build_number: str = ""
    security_patch: str = ""
    kernel: str = ""
    cpu_abi: str = ""
    cpu_hardware: str = ""
    cpu_cores: int = 0
    ram_total_mb: int = 0
    ram_available_mb: int = 0
    display_resolution: str = ""
    display_density: int = 0
    serial_number: str = ""
    uptime: str = ""


@dataclass
class BatteryInfo:
    """Battery status details."""
    level: int = 0
    status: str = ""          # Charging, Discharging, Full, Not charging
    health: str = ""          # Good, Overheat, Dead, …
    temperature: float = 0.0  # °C
    voltage: float = 0.0      # V
    technology: str = ""
    plugged: str = ""         # AC, USB, Wireless, None
    current_now: int = 0      # µA (negative = discharging)
    capacity: int = 0         # design capacity mAh (if available)


@dataclass
class StorageInfo:
    """Partition storage summary."""
    partition: str = ""
    total_mb: int = 0
    used_mb: int = 0
    available_mb: int = 0
    use_percent: float = 0.0


@dataclass
class AppInfo:
    """Installed application descriptor."""
    package: str = ""
    version_name: str = ""
    version_code: str = ""
    install_date: str = ""
    size_bytes: int = 0
    is_system: bool = False
    is_enabled: bool = True


@dataclass
class ToolboxProgress:
    """Generic progress update for toolbox operations."""
    phase: str = ""
    detail: str = ""
    percent: float = 0.0
    done: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# Status / Health label maps
# ---------------------------------------------------------------------------
_BATTERY_STATUS = {
    "1": "Desconhecido",
    "2": "Carregando",
    "3": "Descarregando",
    "4": "Sem carga",
    "5": "Completa",
}

_BATTERY_HEALTH = {
    "1": "Desconhecido",
    "2": "Boa",
    "3": "Superaquecida",
    "4": "Morta",
    "5": "Sobretensão",
    "6": "Falha não-especificada",
    "7": "Fria",
}

_BATTERY_PLUGGED = {
    "0": "Nenhum",
    "1": "AC",
    "2": "USB",
    "4": "Wireless",
}


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class ToolboxManager:
    """Collection of ADB-based device utilities."""

    def __init__(self, adb: ADBCore, output_dir: Optional[Path] = None):
        self.adb = adb
        self.output_dir = output_dir or (
            Path(adb.base_dir) / "toolbox_output"
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def reset_cancel(self):
        self._cancel.clear()

    # ==================================================================
    #  Device info
    # ==================================================================
    def get_device_overview(self, serial: str) -> DeviceOverview:
        """Gather comprehensive device information."""
        sh = lambda cmd: self.adb.run_shell(cmd, serial, timeout=10).strip()

        info = DeviceOverview(serial_number=serial)
        info.model = sh("getprop ro.product.model")
        info.manufacturer = sh("getprop ro.product.manufacturer")
        info.brand = sh("getprop ro.product.brand")
        info.android_version = sh("getprop ro.build.version.release")
        info.sdk_level = sh("getprop ro.build.version.sdk")
        info.build_number = sh("getprop ro.build.display.id")
        info.security_patch = sh("getprop ro.build.version.security_patch")
        info.kernel = sh("uname -r")
        info.cpu_abi = sh("getprop ro.product.cpu.abi")
        info.cpu_hardware = sh("cat /proc/cpuinfo | grep Hardware | head -1").replace("Hardware\t:", "").strip()

        # CPU cores
        try:
            info.cpu_cores = int(sh("nproc"))
        except ValueError:
            info.cpu_cores = 0

        # RAM
        meminfo = sh("cat /proc/meminfo")
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                m = re.search(r"(\d+)", line)
                if m:
                    info.ram_total_mb = int(m.group(1)) // 1024
            elif line.startswith("MemAvailable:"):
                m = re.search(r"(\d+)", line)
                if m:
                    info.ram_available_mb = int(m.group(1)) // 1024

        # Display
        wm_size = sh("wm size 2>/dev/null")
        m = re.search(r"(\d+x\d+)", wm_size)
        if m:
            info.display_resolution = m.group(1)
        wm_density = sh("wm density 2>/dev/null")
        m = re.search(r"(\d+)", wm_density)
        if m:
            info.display_density = int(m.group(1))

        # Uptime
        info.uptime = sh("uptime -p 2>/dev/null || uptime")

        return info

    def get_battery_info(self, serial: str) -> BatteryInfo:
        """Detailed battery information."""
        out = self.adb.run_shell("dumpsys battery", serial, timeout=10)
        bi = BatteryInfo()
        for line in out.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()

            if key == "level":
                bi.level = int(val) if val.isdigit() else 0
            elif key == "status":
                bi.status = _BATTERY_STATUS.get(val, val)
            elif key == "health":
                bi.health = _BATTERY_HEALTH.get(val, val)
            elif key == "temperature":
                try:
                    bi.temperature = int(val) / 10.0
                except ValueError:
                    pass
            elif key == "voltage":
                try:
                    bi.voltage = int(val) / 1000.0
                except ValueError:
                    pass
            elif key == "technology":
                bi.technology = val
            elif key == "plugged" or key == "ac powered" or key == "usb powered" or key == "wireless powered":
                if key == "plugged":
                    bi.plugged = _BATTERY_PLUGGED.get(val, val)
                elif val.lower() == "true":
                    if "ac" in key:
                        bi.plugged = "AC"
                    elif "usb" in key:
                        bi.plugged = "USB"
                    elif "wireless" in key:
                        bi.plugged = "Wireless"

        # Current (µA)
        try:
            current = self.adb.run_shell(
                "cat /sys/class/power_supply/battery/current_now 2>/dev/null",
                serial, timeout=5,
            ).strip()
            if current.lstrip("-").isdigit():
                bi.current_now = int(current)
        except Exception:
            pass

        # Design capacity
        try:
            cap = self.adb.run_shell(
                "cat /sys/class/power_supply/battery/charge_full_design 2>/dev/null",
                serial, timeout=5,
            ).strip()
            if cap.isdigit():
                bi.capacity = int(cap) // 1000  # µAh → mAh
        except Exception:
            pass

        return bi

    # ==================================================================
    #  Storage analysis
    # ==================================================================
    def get_storage_info(self, serial: str) -> List[StorageInfo]:
        """Return storage usage per partition."""
        out = self.adb.run_shell("df -h 2>/dev/null || df", serial, timeout=15)
        results: List[StorageInfo] = []
        for line in out.splitlines()[1:]:  # skip header
            parts = line.split()
            if len(parts) < 5:
                continue
            # Typical: Filesystem  Size  Used  Avail  Use%  Mounted
            try:
                si = StorageInfo(partition=parts[-1])
                si.total_mb = self._parse_size(parts[1])
                si.used_mb = self._parse_size(parts[2])
                si.available_mb = self._parse_size(parts[3])
                pct = parts[4].replace("%", "")
                si.use_percent = float(pct) if pct.replace(".", "").isdigit() else 0.0
                if si.total_mb > 0:
                    results.append(si)
            except (ValueError, IndexError):
                continue
        # Sort by total descending
        results.sort(key=lambda s: s.total_mb, reverse=True)
        return results

    @staticmethod
    def _parse_size(txt: str) -> int:
        """Parse human-readable size (e.g. '3.2G', '512M') → MB."""
        txt = txt.strip().upper()
        m = re.match(r"([\d.]+)\s*([KMGTP]?)", txt)
        if not m:
            return 0
        val = float(m.group(1))
        unit = m.group(2)
        multiplier = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024, "P": 1024 ** 3}
        return int(val * multiplier.get(unit, 1))

    # ==================================================================
    #  App management
    # ==================================================================
    def list_apps(
        self, serial: str, third_party_only: bool = True,
    ) -> List[AppInfo]:
        """List installed apps with version info."""
        flag = "-3" if third_party_only else ""
        out = self.adb.run_shell(f"pm list packages -f {flag}", serial, timeout=30)
        apps: List[AppInfo] = []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("package:"):
                continue
            # package:/data/app/.../base.apk=com.example
            eq = line.rfind("=")
            if eq < 0:
                continue
            pkg = line[eq + 1:]
            app = AppInfo(package=pkg, is_system=not third_party_only)

            # Try to get version
            try:
                dinfo = self.adb.run_shell(
                    f"dumpsys package {pkg} | grep -E 'versionName|versionCode' | head -2",
                    serial, timeout=5,
                )
                for dl in dinfo.splitlines():
                    dl = dl.strip()
                    if "versionName" in dl:
                        app.version_name = dl.split("=")[-1].strip()
                    elif "versionCode" in dl:
                        app.version_code = dl.split("=")[-1].strip().split()[0]
            except Exception:
                pass

            apps.append(app)
            if self._cancel.is_set():
                break

        apps.sort(key=lambda a: a.package)
        return apps

    def uninstall_app(self, serial: str, package: str, keep_data: bool = False) -> Tuple[bool, str]:
        """Uninstall an app. Returns (success, message)."""
        args = ["uninstall"]
        if keep_data:
            args.append("-k")
        args.append(package)
        r = self.adb.run(args, serial=serial, timeout=60)
        ok = r.returncode == 0 and "Success" in r.stdout
        msg = r.stdout.strip() or r.stderr.strip()
        return ok, msg

    def force_stop_app(self, serial: str, package: str) -> bool:
        """Force-stop a running app."""
        self.adb.run_shell(f"am force-stop {package}", serial, timeout=10)
        return True

    def clear_app_data(self, serial: str, package: str) -> Tuple[bool, str]:
        """Clear ALL data for an app (cache, databases, prefs)."""
        r = self.adb.run(["shell", "pm", "clear", package], serial=serial, timeout=30)
        ok = "Success" in (r.stdout or "")
        return ok, (r.stdout or r.stderr or "").strip()

    def clear_app_cache(self, serial: str, package: str) -> bool:
        """Clear only cache for an app."""
        return self.adb.clear_app_cache(package, serial)

    def disable_app(self, serial: str, package: str) -> Tuple[bool, str]:
        """Disable (freeze) a package."""
        r = self.adb.run(["shell", "pm", "disable-user", "--user", "0", package],
                         serial=serial, timeout=15)
        ok = r.returncode == 0
        return ok, (r.stdout or r.stderr or "").strip()

    def enable_app(self, serial: str, package: str) -> Tuple[bool, str]:
        """Re-enable a previously disabled package."""
        r = self.adb.run(["shell", "pm", "enable", package],
                         serial=serial, timeout=15)
        ok = r.returncode == 0
        return ok, (r.stdout or r.stderr or "").strip()

    # ==================================================================
    #  Screen capture & recording
    # ==================================================================
    def take_screenshot(self, serial: str, filename: str = "") -> Tuple[bool, Path]:
        """Capture screenshot and pull to local output dir."""
        if not filename:
            ts = time.strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{ts}.png"
        remote = f"/sdcard/{filename}"
        local = self.output_dir / filename

        self.adb.run_shell(f"screencap -p {remote}", serial, timeout=15)
        ok = self.adb.pull(remote, str(local), serial)
        self.adb.run_shell(f"rm {remote}", serial, timeout=5)
        return ok, local

    def start_screenrecord(
        self, serial: str, duration: int = 30, filename: str = "",
    ) -> Tuple[bool, Path]:
        """Record screen for *duration* seconds (max 180), then pull."""
        duration = max(1, min(duration, 180))
        if not filename:
            ts = time.strftime("%Y%m%d_%H%M%S")
            filename = f"screenrecord_{ts}.mp4"
        remote = f"/sdcard/{filename}"
        local = self.output_dir / filename

        self.adb.run_shell(
            f"screenrecord --time-limit {duration} {remote}",
            serial, timeout=duration + 30,
        )
        ok = self.adb.pull(remote, str(local), serial)
        self.adb.run_shell(f"rm {remote}", serial, timeout=5)
        return ok, local

    # ==================================================================
    #  Reboot modes
    # ==================================================================
    def reboot_normal(self, serial: str):
        self.adb.reboot("", serial)

    def reboot_recovery(self, serial: str):
        self.adb.reboot("recovery", serial)

    def reboot_bootloader(self, serial: str):
        self.adb.reboot("bootloader", serial)

    def reboot_fastboot(self, serial: str):
        self.adb.run(["reboot", "fastboot"], serial=serial, timeout=15)

    def shutdown(self, serial: str):
        """Power off the device."""
        self.adb.run_shell("reboot -p", serial, timeout=15)

    # ==================================================================
    #  WiFi ADB
    # ==================================================================
    def enable_wifi_adb(self, serial: str, port: int = 5555) -> Tuple[bool, str]:
        """Switch device to WiFi ADB mode."""
        # Get device IP
        ip_out = self.adb.run_shell(
            "ip addr show wlan0 | grep 'inet ' | awk '{print $2}' | cut -d/ -f1",
            serial, timeout=10,
        ).strip()
        if not ip_out:
            return False, "Não foi possível obter o IP do dispositivo. Verifique a conexão WiFi."

        self.adb.run_shell(f"setprop service.adb.tcp.port {port}", serial, timeout=5)
        self.adb.run(["tcpip", str(port)], serial=serial, timeout=10)
        time.sleep(1)

        r = self.adb.run(["connect", f"{ip_out}:{port}"], timeout=10)
        ok = "connected" in (r.stdout or "").lower()
        return ok, f"{ip_out}:{port}" if ok else (r.stdout or r.stderr or "Falha na conexão").strip()

    def disable_wifi_adb(self, serial: str) -> bool:
        """Switch back to USB mode."""
        r = self.adb.run(["usb"], serial=serial, timeout=10)
        return r.returncode == 0

    def get_device_ip(self, serial: str) -> str:
        """Return the device's Wi-Fi IP address."""
        return self.adb.run_shell(
            "ip addr show wlan0 | grep 'inet ' | awk '{print $2}' | cut -d/ -f1",
            serial, timeout=10,
        ).strip()

    # ==================================================================
    #  Performance / Optimisation
    # ==================================================================
    def kill_background_apps(self, serial: str) -> int:
        """Kill all background processes. Returns count killed."""
        out = self.adb.run_shell("am kill-all", serial, timeout=15)
        # Also force-stop known heavy background services
        running = self.adb.run_shell(
            "dumpsys activity processes | grep 'app=ProcessRecord' | wc -l",
            serial, timeout=15,
        ).strip()
        try:
            return int(running)
        except ValueError:
            return 0

    def run_fstrim(self, serial: str) -> str:
        """Trigger FSTRIM (SSD TRIM) on all partitions. Needs root on some devices."""
        out = self.adb.run_shell(
            "sm fstrim 2>/dev/null || fstrim -v /data 2>/dev/null || echo 'fstrim indisponível'",
            serial, timeout=60,
        )
        return out.strip()

    def reset_battery_stats(self, serial: str) -> bool:
        """Reset battery statistics."""
        r = self.adb.run(
            ["shell", "dumpsys", "batterystats", "--reset"],
            serial=serial, timeout=15,
        )
        return r.returncode == 0

    def get_running_services(self, serial: str) -> List[str]:
        """List currently running services."""
        out = self.adb.run_shell(
            "dumpsys activity services | grep 'ServiceRecord' | head -50",
            serial, timeout=15,
        )
        services = []
        for line in out.splitlines():
            m = re.search(r"ServiceRecord\{[^ ]+ [^ ]+ ([^\}]+)\}", line)
            if m:
                services.append(m.group(1).strip())
        return services

    def get_running_processes_count(self, serial: str) -> int:
        """Return number of running processes."""
        out = self.adb.run_shell("ps -A 2>/dev/null | wc -l || ps | wc -l", serial, timeout=10)
        try:
            return max(0, int(out.strip()) - 1)  # subtract header line
        except ValueError:
            return 0

    def get_cpu_usage(self, serial: str) -> str:
        """Return top CPU usage summary line."""
        out = self.adb.run_shell(
            "top -bn1 | head -5 2>/dev/null || dumpsys cpuinfo | head -3", serial, timeout=10,
        )
        return out.strip()

    # ==================================================================
    #  Developer tools
    # ==================================================================
    def toggle_stay_awake(self, serial: str, on: bool) -> bool:
        """Toggle 'Stay awake while charging'."""
        val = "1" if on else "0"
        self.adb.run_shell(f"settings put global stay_on_while_plugged_in {3 if on else 0}", serial, timeout=5)
        return True

    def toggle_show_touches(self, serial: str, on: bool) -> bool:
        """Toggle touch pointer visibility."""
        val = "1" if on else "0"
        self.adb.run_shell(f"settings put system show_touches {val}", serial, timeout=5)
        return True

    def toggle_layout_bounds(self, serial: str, on: bool) -> bool:
        """Toggle layout bounds (developer option)."""
        val = "true" if on else "false"
        self.adb.run_shell(
            f"setprop debug.layout {val}", serial, timeout=5,
        )
        # Need to poke SurfaceFlinger to refresh
        self.adb.run_shell("service call activity 1599295570", serial, timeout=5)
        return True

    def set_animation_scale(self, serial: str, scale: float = 1.0) -> bool:
        """Set all animation scales (0 = off, 0.5 = fast, 1.0 = default)."""
        for setting in ("window_animation_scale", "transition_animation_scale",
                        "animator_duration_scale"):
            self.adb.run_shell(
                f"settings put global {setting} {scale}", serial, timeout=5,
            )
        return True

    def get_animation_scale(self, serial: str) -> float:
        """Return current window animation scale."""
        out = self.adb.run_shell(
            "settings get global window_animation_scale", serial, timeout=5,
        ).strip()
        try:
            return float(out)
        except ValueError:
            return 1.0

    # ==================================================================
    #  Network info
    # ==================================================================
    def get_network_info(self, serial: str) -> Dict[str, str]:
        """Return dict with basic network info."""
        info: Dict[str, str] = {}
        info["ip_wifi"] = self.get_device_ip(serial) or "N/A"

        # Connected WiFi SSID
        ssid = self.adb.run_shell(
            "dumpsys wifi | grep 'mWifiInfo' | head -1",
            serial, timeout=10,
        ).strip()
        m = re.search(r'SSID: "?([^",]+)"?', ssid)
        info["ssid"] = m.group(1) if m else "N/A"

        # Mobile data type
        tel = self.adb.run_shell(
            "getprop gsm.network.type", serial, timeout=5,
        ).strip()
        info["mobile_type"] = tel or "N/A"

        # Bluetooth
        bt = self.adb.run_shell(
            "settings get global bluetooth_on", serial, timeout=5,
        ).strip()
        info["bluetooth"] = "Ligado" if bt == "1" else "Desligado"

        # Airplane mode
        airplane = self.adb.run_shell(
            "settings get global airplane_mode_on", serial, timeout=5,
        ).strip()
        info["airplane_mode"] = "Ligado" if airplane == "1" else "Desligado"

        return info

    # ==================================================================
    #  Logcat quick capture
    # ==================================================================
    def capture_logcat(
        self, serial: str, lines: int = 500, filter_tag: str = "",
    ) -> Tuple[str, Path]:
        """Capture logcat and save to file. Returns (text, file_path)."""
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"logcat_{ts}.txt"
        local = self.output_dir / filename

        tag_filter = f" -s {filter_tag}" if filter_tag else ""
        out = self.adb.run_shell(
            f"logcat -d -t {lines}{tag_filter}",
            serial, timeout=30,
        )
        local.write_text(out, encoding="utf-8")
        return out, local

    def clear_logcat(self, serial: str) -> bool:
        """Clear device logcat buffer."""
        r = self.adb.run(["logcat", "-c"], serial=serial, timeout=10)
        return r.returncode == 0

    # ==================================================================
    #  Bulk operations with progress
    # ==================================================================
    def clear_all_apps_cache(
        self,
        serial: str,
        progress_cb: Optional[Callable[[ToolboxProgress], None]] = None,
    ) -> int:
        """Clear cache for all third-party apps. Returns count cleared."""
        self.reset_cancel()
        packages = self.adb.list_packages(serial, third_party=True)
        total = len(packages)
        cleared = 0

        for i, pkg in enumerate(packages):
            if self._cancel.is_set():
                break
            if progress_cb:
                progress_cb(ToolboxProgress(
                    phase="Limpando caches",
                    detail=pkg,
                    percent=(i / total * 100) if total else 0,
                ))
            try:
                self.adb.clear_app_cache(pkg, serial)
                cleared += 1
            except Exception as exc:
                log.debug("Cache clear failed for %s: %s", pkg, exc)

        if progress_cb:
            progress_cb(ToolboxProgress(
                phase="Limpeza concluída",
                detail=f"{cleared}/{total} apps",
                percent=100, done=True,
            ))
        return cleared

    def bulk_force_stop(
        self,
        serial: str,
        progress_cb: Optional[Callable[[ToolboxProgress], None]] = None,
    ) -> int:
        """Force-stop all third-party apps."""
        self.reset_cancel()
        packages = self.adb.list_packages(serial, third_party=True)
        total = len(packages)
        stopped = 0

        for i, pkg in enumerate(packages):
            if self._cancel.is_set():
                break
            if progress_cb:
                progress_cb(ToolboxProgress(
                    phase="Encerrando apps",
                    detail=pkg,
                    percent=(i / total * 100) if total else 0,
                ))
            try:
                self.force_stop_app(serial, pkg)
                stopped += 1
            except Exception:
                pass

        if progress_cb:
            progress_cb(ToolboxProgress(
                phase="Apps encerrados",
                detail=f"{stopped}/{total}",
                percent=100, done=True,
            ))
        return stopped
