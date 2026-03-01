"""
agent_bridge.py — Bridge between existing toolkit managers and the Agent API.

When the Agent is connected on a device, the bridge provides accelerated
alternatives to the standard ADB-based backup/restore/transfer operations.

The GUI can check ``is_agent_available(serial)`` and, if True, route operations
through the agent's ContentResolver / direct-filesystem APIs for:
 - Much faster contact export (direct VCF via ContentResolver vs adb content query)
 - SMS export without root
 - File transfer over TCP (256 KB buffer) instead of adb pull/push
 - App-data backup without adb backup (which is deprecated on SDK 31+)
 - Direct DCIM/media listing without slow adb shell find

Usage:
    from .agent_bridge import AgentBridge

    bridge = AgentBridge(agent_mgr)
    if bridge.is_available(serial):
        contacts_vcf = bridge.export_contacts(serial, dest_path)
        sms_json = bridge.export_sms(serial, dest_path)
        bridge.pull_file(serial, "/sdcard/DCIM", local_dir)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("adb_toolkit.agent_bridge")


class AgentBridge:
    """
    Provides agent-accelerated versions of common toolkit operations.

    Falls back gracefully — callers should always check ``is_available()``
    before using bridge methods, or simply use the standard manager paths.
    """

    def __init__(self, agent_mgr):
        """
        Args:
            agent_mgr: An ``AgentManager`` instance from agent_manager.py
        """
        self._mgr = agent_mgr

    def is_available(self, serial: str) -> bool:
        """Check if the agent is connected and responding on *serial*."""
        client = self._mgr.get_client(serial)
        if not client:
            return False
        try:
            resp = client.ping()
            return resp.ok
        except Exception:
            return False

    def get_client(self, serial: str):
        """Get the underlying ``AgentClient`` for direct API access."""
        return self._mgr.get_client(serial)

    # ──────────────────────────────────────────────────────────────────
    #  CONTACTS
    # ──────────────────────────────────────────────────────────────────

    def export_contacts_vcf(self, serial: str, dest: Path) -> Optional[Path]:
        """
        Export all contacts to a VCF file using the agent's ContactsApi.

        Much faster and more reliable than ``adb content query``.
        Returns the path to the saved VCF file, or None on failure.
        """
        client = self._mgr.get_client(serial)
        if not client:
            return None
        try:
            resp = client.contacts.export_vcf()
            if resp.ok and resp.raw:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(resp.raw)
                log.info("Exported %d bytes of contacts VCF via agent", len(resp.raw))
                return dest
            elif resp.ok and resp.data:
                # VCF content might be in data
                vcf_text = resp.data.get("vcf", "")
                if vcf_text:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(vcf_text, encoding="utf-8")
                    return dest
        except Exception as exc:
            log.warning("Agent contact export failed: %s", exc)
        return None

    def list_contacts(self, serial: str) -> List[Dict[str, Any]]:
        """List all contacts via agent."""
        client = self._mgr.get_client(serial)
        if not client:
            return []
        try:
            resp = client.contacts.list()
            if resp.ok and isinstance(resp.data, list):
                return resp.data
        except Exception as exc:
            log.warning("Agent contact list failed: %s", exc)
        return []

    def contact_count(self, serial: str) -> int:
        """Get contact count via agent."""
        client = self._mgr.get_client(serial)
        if not client:
            return -1
        try:
            resp = client.contacts.count()
            if resp.ok:
                return resp.get("count", 0)
        except Exception:
            pass
        return -1

    # ──────────────────────────────────────────────────────────────────
    #  SMS
    # ──────────────────────────────────────────────────────────────────

    def export_sms(self, serial: str, dest: Path) -> Optional[Path]:
        """
        Export all SMS messages to a JSON file via the agent.

        Works without root and without ``adb backup`` (deprecated on SDK 31+).
        """
        client = self._mgr.get_client(serial)
        if not client:
            return None
        try:
            resp = client.sms.export()
            if resp.ok and resp.data:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(
                    json.dumps(resp.data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                count = len(resp.data) if isinstance(resp.data, list) else resp.get("count", "?")
                log.info("Exported %s SMS messages via agent", count)
                return dest
        except Exception as exc:
            log.warning("Agent SMS export failed: %s", exc)
        return None

    def sms_count(self, serial: str) -> int:
        """Get SMS count via agent."""
        client = self._mgr.get_client(serial)
        if not client:
            return -1
        try:
            resp = client.sms.count()
            if resp.ok:
                return resp.get("count", 0)
        except Exception:
            pass
        return -1

    # ──────────────────────────────────────────────────────────────────
    #  FILES (high-speed TCP)
    # ──────────────────────────────────────────────────────────────────

    def pull_file(self, serial: str, remote_path: str, local_path: Path) -> bool:
        """
        Pull a file from device via the agent TCP transfer.

        Uses 256 KB buffer + SHA-256 verification — significantly faster
        than ``adb pull`` for large files.
        """
        client = self._mgr.get_client(serial)
        if not client:
            return False
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            client.pull(remote_path, str(local_path))
            log.info("Pulled %s via agent TCP", remote_path)
            return True
        except Exception as exc:
            log.warning("Agent pull failed for %s: %s", remote_path, exc)
            return False

    def push_file(self, serial: str, local_path: Path, remote_path: str) -> bool:
        """Push a file to device via the agent TCP transfer."""
        client = self._mgr.get_client(serial)
        if not client:
            return False
        try:
            client.push(str(local_path), remote_path)
            log.info("Pushed %s via agent TCP", local_path.name)
            return True
        except Exception as exc:
            log.warning("Agent push failed for %s: %s", local_path.name, exc)
            return False

    def list_files(self, serial: str, remote_path: str) -> List[Dict[str, Any]]:
        """List files on device via agent (with metadata)."""
        client = self._mgr.get_client(serial)
        if not client:
            return []
        try:
            resp = client.files.list(remote_path)
            if resp.ok and isinstance(resp.data, list):
                return resp.data
        except Exception:
            pass
        return []

    def file_exists(self, serial: str, remote_path: str) -> bool:
        """Check if a file exists on device."""
        client = self._mgr.get_client(serial)
        if not client:
            return False
        try:
            resp = client.files.exists(remote_path)
            return resp.ok and resp.get("exists", False)
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────────────
    #  APPS
    # ──────────────────────────────────────────────────────────────────

    def list_apps(self, serial: str) -> List[Dict[str, Any]]:
        """List installed apps with details via agent."""
        client = self._mgr.get_client(serial)
        if not client:
            return []
        try:
            resp = client.apps.list()
            if resp.ok and isinstance(resp.data, list):
                return resp.data
        except Exception:
            pass
        return []

    def download_apk(self, serial: str, package: str, dest: Path) -> bool:
        """Download an app's APK from device via agent."""
        client = self._mgr.get_client(serial)
        if not client:
            return False
        try:
            resp = client.apps.download(package)
            if resp.ok and resp.raw:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(resp.raw)
                return True
        except Exception as exc:
            log.warning("Agent APK download failed for %s: %s", package, exc)
        return False

    # ──────────────────────────────────────────────────────────────────
    #  DEVICE INFO
    # ──────────────────────────────────────────────────────────────────

    def device_info(self, serial: str) -> Dict[str, Any]:
        """Get comprehensive device info via agent."""
        client = self._mgr.get_client(serial)
        if not client:
            return {}
        try:
            resp = client.device.info()
            if resp.ok:
                return resp.data or {}
        except Exception:
            pass
        return {}

    def screenshot(self, serial: str, dest: Path) -> bool:
        """Take a screenshot via agent."""
        client = self._mgr.get_client(serial)
        if not client:
            return False
        try:
            resp = client.device.screenshot()
            if resp.ok and resp.raw:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(resp.raw)
                return True
        except Exception:
            pass
        return False

    # ──────────────────────────────────────────────────────────────────
    #  SHELL
    # ──────────────────────────────────────────────────────────────────

    def shell_exec(self, serial: str, command: str) -> Optional[str]:
        """Execute a shell command via agent."""
        client = self._mgr.get_client(serial)
        if not client:
            return None
        try:
            resp = client.shell.exec(command)
            if resp.ok:
                return resp.get("output", "")
        except Exception:
            pass
        return None
