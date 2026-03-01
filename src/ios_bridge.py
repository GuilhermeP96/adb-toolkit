"""
ios_bridge.py — Unified bridge for iOS device operations.

Combines two access methods into a single high-level API:
 1. **iOS Agent** (HTTP/TCP): When the iOS Agent app is running on the
    iPhone (contacts, photos, files in sandbox, device info, D2D pairing)
 2. **libimobiledevice** (CLI): Direct USB/WiFi access for backup,
    restore, app install/remove, AFC filesystem, screenshot, syslog

The GUI uses this bridge the same way it uses ``agent_bridge.py``
for Android — check availability, then call the appropriate method.

Usage:
    from src.ios_bridge import IOSBridge

    bridge = IOSBridge(ios_mgr)
    devices = bridge.list_devices()
    if devices:
        dev = devices[0]
        # Agent-based (requires app running)
        contacts = bridge.export_contacts_vcf(dev.udid, Path("contacts.vcf"))
        # libimobiledevice-based (USB, no app needed)
        bridge.backup(dev.udid, Path("backups/my_iphone"))
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .ios_manager import IOSManager, IOSDevice, IOSAppInfo, IOSBackupProgress

log = logging.getLogger("adb_toolkit.ios_bridge")

# Re-use the companion_client for iOS Agent HTTP comms
# (same protocol: HTTP on 15555, TCP on 15556, ECDH+HMAC auth)
try:
    from .companion_client import AgentClient, AgentResponse
    HAS_AGENT_CLIENT = True
except ImportError:
    HAS_AGENT_CLIENT = False
    AgentClient = None       # type: ignore[assignment, misc]
    AgentResponse = None     # type: ignore[assignment, misc]

OutputCallback = Callable[[str, str], None]


class IOSBridge:
    """
    Unified iOS bridge — delegates to Agent HTTP or libimobiledevice
    depending on availability.

    Priority: Agent app > libimobiledevice > unavailable
    """

    def __init__(self, ios_mgr: Optional[IOSManager] = None):
        self._mgr = ios_mgr or IOSManager()
        # Map of udid → AgentClient for iOS devices running the agent
        self._agent_clients: Dict[str, AgentClient] = {}
        # Active iproxy processes {udid: Popen}
        self._iproxy_procs: Dict[str, subprocess.Popen] = {}

    # ──────────────────────────────────────────────────────────────
    #  CONNECTION MANAGEMENT
    # ──────────────────────────────────────────────────────────────

    def connect_agent(
        self,
        udid: str,
        host: str,
        port: int = 15555,
        token: str = "",
    ) -> bool:
        """
        Connect to the iOS Agent app running on a device.
        Call this when the device IP and token are known
        (e.g., user scanned QR or entered manually).
        """
        if not HAS_AGENT_CLIENT:
            log.warning("companion_client not available — cannot connect to iOS agent")
            return False

        try:
            client = AgentClient(host=host, port=port, token=token)
            client.connect()
            resp = client.ping()
            if resp.ok:
                self._agent_clients[udid] = client
                log.info("Connected to iOS Agent on %s:%d", host, port)
                return True
        except Exception as exc:
            log.warning("Failed to connect iOS Agent at %s:%d — %s", host, port, exc)
        return False

    def connect_agent_via_usb(
        self,
        udid: str,
        token: str = "",
        local_port: int = 15555,
    ) -> bool:
        """
        Connect to iOS Agent via USB (iproxy port forwarding).
        Uses iproxy to forward local_port → device:15555.
        """
        proc = self._mgr.start_iproxy(udid, local_port, 15555)
        if not proc:
            log.warning("Failed to start iproxy for %s", udid)
            return False

        self._iproxy_procs[udid] = proc
        return self.connect_agent(udid, "127.0.0.1", local_port, token)

    def disconnect_agent(self, udid: str):
        """Disconnect from a device's agent."""
        self._agent_clients.pop(udid, None)
        proc = self._iproxy_procs.pop(udid, None)
        if proc:
            proc.kill()
            proc.wait()

    def has_agent(self, udid: str) -> bool:
        """Check if we have an active agent connection for this device."""
        client = self._agent_clients.get(udid)
        if not client:
            return False
        try:
            resp = client.ping()
            return resp.ok
        except Exception:
            self._agent_clients.pop(udid, None)
            return False

    def has_libimobiledevice(self) -> bool:
        """Check if libimobiledevice tools are available."""
        return self._mgr.is_available

    # ──────────────────────────────────────────────────────────────
    #  DEVICE DISCOVERY
    # ──────────────────────────────────────────────────────────────

    def list_devices(self) -> List[IOSDevice]:
        """
        List connected iOS devices via libimobiledevice.
        This discovers USB-connected iPhones/iPads.
        """
        return self._mgr.list_devices()

    def device_info(self, udid: str) -> Dict[str, Any]:
        """
        Get device info — prefers agent (richer data) then libimobiledevice.
        """
        # Try agent first
        client = self._agent_clients.get(udid)
        if client:
            try:
                resp = client._get("/api/device/info")
                if resp.ok and resp.data:
                    return resp.data
            except Exception:
                pass

        # Fall back to libimobiledevice
        return self._mgr.device_info(udid)

    # ──────────────────────────────────────────────────────────────
    #  CONTACTS (agent only)
    # ──────────────────────────────────────────────────────────────

    def export_contacts_vcf(self, udid: str, dest: Path) -> Optional[Path]:
        """
        Export contacts as VCF — requires iOS Agent app.
        (libimobiledevice can get contacts via backup, but that's a full backup.)
        """
        client = self._agent_clients.get(udid)
        if not client:
            log.warning("No iOS agent for %s — contacts require the agent app", udid)
            return None

        try:
            resp = client._get("/api/contacts/export-vcf")
            if resp.ok and resp.raw:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(resp.raw)
                log.info("Exported %d bytes of iOS contacts VCF", len(resp.raw))
                return dest
        except Exception as exc:
            log.warning("iOS contact export failed: %s", exc)
        return None

    def list_contacts(self, udid: str) -> List[Dict[str, Any]]:
        """List contacts via iOS agent."""
        client = self._agent_clients.get(udid)
        if not client:
            return []
        try:
            resp = client._get("/api/contacts/list")
            if resp.ok and isinstance(resp.data, list):
                return resp.data
        except Exception:
            pass
        return []

    def contact_count(self, udid: str) -> int:
        """Get contact count via iOS agent."""
        client = self._agent_clients.get(udid)
        if not client:
            return -1
        try:
            resp = client._get("/api/contacts/count")
            if resp.ok:
                return resp.get("count", 0)
        except Exception:
            pass
        return -1

    # ──────────────────────────────────────────────────────────────
    #  PHOTOS (agent only)
    # ──────────────────────────────────────────────────────────────

    def list_photos(self, udid: str, offset: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        """List photos via iOS agent (paginated)."""
        client = self._agent_clients.get(udid)
        if not client:
            return []
        try:
            resp = client._get(f"/api/photos/list?offset={offset}&limit={limit}")
            if resp.ok and isinstance(resp.data, list):
                return resp.data
        except Exception:
            pass
        return []

    def photo_count(self, udid: str) -> int:
        """Get total photo count."""
        client = self._agent_clients.get(udid)
        if not client:
            return -1
        try:
            resp = client._get("/api/photos/count")
            if resp.ok:
                return resp.get("count", 0)
        except Exception:
            pass
        return -1

    def download_photo(self, udid: str, asset_id: str, dest: Path) -> bool:
        """Download a full-res photo from the agent."""
        client = self._agent_clients.get(udid)
        if not client:
            return False
        try:
            resp = client._get(f"/api/photos/full?id={asset_id}")
            if resp.ok and resp.raw:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(resp.raw)
                return True
        except Exception:
            pass
        return False

    # ──────────────────────────────────────────────────────────────
    #  FILES (agent for sandbox, AFC for media)
    # ──────────────────────────────────────────────────────────────

    def list_files_agent(self, udid: str, path: str = "/") -> List[Dict[str, Any]]:
        """List files in the agent's sandbox."""
        client = self._agent_clients.get(udid)
        if not client:
            return []
        try:
            resp = client._get(f"/api/files/list?path={path}")
            if resp.ok and isinstance(resp.data, list):
                return resp.data
        except Exception:
            pass
        return []

    def mount_media(self, udid: str, mount_point: str) -> Tuple[bool, str]:
        """Mount the device media directory via AFC (ifuse)."""
        return self._mgr.mount_afc(udid, mount_point)

    def unmount_media(self, mount_point: str) -> Tuple[bool, str]:
        """Unmount AFC mount point."""
        return self._mgr.unmount_afc(mount_point)

    # ──────────────────────────────────────────────────────────────
    #  BACKUP / RESTORE (libimobiledevice)
    # ──────────────────────────────────────────────────────────────

    def backup(
        self,
        udid: str,
        backup_dir: Path,
        encrypted: bool = False,
        password: str = "",
        output_cb: Optional[OutputCallback] = None,
        progress_cb: Optional[Callable[[IOSBackupProgress], None]] = None,
    ) -> Tuple[bool, str]:
        """
        Create a full iOS backup using libimobiledevice.
        This gets contacts, SMS, call history, WhatsApp data, photos,
        app data — everything in an iTunes-style backup.
        """
        return self._mgr.backup(
            udid, str(backup_dir),
            encrypted=encrypted, password=password,
            output_cb=output_cb, progress_cb=progress_cb,
        )

    def restore(
        self,
        udid: str,
        backup_dir: Path,
        output_cb: Optional[OutputCallback] = None,
        progress_cb: Optional[Callable[[IOSBackupProgress], None]] = None,
    ) -> Tuple[bool, str]:
        """Restore a backup to the device."""
        return self._mgr.restore(
            udid, str(backup_dir),
            output_cb=output_cb, progress_cb=progress_cb,
        )

    # ──────────────────────────────────────────────────────────────
    #  APP MANAGEMENT (libimobiledevice)
    # ──────────────────────────────────────────────────────────────

    def list_apps(self, udid: str) -> List[IOSAppInfo]:
        """List installed apps on the device."""
        return self._mgr.list_apps(udid)

    def install_ipa(
        self,
        udid: str,
        ipa_path: Path,
        output_cb: Optional[OutputCallback] = None,
    ) -> Tuple[bool, str]:
        """Sideload an .ipa file to the device."""
        return self._mgr.install_ipa(udid, str(ipa_path), output_cb)

    def uninstall_app(
        self,
        udid: str,
        bundle_id: str,
        output_cb: Optional[OutputCallback] = None,
    ) -> Tuple[bool, str]:
        """Uninstall an app from the device."""
        return self._mgr.uninstall_app(udid, bundle_id, output_cb)

    # ──────────────────────────────────────────────────────────────
    #  SCREENSHOT
    # ──────────────────────────────────────────────────────────────

    def screenshot(self, udid: str, dest: Path) -> bool:
        """Take a screenshot — prefers agent, falls back to libimobiledevice."""
        # Try agent (faster, no USB required)
        client = self._agent_clients.get(udid)
        if client:
            try:
                resp = client._get("/api/device/screenshot")
                if resp.ok and resp.raw:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(resp.raw)
                    return True
            except Exception:
                pass

        # Fall back to libimobiledevice
        ok, _ = self._mgr.screenshot(udid, str(dest))
        return ok

    # ──────────────────────────────────────────────────────────────
    #  SYSLOG (libimobiledevice)
    # ──────────────────────────────────────────────────────────────

    def start_syslog(
        self,
        udid: str,
        output_cb: OutputCallback,
    ) -> Optional[subprocess.Popen]:
        """Start streaming syslog from the device."""
        return self._mgr.start_syslog(udid, output_cb)

    # ──────────────────────────────────────────────────────────────
    #  PAIRING RECORDS
    # ──────────────────────────────────────────────────────────────

    def list_pairing_records(self) -> List[Dict[str, str]]:
        """List available pairing records (for WiFi re-pairing)."""
        return self._mgr.list_pairing_records()

    def export_pairing_record(self, udid: str, dest: Path) -> Tuple[bool, str]:
        """Export a pairing record (e.g., to copy to Android for WiFi access)."""
        return self._mgr.export_pairing_record(udid, str(dest))

    # ──────────────────────────────────────────────────────────────
    #  TOOL MANAGEMENT
    # ──────────────────────────────────────────────────────────────

    def check_tools(self) -> Dict[str, bool]:
        """Check available libimobiledevice tools."""
        return self._mgr.check_tools()

    def install_tools(
        self,
        output_cb: Optional[OutputCallback] = None,
    ) -> Tuple[bool, str]:
        """Auto-install libimobiledevice tools via system package manager."""
        return self._mgr.install_tools(output_cb)

    # ──────────────────────────────────────────────────────────────
    #  CAPABILITIES SUMMARY
    # ──────────────────────────────────────────────────────────────

    def capabilities(self, udid: str) -> Dict[str, Dict[str, bool]]:
        """
        Returns a summary of available capabilities for a device:
          {
            "agent": { "contacts": True, "photos": True, ... },
            "libimobiledevice": { "backup": True, "apps": True, ... }
          }
        """
        tools = self._mgr.check_tools()
        has_agent = self.has_agent(udid)

        return {
            "agent": {
                "contacts": has_agent,
                "photos": has_agent,
                "files": has_agent,
                "device_info": has_agent,
                "screenshot": has_agent,
                "pairing": has_agent,
            },
            "libimobiledevice": {
                "device_info": tools.get("ideviceinfo", False),
                "backup": tools.get("idevicebackup2", False),
                "restore": tools.get("idevicebackup2", False),
                "apps": tools.get("ideviceinstaller", False),
                "filesystem": tools.get("ifuse", False),
                "screenshot": tools.get("idevicescreenshot", False),
                "syslog": tools.get("idevicesyslog", False),
                "port_forward": tools.get("iproxy", False),
                "pairing": tools.get("idevicepair", False),
            },
        }

    # ──────────────────────────────────────────────────────────────
    #  CLEANUP
    # ──────────────────────────────────────────────────────────────

    def cleanup(self):
        """Clean up all agent connections and iproxy processes."""
        self._agent_clients.clear()
        for proc in self._iproxy_procs.values():
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        self._iproxy_procs.clear()
