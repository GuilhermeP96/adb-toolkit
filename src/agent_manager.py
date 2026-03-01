"""
agent_manager.py — Manages the ADB Toolkit Agent lifecycle on Android devices.

Handles:
 - Detecting if agent APK is installed on a connected device
 - Installing / updating the agent via ``adb install``
 - Starting / stopping the agent service
 - ADB port forwarding to reach the agent HTTP API
 - Connecting via ``companion_client.AgentClient``
 - Building the APK from source (Gradle, if available)
 - Querying agent version & health

Usage in GUI:
    mgr = AgentManager(adb)
    status = mgr.get_status(serial)
    mgr.install(serial)
    client = mgr.connect(serial)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adb_core import ADBCore

log = logging.getLogger("adb_toolkit.agent_manager")

# ═══════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

AGENT_PACKAGE = "com.adbtoolkit.agent"
AGENT_SERVICE = f"{AGENT_PACKAGE}/.services.AgentService"
AGENT_MAIN_ACTIVITY = f"{AGENT_PACKAGE}/.ui.MainActivity"
AGENT_HTTP_PORT = 15555
AGENT_TCP_PORT = 15556
AGENT_DEFAULT_TOKEN_PROP = "persist.adbtoolkit.token"

# Paths relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_SRC_DIR = _PROJECT_ROOT / "agent"
AGENT_APK_DIR = _PROJECT_ROOT / "agent" / "app" / "build" / "outputs" / "apk"
AGENT_APK_DEBUG = AGENT_APK_DIR / "debug" / "app-debug.apk"
AGENT_APK_RELEASE = AGENT_APK_DIR / "release" / "app-release.apk"
AGENT_PREBUILT_DIR = _PROJECT_ROOT / "agent" / "prebuilt"

# Persistent store for direct (WiFi) paired devices
_DIRECT_DEVICES_FILE = _PROJECT_ROOT / "data" / "direct_devices.json"


class ConnectionProtocol(str, Enum):
    """How the PC toolkit communicates with the agent."""
    ADB = "adb"           # via ADB port forwarding (USB or adb connect)
    DIRECT = "direct"     # via WiFi HTTP direct to device IP


@dataclass
class DirectDevice:
    """
    A device connected via direct WiFi protocol (no ADB needed).

    Stored persistently — when the agent is running on the network
    the PC toolkit can reach it without any ADB involvement.
    """
    device_id: str           # unique agent device ID
    label: str               # friendly name (model)
    ip: str                  # WiFi IP address
    http_port: int = AGENT_HTTP_PORT
    tcp_port: int = AGENT_TCP_PORT
    token: str = ""          # auth token
    last_seen: float = 0.0   # timestamp of last successful ping
    model: str = ""
    android_version: str = ""

    def as_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "label": self.label,
            "ip": self.ip,
            "http_port": self.http_port,
            "tcp_port": self.tcp_port,
            "token": self.token,
            "last_seen": self.last_seen,
            "model": self.model,
            "android_version": self.android_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DirectDevice":
        return cls(
            device_id=d.get("device_id", ""),
            label=d.get("label", ""),
            ip=d.get("ip", ""),
            http_port=d.get("http_port", AGENT_HTTP_PORT),
            tcp_port=d.get("tcp_port", AGENT_TCP_PORT),
            token=d.get("token", ""),
            last_seen=d.get("last_seen", 0.0),
            model=d.get("model", ""),
            android_version=d.get("android_version", ""),
        )


class AgentState(str, Enum):
    """Possible states for the agent on a device."""
    NOT_INSTALLED = "not_installed"
    INSTALLED_STOPPED = "installed_stopped"
    INSTALLED_RUNNING = "installed_running"
    CONNECTED = "connected"
    UPDATE_AVAILABLE = "update_available"
    ERROR = "error"


@dataclass
class AgentStatus:
    """Status snapshot of the agent on a specific device."""
    serial: str
    state: AgentState = AgentState.NOT_INSTALLED
    installed_version: str = ""
    latest_version: str = ""
    agent_token: str = ""
    http_port: int = AGENT_HTTP_PORT
    tcp_port: int = AGENT_TCP_PORT
    error: str = ""
    device_sdk: int = 0
    device_model: str = ""

    @property
    def is_installed(self) -> bool:
        return self.state not in (AgentState.NOT_INSTALLED, AgentState.ERROR)

    @property
    def is_running(self) -> bool:
        return self.state in (AgentState.INSTALLED_RUNNING, AgentState.CONNECTED)

    @property
    def needs_update(self) -> bool:
        return self.state == AgentState.UPDATE_AVAILABLE

    def as_dict(self) -> dict:
        return {
            "serial": self.serial,
            "state": self.state.value,
            "installed_version": self.installed_version,
            "latest_version": self.latest_version,
            "http_port": self.http_port,
            "tcp_port": self.tcp_port,
            "device_sdk": self.device_sdk,
            "device_model": self.device_model,
            "error": self.error,
        }


@dataclass
class BuildResult:
    """Result of a Gradle build."""
    success: bool
    apk_path: Optional[Path] = None
    output: str = ""
    error: str = ""


# ═══════════════════════════════════════════════════════════════════════
#  PROGRESS CALLBACK
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class AgentProgress:
    """Progress info for agent operations."""
    stage: str = ""
    message: str = ""
    percent: float = 0.0
    done: bool = False
    error: str = ""


# ═══════════════════════════════════════════════════════════════════════
#  AGENT MANAGER
# ═══════════════════════════════════════════════════════════════════════

class AgentManager:
    """
    Manages the ADB Toolkit Agent on connected Android devices.

    Works with ADBCore for device communication and companion_client
    for high-level API access once the agent is running.
    """

    def __init__(self, adb: ADBCore):
        self.adb = adb
        self._clients: Dict[str, Any] = {}  # serial or device_id -> AgentClient
        self._statuses: Dict[str, AgentStatus] = {}
        self._direct_devices: Dict[str, DirectDevice] = {}  # device_id -> DirectDevice
        self._progress_cb: Optional[Callable[[AgentProgress], None]] = None
        self._load_direct_devices()

    def set_progress_callback(self, cb: Optional[Callable[[AgentProgress], None]]):
        """Register a callback for progress updates."""
        self._progress_cb = cb

    def _emit(self, stage: str, message: str, percent: float = 0.0,
              done: bool = False, error: str = ""):
        if self._progress_cb:
            self._progress_cb(AgentProgress(
                stage=stage, message=message,
                percent=percent, done=done, error=error,
            ))

    # ──────────────────────────────────────────────────────────────────
    #  STATUS / DETECTION
    # ──────────────────────────────────────────────────────────────────

    def get_status(self, serial: str) -> AgentStatus:
        """Get the current agent status on a device."""
        status = AgentStatus(serial=serial)
        try:
            # Get device info
            status.device_sdk = self._get_sdk_version(serial)
            status.device_model = self._get_prop(serial, "ro.product.model")

            # Check if package is installed
            version = self._get_installed_version(serial)
            if not version:
                status.state = AgentState.NOT_INSTALLED
                self._statuses[serial] = status
                return status

            status.installed_version = version
            status.latest_version = self._get_latest_version()

            # Check if service is running
            if self._is_service_running(serial):
                status.state = AgentState.INSTALLED_RUNNING
                # Try to get token
                status.agent_token = self._get_agent_token(serial)
            else:
                status.state = AgentState.INSTALLED_STOPPED

            # Check for updates
            if (status.latest_version and status.installed_version
                    and self._version_compare(status.latest_version,
                                              status.installed_version) > 0):
                status.state = AgentState.UPDATE_AVAILABLE

        except Exception as exc:
            log.error("Failed to get agent status for %s: %s", serial, exc)
            status.state = AgentState.ERROR
            status.error = str(exc)

        self._statuses[serial] = status
        return status

    def _get_sdk_version(self, serial: str) -> int:
        """Get the SDK version of the device."""
        val = self._get_prop(serial, "ro.build.version.sdk")
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0

    def _get_prop(self, serial: str, prop: str) -> str:
        """Get a system property from the device."""
        try:
            result = self.adb.run_cmd(["-s", serial, "shell", "getprop", prop])
            return result.strip() if result else ""
        except Exception:
            return ""

    def _get_installed_version(self, serial: str) -> str:
        """Get the installed agent version or empty string if not installed."""
        try:
            result = self.adb.run_cmd([
                "-s", serial, "shell",
                "dumpsys", "package", AGENT_PACKAGE,
            ])
            if not result or AGENT_PACKAGE not in result:
                return ""
            # Parse versionName from dumpsys output
            for line in result.splitlines():
                line = line.strip()
                if line.startswith("versionName="):
                    return line.split("=", 1)[1].strip()
            return "unknown"
        except Exception:
            return ""

    def _is_service_running(self, serial: str) -> bool:
        """Check if the agent service is running."""
        try:
            result = self.adb.run_cmd([
                "-s", serial, "shell",
                "dumpsys", "activity", "services", AGENT_PACKAGE,
            ])
            if result and "ServiceRecord" in result:
                return True
        except Exception:
            pass
        return False

    def _get_agent_token(self, serial: str) -> str:
        """Retrieve the agent auth token from the device."""
        # Try system property first
        token = self._get_prop(serial, AGENT_DEFAULT_TOKEN_PROP)
        if token:
            return token
        # Try reading from agent shared prefs
        try:
            result = self.adb.run_cmd([
                "-s", serial, "shell",
                "run-as", AGENT_PACKAGE,
                "cat", "shared_prefs/agent_prefs.xml",
            ])
            if result:
                # Parse XML for token
                match = re.search(
                    r'name="auth_token"[^>]*>([^<]+)<', result
                )
                if match:
                    return match.group(1)
        except Exception:
            pass
        return ""

    def _get_latest_version(self) -> str:
        """Get the latest available agent version from the APK or build config."""
        # Try reading from build.gradle.kts
        build_file = AGENT_SRC_DIR / "app" / "build.gradle.kts"
        if build_file.exists():
            try:
                content = build_file.read_text(encoding="utf-8")
                match = re.search(r'versionName\s*=\s*"([^"]+)"', content)
                if match:
                    return match.group(1)
            except Exception:
                pass
        return ""

    @staticmethod
    def _version_compare(v1: str, v2: str) -> int:
        """Compare two version strings. Returns >0 if v1 > v2."""
        def _parts(v: str) -> List[int]:
            return [int(x) for x in re.findall(r'\d+', v)]
        p1, p2 = _parts(v1), _parts(v2)
        for a, b in zip(p1, p2):
            if a != b:
                return a - b
        return len(p1) - len(p2)

    # ──────────────────────────────────────────────────────────────────
    #  APK LOCATING / BUILDING
    # ──────────────────────────────────────────────────────────────────

    def find_apk(self) -> Optional[Path]:
        """Find the best available APK to install."""
        # 1. Check prebuilt directory
        if AGENT_PREBUILT_DIR.exists():
            apks = sorted(AGENT_PREBUILT_DIR.glob("*.apk"), reverse=True)
            if apks:
                return apks[0]

        # 2. Check Gradle build outputs
        if AGENT_APK_DEBUG.exists():
            return AGENT_APK_DEBUG
        if AGENT_APK_RELEASE.exists():
            return AGENT_APK_RELEASE

        return None

    def build_apk(self, release: bool = False) -> BuildResult:
        """
        Build the agent APK using Gradle.

        Requires:
         - Android SDK installed
         - ANDROID_HOME or ANDROID_SDK_ROOT set
         - Java 17+
        """
        self._emit("build", "Checking build environment...", 5)

        if not AGENT_SRC_DIR.exists():
            return BuildResult(
                success=False,
                error="Agent source not found at: " + str(AGENT_SRC_DIR),
            )

        # Find Gradle wrapper
        if platform.system() == "Windows":
            gradlew = AGENT_SRC_DIR / "gradlew.bat"
        else:
            gradlew = AGENT_SRC_DIR / "gradlew"

        if not gradlew.exists():
            # Try system Gradle
            gradlew_path = shutil.which("gradle")
            if not gradlew_path:
                return BuildResult(
                    success=False,
                    error="Gradle not found. Install Android Studio or set up gradlew.",
                )
            gradlew = Path(gradlew_path)

        task = ":app:assembleRelease" if release else ":app:assembleDebug"

        self._emit("build", f"Building APK ({task})...", 20)

        try:
            env = os.environ.copy()

            # ── Ensure JAVA_HOME is set ──
            if "JAVA_HOME" not in env or not Path(env["JAVA_HOME"]).is_dir():
                java_home = DependencyManager._find_java_home()
                if java_home:
                    env["JAVA_HOME"] = java_home
                    java_bin = str(Path(java_home) / "bin")
                    if java_bin not in env.get("PATH", ""):
                        env["PATH"] = java_bin + os.pathsep + env.get("PATH", "")

            # ── Ensure ANDROID_HOME is set ──
            sdk_path = DependencyManager._find_android_sdk()
            if sdk_path:
                env.setdefault("ANDROID_HOME", sdk_path)
                env.setdefault("ANDROID_SDK_ROOT", sdk_path)

            # ── Ensure gradle.properties exists (android.useAndroidX) ──
            DependencyManager._ensure_gradle_properties()

            # ── Ensure local.properties exists (sdk.dir) ──
            DependencyManager._ensure_local_properties(sdk_path)

            result = subprocess.run(
                [str(gradlew), task, "--no-daemon"],
                cwd=str(AGENT_SRC_DIR),
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0:
                apk = AGENT_APK_RELEASE if release else AGENT_APK_DEBUG
                if apk.exists():
                    self._emit("build", "Build successful!", 100, done=True)
                    return BuildResult(success=True, apk_path=apk, output=result.stdout)
                else:
                    return BuildResult(
                        success=False,
                        output=result.stdout,
                        error="Build succeeded but APK not found at expected path.",
                    )
            else:
                return BuildResult(
                    success=False,
                    output=result.stdout,
                    error=result.stderr or "Gradle build failed.",
                )

        except subprocess.TimeoutExpired:
            return BuildResult(success=False, error="Build timed out after 5 minutes.")
        except Exception as exc:
            return BuildResult(success=False, error=str(exc))

    # ──────────────────────────────────────────────────────────────────
    #  INSTALL / UNINSTALL
    # ──────────────────────────────────────────────────────────────────

    def install(self, serial: str, apk_path: Optional[Path] = None) -> bool:
        """
        Install or update the agent APK on a device.

        If *apk_path* is not given, locates the best available APK
        (prebuilt > debug build > release build).
        """
        self._emit("install", "Locating APK...", 5)

        if apk_path is None:
            apk_path = self.find_apk()

        if apk_path is None or not apk_path.exists():
            self._emit("install", "APK not found.", 0, error="No APK available. Build first or place in agent/prebuilt/.")
            return False

        self._emit("install", f"Installing {apk_path.name}...", 20)

        try:
            result = self.adb.run_cmd([
                "-s", serial, "install", "-r", str(apk_path),
            ])
            if result and "Success" in result:
                self._emit("install", "Agent installed successfully!", 80)
                log.info("Agent installed on %s from %s", serial, apk_path)
                # Grant runtime permissions
                self._grant_permissions(serial)
                self._emit("install", "Installation complete.", 100, done=True)
                return True
            else:
                err = result or "Unknown error"
                self._emit("install", f"Install failed: {err}", 0, error=err)
                return False
        except Exception as exc:
            self._emit("install", str(exc), 0, error=str(exc))
            return False

    def uninstall(self, serial: str) -> bool:
        """Uninstall the agent from a device."""
        try:
            self.stop_service(serial)
            self.disconnect(serial)
            result = self.adb.run_cmd([
                "-s", serial, "uninstall", AGENT_PACKAGE,
            ])
            if result and "Success" in result:
                log.info("Agent uninstalled from %s", serial)
                return True
        except Exception as exc:
            log.error("Uninstall failed: %s", exc)
        return False

    def _grant_permissions(self, serial: str):
        """Grant key runtime permissions after install."""
        permissions = [
            "android.permission.READ_CONTACTS",
            "android.permission.WRITE_CONTACTS",
            "android.permission.READ_SMS",
            "android.permission.READ_EXTERNAL_STORAGE",
            "android.permission.WRITE_EXTERNAL_STORAGE",
            "android.permission.READ_CALL_LOG",
            "android.permission.CAMERA",
        ]
        for perm in permissions:
            try:
                self.adb.run_cmd([
                    "-s", serial, "shell",
                    "pm", "grant", AGENT_PACKAGE, perm,
                ])
            except Exception:
                pass  # Some permissions may not exist on all SDK levels

    # ──────────────────────────────────────────────────────────────────
    #  SERVICE CONTROL
    # ──────────────────────────────────────────────────────────────────

    def start_service(self, serial: str) -> bool:
        """Start the agent foreground service."""
        try:
            # First, launch the main activity to ensure the app process is alive
            self.adb.run_cmd([
                "-s", serial, "shell",
                "am", "start", "-n", AGENT_MAIN_ACTIVITY,
            ])
            time.sleep(1)

            # Start the foreground service
            self.adb.run_cmd([
                "-s", serial, "shell",
                "am", "startforegroundservice",
                "-n", AGENT_SERVICE,
            ])
            time.sleep(1)

            if self._is_service_running(serial):
                log.info("Agent service started on %s", serial)
                return True
            else:
                log.warning("Service start command sent but service not detected on %s", serial)
                return False
        except Exception as exc:
            log.error("Failed to start agent service on %s: %s", serial, exc)
            return False

    def stop_service(self, serial: str) -> bool:
        """Stop the agent service."""
        try:
            self.adb.run_cmd([
                "-s", serial, "shell",
                "am", "stopservice", "-n", AGENT_SERVICE,
            ])
            log.info("Agent service stopped on %s", serial)
            return True
        except Exception as exc:
            log.error("Failed to stop agent service on %s: %s", serial, exc)
            return False

    def launch_app(self, serial: str) -> bool:
        """Launch the agent main activity."""
        try:
            self.adb.run_cmd([
                "-s", serial, "shell",
                "am", "start", "-n", AGENT_MAIN_ACTIVITY,
            ])
            return True
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────────────
    #  CONNECTION (ADB FORWARDING + COMPANION CLIENT)
    # ──────────────────────────────────────────────────────────────────

    def connect(self, serial: str) -> Optional[Any]:
        """
        Set up ADB forwarding and connect to the agent via companion_client.

        Returns an ``AgentClient`` instance or ``None`` on failure.
        """
        self._emit("connect", "Setting up ADB port forwarding...", 10)

        try:
            # Set up port forwarding
            self._setup_forwarding(serial)
            self._emit("connect", "Connecting to agent...", 40)

            # Get token
            status = self.get_status(serial)
            token = status.agent_token

            # Import and create client
            from .companion_client import AgentClient
            client = AgentClient(
                host="127.0.0.1",
                port=AGENT_HTTP_PORT,
                token=token,
                adb_path=str(self.adb.adb_path),
                serial=serial,
            )

            # Test connection
            self._emit("connect", "Testing connection...", 60)
            resp = client.ping()
            if resp.ok:
                self._clients[serial] = client
                self._emit("connect", "Connected!", 100, done=True)
                log.info("Connected to agent on %s", serial)

                # Auto-register WiFi IP for direct protocol use
                self._auto_register_direct(serial, client)

                return client
            else:
                self._emit("connect", f"Ping failed: {resp.error}", 0,
                           error=resp.error)
                return None

        except Exception as exc:
            self._emit("connect", str(exc), 0, error=str(exc))
            return None

    def disconnect(self, serial: str):
        """Disconnect and remove port forwarding."""
        client = self._clients.pop(serial, None)
        if client:
            try:
                client.disconnect()
            except Exception:
                pass
        self._remove_forwarding(serial)

    def get_client(self, serial: str) -> Optional[Any]:
        """Get an existing connected client for a device."""
        return self._clients.get(serial)

    def _setup_forwarding(self, serial: str):
        """Set up ADB port forwarding to the agent."""
        for port in (AGENT_HTTP_PORT, AGENT_TCP_PORT):
            self.adb.run_cmd([
                "-s", serial,
                "forward", f"tcp:{port}", f"tcp:{port}",
            ])

    def _auto_register_direct(self, serial: str, client: Any):
        """
        After a successful ADB connection, discover the device's WiFi IP
        and register it for future direct protocol access.
        """
        try:
            info_resp = client.device.info()
            if not info_resp.ok or not isinstance(info_resp.data, dict):
                return
            data = info_resp.data
            device_id = data.get("device_id", "")
            if not device_id:
                return
            # Get WiFi IP from the device
            wifi_ip = ""
            try:
                ip_out = self.adb.run_cmd([
                    "-s", serial, "shell",
                    "ip", "route", "get", "8.8.8.8",
                ])
                # Parse: "8.8.8.8 via ... dev wlan0 src 192.168.1.X ..."
                import re as _re
                m = _re.search(r"src\s+([\d.]+)", ip_out)
                if m:
                    wifi_ip = m.group(1)
            except Exception:
                pass
            if not wifi_ip:
                return

            token = client.token if hasattr(client, "token") else ""
            model = data.get("model", "") or data.get("product_model", "")
            android_ver = data.get("android_version", "") or data.get("release", "")
            self.register_direct_device(
                ip=wifi_ip,
                token=token,
                device_id=device_id,
                label=model or serial,
                model=model,
                android_version=android_ver,
            )
            log.info("Auto-registered direct device %s at %s", device_id, wifi_ip)
        except Exception as exc:
            log.debug("Auto-register direct failed for %s: %s", serial, exc)

    def _remove_forwarding(self, serial: str):
        """Remove ADB port forwarding."""
        for port in (AGENT_HTTP_PORT, AGENT_TCP_PORT):
            try:
                self.adb.run_cmd([
                    "-s", serial,
                    "forward", "--remove", f"tcp:{port}",
                ])
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────
    #  DIRECT (WiFi) PROTOCOL — connect without ADB
    # ──────────────────────────────────────────────────────────────────

    def connect_direct(
        self,
        ip: str,
        token: str,
        port: int = AGENT_HTTP_PORT,
        label: str = "",
    ) -> Optional[Any]:
        """
        Connect directly to an agent over WiFi (no ADB needed).

        The device must be on the same network and the agent service
        must be running.  Returns an ``AgentClient`` or ``None``.
        """
        self._emit("connect_direct", f"Connecting to {ip}:{port}...", 10)
        try:
            from .companion_client import AgentClient
            client = AgentClient(
                host=ip,
                port=port,
                token=token,
                timeout=10,
            )
            self._emit("connect_direct", "Pinging agent...", 50)
            resp = client.ping()
            if not resp.ok:
                self._emit("connect_direct", f"Ping failed: {resp.error}", 0,
                           error=resp.error)
                return None

            # Fetch device info to populate DirectDevice metadata
            device_id = ""
            model = ""
            android_ver = ""
            try:
                info = client.device.info()
                if info.ok and isinstance(info.data, dict):
                    device_id = info.data.get("device_id", "")
                    model = info.data.get("model", "") or info.data.get("product_model", "")
                    android_ver = info.data.get("android_version", "") or info.data.get("release", "")
            except Exception:
                pass

            if not device_id:
                device_id = f"direct_{ip.replace('.', '_')}"

            dev = DirectDevice(
                device_id=device_id,
                label=label or model or ip,
                ip=ip,
                http_port=port,
                token=token,
                last_seen=time.time(),
                model=model,
                android_version=android_ver,
            )

            # Store client keyed by device_id (not ADB serial)
            self._direct_devices[device_id] = dev
            self._clients[device_id] = client
            self._save_direct_devices()

            self._emit("connect_direct", "Connected via direct protocol!", 100, done=True)
            log.info("Direct connection established to %s (%s:%d)", device_id, ip, port)
            return client

        except Exception as exc:
            self._emit("connect_direct", str(exc), 0, error=str(exc))
            return None

    def disconnect_direct(self, device_id: str):
        """Disconnect a direct (WiFi) connection."""
        client = self._clients.pop(device_id, None)
        if client:
            try:
                client.disconnect()
            except Exception:
                pass
        self._direct_devices.pop(device_id, None)
        self._save_direct_devices()
        log.info("Direct connection removed: %s", device_id)

    def get_direct_devices(self) -> Dict[str, DirectDevice]:
        """Return all known direct-protocol devices."""
        return dict(self._direct_devices)

    def ping_direct_device(self, device_id: str) -> bool:
        """Check if a direct-protocol device is still reachable."""
        client = self._clients.get(device_id)
        if not client:
            dev = self._direct_devices.get(device_id)
            if not dev:
                return False
            # Recreate client from stored info
            try:
                from .companion_client import AgentClient
                client = AgentClient(
                    host=dev.ip,
                    port=dev.http_port,
                    token=dev.token,
                    timeout=5,
                )
            except Exception:
                return False
        try:
            resp = client.ping()
            if resp.ok:
                if device_id in self._direct_devices:
                    self._direct_devices[device_id].last_seen = time.time()
                    self._save_direct_devices()
                return True
        except Exception:
            pass
        return False

    def refresh_direct_devices(self) -> Dict[str, bool]:
        """
        Ping all stored direct devices and return reachability map.

        Returns ``{device_id: True/False}``.
        """
        results: Dict[str, bool] = {}
        for device_id in list(self._direct_devices.keys()):
            results[device_id] = self.ping_direct_device(device_id)
        return results

    def register_direct_device(
        self,
        ip: str,
        token: str,
        port: int = AGENT_HTTP_PORT,
        device_id: str = "",
        label: str = "",
        model: str = "",
        android_version: str = "",
    ):
        """
        Register a paired device for direct protocol without connecting now.

        Useful when pairing is done via ADB and we store the WiFi IP for
        later direct access.
        """
        if not device_id:
            device_id = f"direct_{ip.replace('.', '_')}"
        dev = DirectDevice(
            device_id=device_id,
            label=label or model or ip,
            ip=ip,
            http_port=port,
            token=token,
            model=model,
            android_version=android_version,
        )
        self._direct_devices[device_id] = dev
        self._save_direct_devices()
        log.info("Registered direct device: %s (%s)", device_id, ip)

    def remove_direct_device(self, device_id: str):
        """Remove a stored direct device (forget it)."""
        self.disconnect_direct(device_id)  # also removes from store

    # ── Persistence helpers ───────────────────────────────────────────

    def _load_direct_devices(self):
        """Load stored direct devices from JSON file."""
        if _DIRECT_DEVICES_FILE.exists():
            try:
                data = json.loads(_DIRECT_DEVICES_FILE.read_text(encoding="utf-8"))
                for item in data:
                    dev = DirectDevice.from_dict(item)
                    self._direct_devices[dev.device_id] = dev
                log.debug("Loaded %d direct devices", len(self._direct_devices))
            except Exception as exc:
                log.warning("Failed to load direct devices: %s", exc)

    def _save_direct_devices(self):
        """Persist direct devices to JSON file."""
        try:
            _DIRECT_DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = [dev.as_dict() for dev in self._direct_devices.values()]
            _DIRECT_DEVICES_FILE.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("Failed to save direct devices: %s", exc)

    def get_connection_protocol(self, identifier: str) -> ConnectionProtocol:
        """
        Determine the connection protocol for a given device identifier.

        Returns ``ConnectionProtocol.DIRECT`` if it's a known direct device,
        otherwise ``ConnectionProtocol.ADB``.
        """
        if identifier in self._direct_devices:
            return ConnectionProtocol.DIRECT
        return ConnectionProtocol.ADB

    # ──────────────────────────────────────────────────────────────────
    #  QUICK ACTIONS (convenience wrappers)
    # ──────────────────────────────────────────────────────────────────

    def full_setup(self, serial: str, apk_path: Optional[Path] = None) -> Optional[Any]:
        """
        Full agent setup: install → start → connect.

        Returns an ``AgentClient`` on success, ``None`` on failure.
        """
        self._emit("setup", "Starting full agent setup...", 0)

        # 1. Install
        status = self.get_status(serial)
        if not status.is_installed:
            self._emit("setup", "Installing agent...", 10)
            if not self.install(serial, apk_path):
                return None
            time.sleep(2)

        # 2. Start service
        self._emit("setup", "Starting agent service...", 50)
        if not self._is_service_running(serial):
            self.start_service(serial)
            time.sleep(2)

        # 3. Connect
        self._emit("setup", "Connecting...", 70)
        return self.connect(serial)

    def get_agent_info(self, serial: str) -> Dict[str, Any]:
        """Get detailed agent info from a connected device."""
        client = self._clients.get(serial)
        if not client:
            return {"error": "Not connected"}
        try:
            resp = client.device.info()
            if resp.ok:
                return resp.data or {}
        except Exception as exc:
            return {"error": str(exc)}
        return {}

    # ──────────────────────────────────────────────────────────────────
    #  MANAGE EXTERNAL STORAGE PERMISSION (Android 11+)
    # ──────────────────────────────────────────────────────────────────

    def request_manage_storage(self, serial: str) -> bool:
        """
        Open the MANAGE_EXTERNAL_STORAGE settings for the agent.
        The user must manually grant this on the device.
        """
        try:
            self.adb.run_cmd([
                "-s", serial, "shell",
                "am", "start",
                "-a", "android.settings.MANAGE_APP_ALL_FILES_ACCESS_PERMISSION",
                "-d", f"package:{AGENT_PACKAGE}",
            ])
            return True
        except Exception:
            return False

    def enable_device_admin(self, serial: str) -> bool:
        """Open device admin settings for activation."""
        try:
            self.adb.run_cmd([
                "-s", serial, "shell",
                "am", "start",
                "-a", "android.app.action.ADD_DEVICE_ADMIN",
                "--es", "android.app.extra.DEVICE_ADMIN",
                f"{AGENT_PACKAGE}/.services.AgentDeviceAdmin",
            ])
            return True
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════════════
#  DEPENDENCY MANAGER
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class DepStatus:
    """Status of a single dependency."""
    name: str
    installed: bool
    version: str = ""
    required_for: str = ""       # "connection" | "build" | "optional"
    install_cmd: str = ""        # pip install command or download URL
    description: str = ""
    auto_installable: bool = False


@dataclass
class DependencyReport:
    """Full dependency check report."""
    python_deps: List[DepStatus] = field(default_factory=list)
    build_deps: List[DepStatus] = field(default_factory=list)

    @property
    def all_connection_ok(self) -> bool:
        return all(d.installed for d in self.python_deps if d.required_for == "connection")

    @property
    def all_build_ok(self) -> bool:
        return all(d.installed for d in self.build_deps)

    @property
    def missing_python(self) -> List[DepStatus]:
        return [d for d in self.python_deps if not d.installed]

    @property
    def missing_build(self) -> List[DepStatus]:
        return [d for d in self.build_deps if not d.installed]

    @property
    def all_missing(self) -> List[DepStatus]:
        return self.missing_python + self.missing_build

    @property
    def auto_installable(self) -> List[DepStatus]:
        """Dependencies that can be auto-installed."""
        return [d for d in self.all_missing if d.auto_installable]

    @property
    def manual_only(self) -> List[DepStatus]:
        """Dependencies that require manual installation."""
        return [d for d in self.all_missing if not d.auto_installable]

    @property
    def has_missing(self) -> bool:
        return bool(self.missing_python or self.missing_build)

    @property
    def pip_install_list(self) -> List[str]:
        return [d.install_cmd.replace("pip install ", "")
                for d in self.missing_python
                if d.install_cmd.startswith("pip")]


# Type alias for the line-by-line output callback
# cb(source: str, line: str)  — source is "pip", "winget", "gradle", etc.
OutputCallback = Callable[[str, str], None]


class DependencyManager:
    """
    Checks and auto-installs dependencies required by the Agent features.

    Fully automatic flow:
      1. ``check_all()`` — scans environment
      2. ``install_all_missing()`` — installs everything possible:
         - Python packages via ``pip install`` (streamed output)
         - Java via ``winget`` on Windows (streamed output)
         - Gradle wrapper auto-generated into agent/ project
      3. Each subprocess is tracked to completion with line-by-line output
         streamed to the caller via ``output_cb(source, line)``

    The caller only needs to show ONE confirmation dialog, then call
    ``install_all_missing()`` and watch the log stream.
    """

    # Python packages: (import_name, pip_spec, required_for, description)
    PYTHON_DEPS = [
        ("requests", "requests>=2.28.0", "connection",
         "HTTP client for communicating with the agent"),
        ("cryptography", "cryptography>=41.0.0", "connection",
         "ECDH key exchange and HMAC for secure P2P pairing"),
    ]

    # Gradle wrapper version to bootstrap
    GRADLE_WRAPPER_VERSION = "8.11.1"
    GRADLE_WRAPPER_DIST = (
        "https://services.gradle.org/distributions/"
        f"gradle-{GRADLE_WRAPPER_VERSION}-bin.zip"
    )

    def __init__(self):
        self._report: Optional[DependencyReport] = None

    # ──────────────────────────────────────────────────────────────────
    #  CHECK
    # ──────────────────────────────────────────────────────────────────

    def check_all(self, include_build: bool = True) -> DependencyReport:
        """Run a full dependency check and return a report."""
        report = DependencyReport()

        for import_name, pip_spec, required_for, desc in self.PYTHON_DEPS:
            status = self._check_python_package(import_name, pip_spec, required_for, desc)
            report.python_deps.append(status)

        if include_build:
            report.build_deps.append(self._check_java())
            report.build_deps.append(self._check_android_sdk())
            report.build_deps.append(self._check_gradle())

        self._report = report
        return report

    # ──────────────────────────────────────────────────────────────────
    #  INSTALL ALL (single entry point)
    # ──────────────────────────────────────────────────────────────────

    def install_all_missing(
        self,
        output_cb: Optional[OutputCallback] = None,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> Tuple[bool, List[str]]:
        """
        Install every auto-installable missing dependency.

        Runs each installer sequentially, streaming subprocess output
        line-by-line via ``output_cb(source, line)``.  Progress is reported
        via ``progress_cb(message, percent)`` at key milestones.

        Returns ``(all_ok, list_of_error_messages)``.
        """
        if self._report is None:
            self.check_all(include_build=True)

        report = self._report
        assert report is not None

        installable = report.auto_installable
        if not installable:
            if progress_cb:
                progress_cb("All dependencies satisfied.", 100)
            return True, []

        total_steps = len(installable)
        errors: List[str] = []
        step = 0

        # ── 1. Python packages ──
        pip_pkgs = report.pip_install_list
        if pip_pkgs:
            step += 1
            pct = (step / total_steps) * 100
            if progress_cb:
                progress_cb(f"[{step}/{total_steps}] Installing Python packages...", pct * 0.1)
            ok, err = self._pip_install_streaming(pip_pkgs, output_cb, progress_cb,
                                                   step, total_steps)
            if not ok:
                errors.append(f"pip: {err}")

        # ── 2. Java (winget on Windows) ──
        java_dep = next((d for d in report.missing_build
                         if d.name == "Java" and d.auto_installable), None)
        if java_dep:
            step += 1
            pct = (step / total_steps) * 100
            if progress_cb:
                progress_cb(f"[{step}/{total_steps}] Installing Java (Temurin JDK 17)...",
                            pct * 0.3)
            ok, err = self._install_java_streaming(output_cb, progress_cb,
                                                    step, total_steps)
            if not ok:
                errors.append(f"Java: {err}")

        # ── 3. Project config files (gradle.properties, local.properties) ──
        if output_cb:
            output_cb("config", "Ensuring project configuration files...")
        self._ensure_gradle_properties(output_cb)
        self._ensure_local_properties(output_cb=output_cb)

        # ── 4. Gradle wrapper ──
        gradle_dep = next((d for d in report.missing_build
                           if d.name == "Gradle" and d.auto_installable), None)
        if gradle_dep:
            step += 1
            pct = (step / total_steps) * 100
            if progress_cb:
                progress_cb(f"[{step}/{total_steps}] Creating Gradle wrapper...", pct * 0.8)
            ok, err = self._bootstrap_gradle_wrapper(output_cb)
            if not ok:
                errors.append(f"Gradle: {err}")

        # ── Final verification ──
        if progress_cb:
            progress_cb("Verifying installations...", 90)
        self.check_all(include_build=True)
        still_missing = [d for d in (self._report.all_missing if self._report else [])
                         if d.auto_installable]
        if still_missing:
            names = ", ".join(d.name for d in still_missing)
            errors.append(f"Still missing after install: {names}")

        all_ok = len(errors) == 0
        if progress_cb:
            if all_ok:
                progress_cb("All dependencies installed successfully!", 100)
            else:
                progress_cb(f"Completed with {len(errors)} error(s).", 100)

        return all_ok, errors

    # ──────────────────────────────────────────────────────────────────
    #  PIP INSTALL (streamed line-by-line)
    # ──────────────────────────────────────────────────────────────────

    def _pip_install_streaming(
        self,
        packages: List[str],
        output_cb: Optional[OutputCallback],
        progress_cb: Optional[Callable[[str, float], None]],
        step: int,
        total_steps: int,
    ) -> Tuple[bool, str]:
        """Install Python packages via pip with real-time output streaming."""
        cmd = [
            sys.executable, "-m", "pip", "install", "--upgrade",
            "--progress-bar=on",
        ] + packages

        if output_cb:
            output_cb("pip", f"$ {' '.join(cmd)}")

        proc: Optional[subprocess.Popen[str]] = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            full_output = []
            line_count = 0
            while True:
                line = proc.stdout.readline()  # type: ignore[union-attr]
                if not line and proc.poll() is not None:
                    break
                if line:
                    stripped = line.rstrip()
                    full_output.append(stripped)
                    line_count += 1
                    if output_cb:
                        output_cb("pip", stripped)
                    # Approximate progress within this step
                    if progress_cb:
                        inner_pct = min(line_count * 3, 90)
                        overall = ((step - 1) / total_steps * 100
                                   + inner_pct / total_steps)
                        progress_cb(stripped[:80], overall)

            # Ensure process has fully terminated and get return code
            proc.wait(timeout=30)
            exit_code = proc.returncode

            if output_cb:
                output_cb("pip", f"pip exited with code {exit_code}")

            if exit_code == 0:
                return True, ""
            else:
                err_text = "\n".join(full_output[-5:])
                return False, f"pip exit code {exit_code}: {err_text}"

        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
                proc.wait()
            return False, "pip timed out"
        except Exception as exc:
            return False, str(exc)

    # ──────────────────────────────────────────────────────────────────
    #  JAVA INSTALL (winget / brew, streamed)
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_package_manager() -> Optional[str]:
        """Detect available system package manager."""
        if platform.system() == "Windows":
            if shutil.which("winget"):
                return "winget"
            if shutil.which("choco"):
                return "choco"
            if shutil.which("scoop"):
                return "scoop"
        elif platform.system() == "Darwin":
            if shutil.which("brew"):
                return "brew"
        else:  # Linux
            for pm in ("apt-get", "dnf", "pacman", "zypper"):
                if shutil.which(pm):
                    return pm
        return None

    @staticmethod
    def _find_java_home() -> Optional[str]:
        """Detect JAVA_HOME from common install locations."""
        # Already set?
        jh = os.environ.get("JAVA_HOME")
        if jh and Path(jh).is_dir():
            return jh

        system = platform.system()
        if system == "Windows":
            # Eclipse Adoptium / Temurin
            for base in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                         Path(r"C:\Program Files")):
                adoptium = base / "Eclipse Adoptium"
                if adoptium.is_dir():
                    jdks = sorted(
                        [d for d in adoptium.iterdir()
                         if d.is_dir() and d.name.startswith("jdk-17")],
                        reverse=True,
                    )
                    if jdks:
                        return str(jdks[0])
                    # Fall back to any JDK >= 17
                    jdks = sorted(
                        [d for d in adoptium.iterdir() if d.is_dir()],
                        reverse=True,
                    )
                    if jdks:
                        return str(jdks[0])
            # Oracle / other
            java_base = Path(r"C:\Program Files\Java")
            if java_base.is_dir():
                jdks = sorted(
                    [d for d in java_base.iterdir()
                     if d.is_dir() and d.name.startswith("jdk")],
                    reverse=True,
                )
                if jdks:
                    return str(jdks[0])
        elif system == "Darwin":
            # macOS Temurin / system Java
            java_home_cmd = "/usr/libexec/java_home"
            if Path(java_home_cmd).exists():
                try:
                    r = subprocess.run([java_home_cmd], capture_output=True, text=True, timeout=5)
                    if r.returncode == 0 and r.stdout.strip():
                        return r.stdout.strip()
                except Exception:
                    pass
        else:  # Linux
            for candidate in ("/usr/lib/jvm/temurin-17-jdk-amd64",
                              "/usr/lib/jvm/java-17-openjdk-amd64",
                              "/usr/lib/jvm/java-17"):
                if Path(candidate).is_dir():
                    return candidate

        return None

    @staticmethod
    def _find_android_sdk() -> Optional[str]:
        """Detect Android SDK from env vars or common locations."""
        for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            path = os.environ.get(var)
            if path and Path(path).is_dir():
                return path
        system = platform.system()
        if system == "Windows":
            default = Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk"
        elif system == "Darwin":
            default = Path.home() / "Library" / "Android" / "sdk"
        else:
            default = Path.home() / "Android" / "Sdk"
        if default.is_dir():
            return str(default)
        return None

    @staticmethod
    def _ensure_gradle_properties(output_cb: Optional[OutputCallback] = None):
        """Create gradle.properties with android.useAndroidX=true if missing."""
        props_file = AGENT_SRC_DIR / "gradle.properties"
        if props_file.exists():
            content = props_file.read_text(encoding="utf-8")
            if "android.useAndroidX" in content:
                return  # already configured
            # Append the flag
            with open(props_file, "a", encoding="utf-8") as f:
                f.write("\nandroid.useAndroidX=true\n")
            if output_cb:
                output_cb("gradle", "  Added android.useAndroidX=true to gradle.properties")
            return

        props_file.write_text(
            "# Project-wide Gradle settings.\n"
            "\n"
            "# AndroidX package structure\n"
            "android.useAndroidX=true\n"
            "\n"
            "# Kotlin code style\n"
            "kotlin.code.style=official\n"
            "\n"
            "# JVM args for Gradle daemon\n"
            "org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8\n"
            "\n"
            "# Enable build cache\n"
            "org.gradle.caching=true\n"
            "\n"
            "# Non-transitive R classes (recommended for AGP 8+)\n"
            "android.nonTransitiveRClass=true\n",
            encoding="utf-8",
        )
        if output_cb:
            output_cb("gradle", "  Created gradle.properties (android.useAndroidX=true)")

    @staticmethod
    def _ensure_local_properties(
        sdk_path: Optional[str] = None,
        output_cb: Optional[OutputCallback] = None,
    ):
        """Create local.properties with sdk.dir if missing."""
        local_props = AGENT_SRC_DIR / "local.properties"
        if local_props.exists():
            return  # don't overwrite user's file

        if not sdk_path:
            sdk_path = DependencyManager._find_android_sdk()
        if not sdk_path:
            return  # can't write without a path

        # Gradle expects forward slashes even on Windows
        sdk_dir_escaped = sdk_path.replace("\\", "/")
        local_props.write_text(
            f"# Auto-generated by ADB Toolkit\n"
            f"sdk.dir={sdk_dir_escaped}\n",
            encoding="utf-8",
        )
        if output_cb:
            output_cb("sdk", f"  Created local.properties (sdk.dir={sdk_dir_escaped})")

    def _install_java_streaming(
        self,
        output_cb: Optional[OutputCallback],
        progress_cb: Optional[Callable[[str, float], None]],
        step: int,
        total_steps: int,
    ) -> Tuple[bool, str]:
        """Install Java 17 (Temurin) via system package manager."""
        pm = self._find_package_manager()
        if not pm:
            return False, "No package manager found (winget/choco/brew). Install Java manually."

        # Build the install command per package manager
        cmd_map = {
            "winget": ["winget", "install", "--id", "EclipseAdoptium.Temurin.17.JDK",
                       "--accept-source-agreements", "--accept-package-agreements"],
            "choco": ["choco", "install", "temurin17", "-y"],
            "scoop": ["scoop", "install", "temurin17-jdk"],
            "brew": ["brew", "install", "--cask", "temurin@17"],
            "apt-get": ["sudo", "apt-get", "install", "-y", "temurin-17-jdk"],
            "dnf": ["sudo", "dnf", "install", "-y", "temurin-17-jdk"],
            "pacman": ["sudo", "pacman", "-S", "--noconfirm", "jdk17-temurin"],
        }
        cmd = cmd_map.get(pm)
        if not cmd:
            return False, f"Unsupported package manager: {pm}"

        if output_cb:
            output_cb("java", f"Using {pm} to install Java 17 (Temurin)...")
            output_cb("java", f"$ {' '.join(cmd)}")

        ok, err = self._run_streaming(cmd, "java", output_cb, progress_cb,
                                      step, total_steps, timeout=300)

        # After install, detect and set JAVA_HOME in the current process
        if ok:
            java_home = self._find_java_home()
            if java_home:
                os.environ["JAVA_HOME"] = java_home
                # Also add to PATH so gradlew can find java
                java_bin = str(Path(java_home) / "bin")
                if java_bin not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = java_bin + os.pathsep + os.environ.get("PATH", "")
                if output_cb:
                    output_cb("java", f"  JAVA_HOME={java_home}")
            else:
                if output_cb:
                    output_cb("java", "  Warning: JAVA_HOME not auto-detected. "
                              "You may need to restart the application.")

        return ok, err

    # ──────────────────────────────────────────────────────────────────
    #  GRADLE WRAPPER BOOTSTRAP
    # ──────────────────────────────────────────────────────────────────

    def _bootstrap_gradle_wrapper(
        self,
        output_cb: Optional[OutputCallback],
    ) -> Tuple[bool, str]:
        """Create Gradle wrapper files in the agent project directory."""
        wrapper_dir = AGENT_SRC_DIR / "gradle" / "wrapper"
        gradlew_bat = AGENT_SRC_DIR / "gradlew.bat"
        gradlew_sh = AGENT_SRC_DIR / "gradlew"
        props_file = wrapper_dir / "gradle-wrapper.properties"

        if output_cb:
            output_cb("gradle", "Bootstrapping Gradle wrapper...")

        try:
            wrapper_dir.mkdir(parents=True, exist_ok=True)

            # gradle-wrapper.properties
            if not props_file.exists():
                props_file.write_text(
                    f"distributionBase=GRADLE_USER_HOME\n"
                    f"distributionPath=wrapper/dists\n"
                    f"distributionUrl=https\\://services.gradle.org/distributions/"
                    f"gradle-{self.GRADLE_WRAPPER_VERSION}-bin.zip\n"
                    f"networkTimeout=10000\n"
                    f"validateDistributionUrl=true\n"
                    f"zipStoreBase=GRADLE_USER_HOME\n"
                    f"zipStorePath=wrapper/dists\n",
                    encoding="utf-8",
                )
                if output_cb:
                    output_cb("gradle", f"  Created {props_file.name}")

            # gradlew.bat (Windows)
            if not gradlew_bat.exists():
                gradlew_bat.write_text(self._GRADLEW_BAT, encoding="utf-8")
                if output_cb:
                    output_cb("gradle", "  Created gradlew.bat")

            # gradlew (Unix)
            if not gradlew_sh.exists():
                gradlew_sh.write_text(self._GRADLEW_SH, encoding="utf-8")
                try:
                    gradlew_sh.chmod(0o755)
                except Exception:
                    pass
                if output_cb:
                    output_cb("gradle", "  Created gradlew")

            # We still need the gradle-wrapper.jar. Check if system Gradle
            # can generate it, or try downloading it.
            jar_file = wrapper_dir / "gradle-wrapper.jar"
            if not jar_file.exists():
                ok = self._download_wrapper_jar(jar_file, output_cb)
                if not ok:
                    # Try using system gradle to generate it
                    gradle_path = shutil.which("gradle")
                    if gradle_path:
                        if output_cb:
                            output_cb("gradle", "  Running 'gradle wrapper' to generate jar...")
                        try:
                            result = subprocess.run(
                                [gradle_path, "wrapper",
                                 f"--gradle-version={self.GRADLE_WRAPPER_VERSION}"],
                                cwd=str(AGENT_SRC_DIR),
                                capture_output=True, text=True, timeout=60,
                            )
                            if result.returncode == 0 and jar_file.exists():
                                if output_cb:
                                    output_cb("gradle", "  Wrapper jar generated successfully")
                            else:
                                if output_cb:
                                    output_cb("gradle",
                                              f"  Warning: could not generate wrapper jar: "
                                              f"{result.stderr[:200]}")
                        except Exception as exc:
                            if output_cb:
                                output_cb("gradle", f"  Warning: {exc}")
                    else:
                        if output_cb:
                            output_cb("gradle",
                                      "  Warning: gradle-wrapper.jar not downloaded. "
                                      "Run 'gradle wrapper' manually or download Gradle.")

            if output_cb:
                output_cb("gradle", "Gradle wrapper setup complete.")

            return True, ""

        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def _download_wrapper_jar(
        dest: Path,
        output_cb: Optional[OutputCallback],
    ) -> bool:
        """Download gradle-wrapper.jar from the Gradle GitHub releases."""
        # The wrapper jar is distributed via Gradle's GitHub repo
        jar_url = (
            "https://raw.githubusercontent.com/gradle/gradle/master/"
            "gradle/wrapper/gradle-wrapper.jar"
        )
        if output_cb:
            output_cb("gradle", f"  Downloading gradle-wrapper.jar...")

        try:
            import urllib.request
            urllib.request.urlretrieve(jar_url, str(dest))
            if dest.exists() and dest.stat().st_size > 1000:
                if output_cb:
                    output_cb("gradle", f"  Downloaded ({dest.stat().st_size:,} bytes)")
                return True
        except Exception as exc:
            if output_cb:
                output_cb("gradle", f"  Download failed: {exc}")
        return False

    # ──────────────────────────────────────────────────────────────────
    #  GENERIC STREAMING PROCESS RUNNER
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _run_streaming(
        cmd: List[str],
        source: str,
        output_cb: Optional[OutputCallback],
        progress_cb: Optional[Callable[[str, float], None]],
        step: int,
        total_steps: int,
        timeout: int = 120,
    ) -> Tuple[bool, str]:
        """
        Run a subprocess with real-time line-by-line output.

        Tracks the child process to completion and returns
        ``(success, error_message)``.
        """
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            full_output: List[str] = []
            line_count = 0
            import time as _time
            start = _time.monotonic()

            while True:
                # Check timeout
                if _time.monotonic() - start > timeout:
                    proc.kill()
                    proc.wait()
                    return False, f"Process timed out after {timeout}s"

                line = proc.stdout.readline()  # type: ignore[union-attr]
                if not line and proc.poll() is not None:
                    break
                if line:
                    stripped = line.rstrip()
                    full_output.append(stripped)
                    line_count += 1
                    if output_cb:
                        output_cb(source, stripped)
                    if progress_cb:
                        inner_pct = min(line_count * 2, 90)
                        overall = ((step - 1) / total_steps * 100
                                   + inner_pct / total_steps)
                        progress_cb(stripped[:80], min(overall, 95))

            # Wait for process to fully terminate
            proc.wait(timeout=30)
            exit_code = proc.returncode

            if output_cb:
                output_cb(source, f"{source} exited with code {exit_code}")

            if exit_code == 0:
                return True, ""
            else:
                tail = "\n".join(full_output[-5:])
                return False, f"Exit code {exit_code}: {tail}"

        except FileNotFoundError:
            return False, f"Command not found: {cmd[0]}"
        except Exception as exc:
            return False, str(exc)

    # ──────────────────────────────────────────────────────────────────
    #  INDIVIDUAL CHECKS
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _check_python_package(
        import_name: str, pip_spec: str, required_for: str, description: str,
    ) -> DepStatus:
        try:
            mod = __import__(import_name)
            version = getattr(mod, "__version__", "")
            if not version:
                try:
                    from importlib.metadata import version as meta_version
                    version = meta_version(import_name)
                except Exception:
                    version = "installed"
            return DepStatus(
                name=import_name, installed=True, version=version,
                required_for=required_for,
                install_cmd=f"pip install {pip_spec}",
                description=description,
                auto_installable=True,
            )
        except ImportError:
            return DepStatus(
                name=import_name, installed=False, version="",
                required_for=required_for,
                install_cmd=f"pip install {pip_spec}",
                description=description,
                auto_installable=True,
            )

    @staticmethod
    def _check_java() -> DepStatus:
        # First try JAVA_HOME detection (may find Java even if not on PATH)
        java_home = DependencyManager._find_java_home()
        java_cmd = "java"
        if java_home:
            candidate = Path(java_home) / "bin" / ("java.exe" if platform.system() == "Windows" else "java")
            if candidate.exists():
                java_cmd = str(candidate)

        try:
            result = subprocess.run(
                [java_cmd, "-version"],
                capture_output=True, text=True, timeout=10,
            )
            output = result.stderr or result.stdout
            match = re.search(r'version "(\d+)', output)
            if match:
                major = int(match.group(1))
                version = match.group(0).replace('version "', "").rstrip('"')
                desc = f"Java {major} found"
                if java_home:
                    desc += f" (JAVA_HOME={java_home})"
                if major < 17:
                    desc += " (need 17+)"
                return DepStatus(
                    name="Java", installed=major >= 17,
                    version=version,
                    required_for="build",
                    install_cmd="winget install EclipseAdoptium.Temurin.17.JDK",
                    description=desc,
                    auto_installable=(major < 17),  # can auto-upgrade
                )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # Not installed — check if we CAN auto-install
        has_pm = bool(DependencyManager._find_package_manager())
        return DepStatus(
            name="Java", installed=False, version="",
            required_for="build",
            install_cmd="winget install EclipseAdoptium.Temurin.17.JDK"
            if has_pm else "https://adoptium.net/temurin/releases/",
            description="Java 17+ required to compile the agent APK",
            auto_installable=has_pm,
        )

    @staticmethod
    def _check_android_sdk() -> DepStatus:
        for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            path = os.environ.get(var)
            if path and Path(path).is_dir():
                bt = Path(path) / "build-tools"
                versions = sorted(bt.iterdir(), reverse=True) if bt.is_dir() else []
                ver = versions[0].name if versions else "found"
                return DepStatus(
                    name="Android SDK", installed=True, version=ver,
                    required_for="build",
                    install_cmd="https://developer.android.com/studio",
                    description=f"SDK at {path}",
                    auto_installable=False,
                )
        if platform.system() == "Windows":
            default = Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk"
        elif platform.system() == "Darwin":
            default = Path.home() / "Library" / "Android" / "sdk"
        else:
            default = Path.home() / "Android" / "Sdk"
        if default.is_dir():
            return DepStatus(
                name="Android SDK", installed=True,
                version="auto-detected",
                required_for="build",
                install_cmd="https://developer.android.com/studio",
                description=f"SDK at {default}",
                auto_installable=False,
            )
        return DepStatus(
            name="Android SDK", installed=False, version="",
            required_for="build",
            install_cmd="https://developer.android.com/studio",
            description="Android SDK required to compile the agent APK",
            auto_installable=False,  # too complex to auto-install
        )

    @staticmethod
    def _check_gradle() -> DepStatus:
        wrapper = AGENT_SRC_DIR / (
            "gradlew.bat" if platform.system() == "Windows" else "gradlew"
        )
        jar = AGENT_SRC_DIR / "gradle" / "wrapper" / "gradle-wrapper.jar"
        if wrapper.exists() and jar.exists():
            return DepStatus(
                name="Gradle", installed=True, version="wrapper",
                required_for="build",
                install_cmd="",
                description="Gradle wrapper found in agent project",
                auto_installable=False,
            )
        gradle_path = shutil.which("gradle")
        if gradle_path:
            try:
                result = subprocess.run(
                    [gradle_path, "--version"],
                    capture_output=True, text=True, timeout=15,
                )
                match = re.search(r'Gradle (\S+)', result.stdout)
                ver = match.group(1) if match else "found"
                return DepStatus(
                    name="Gradle", installed=True, version=ver,
                    required_for="build",
                    install_cmd="",
                    description=f"Gradle {ver} at {gradle_path}",
                    auto_installable=False,
                )
            except Exception:
                return DepStatus(
                    name="Gradle", installed=True, version="found",
                    required_for="build",
                    install_cmd="",
                    description=f"Gradle at {gradle_path}",
                    auto_installable=False,
                )
        # Not installed — we CAN auto-bootstrap the wrapper
        return DepStatus(
            name="Gradle", installed=False, version="",
            required_for="build",
            install_cmd="auto-bootstrap wrapper",
            description="Will create Gradle wrapper in the agent project",
            auto_installable=True,
        )

    # ── gradlew script templates ──────────────────────────────────────

    _GRADLEW_BAT = r"""@rem
@rem  Gradle start up script for Windows
@rem
@if "%DEBUG%"=="" @echo off
setlocal
set DIRNAME=%~dp0
set APP_BASE_NAME=%~n0
set APP_HOME=%DIRNAME%
set DEFAULT_JVM_OPTS="-Xmx64m" "-Xms64m"
set CLASSPATH=%APP_HOME%\gradle\wrapper\gradle-wrapper.jar
@rem Execute Gradle
"%JAVA_HOME%\bin\java.exe" %DEFAULT_JVM_OPTS% %JAVA_OPTS% ^
  -classpath "%CLASSPATH%" ^
  org.gradle.wrapper.GradleWrapperMain %*
:end
endlocal
"""

    _GRADLEW_SH = r"""#!/bin/sh
APP_HOME=$(cd "$(dirname "$0")" && pwd -P)
CLASSPATH="$APP_HOME/gradle/wrapper/gradle-wrapper.jar"
DEFAULT_JVM_OPTS='"-Xmx64m" "-Xms64m"'
exec java $DEFAULT_JVM_OPTS $JAVA_OPTS \
    -classpath "$CLASSPATH" \
    org.gradle.wrapper.GradleWrapperMain "$@"
"""

