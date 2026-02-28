"""
adb_adapter.py - Adapter that wraps the existing ADBCore as a DeviceInterface.

This allows TransferManager and any cross-platform code to use the same
abstract interface for Android devices.
"""

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .adb_core import ADBCore, DeviceInfo
from .device_interface import (
    ContactEntry,
    DeviceInterface,
    DeviceManager,
    DevicePlatform,
    DeviceState,
    SMSEntry,
    UnifiedDeviceInfo,
)

log = logging.getLogger("adb_toolkit.adb_adapter")

# Media paths on Android internal storage
_ANDROID_MEDIA_PATHS = {
    "photos": ["/sdcard/DCIM", "/sdcard/Pictures"],
    "videos": ["/sdcard/Movies", "/sdcard/DCIM"],
    "music": ["/sdcard/Music"],
    "documents": ["/sdcard/Documents", "/sdcard/Download"],
}


def _adb_to_unified(dev: DeviceInfo) -> UnifiedDeviceInfo:
    """Convert legacy DeviceInfo → UnifiedDeviceInfo."""
    state_map = {
        "device": DeviceState.CONNECTED,
        "unauthorized": DeviceState.UNAUTHORIZED,
        "offline": DeviceState.OFFLINE,
        "recovery": DeviceState.RECOVERY,
    }
    return UnifiedDeviceInfo(
        serial=dev.serial,
        platform=DevicePlatform.ANDROID,
        state=state_map.get(dev.state, DeviceState.CONNECTED),
        model=dev.model,
        manufacturer=dev.manufacturer,
        os_version=dev.android_version,
        product=dev.product,
        storage_total=dev.storage_total,
        storage_free=dev.storage_free,
        battery_level=dev.battery_level,
        sdk_version=dev.sdk_version,
    )


class ADBAdapter(DeviceInterface):
    """Makes the existing ADBCore comply with the DeviceInterface ABC."""

    def __init__(self, adb: ADBCore):
        self.adb = adb

    # ---- Platform ------------------------------------------------------
    def platform(self) -> DevicePlatform:
        return DevicePlatform.ANDROID

    # ---- Discovery -----------------------------------------------------
    def list_devices(self) -> List[UnifiedDeviceInfo]:
        return [_adb_to_unified(d) for d in self.adb.list_devices()]

    def get_device_details(self, serial: str) -> UnifiedDeviceInfo:
        return _adb_to_unified(self.adb.get_device_details(serial))

    # ---- File operations -----------------------------------------------
    def pull(self, remote: str, local: str, serial: str) -> bool:
        return self.adb.pull(remote, local, serial)

    def push(self, local: str, remote: str, serial: str) -> bool:
        return self.adb.push(local, remote, serial)

    def list_dir(self, remote_path: str, serial: str) -> List[str]:
        return self.adb.list_dir(remote_path, serial)

    def file_exists(self, remote_path: str, serial: str) -> bool:
        out = self.adb.run_shell(f"[ -e '{remote_path}' ] && echo Y || echo N", serial)
        return out.strip().startswith("Y")

    def mkdir(self, remote_path: str, serial: str) -> bool:
        self.adb.run_shell(f"mkdir -p '{remote_path}'", serial)
        return True

    def delete(self, remote_path: str, serial: str) -> bool:
        self.adb.run_shell(f"rm -rf '{remote_path}'", serial)
        return True

    def stat_file(self, remote_path: str, serial: str) -> Tuple[int, float]:
        out = self.adb.run_shell(f"stat -c '%s %Y' '{remote_path}' 2>/dev/null", serial)
        parts = out.split()
        if len(parts) >= 2:
            try:
                return int(parts[0]), float(parts[1])
            except ValueError:
                pass
        return 0, 0.0

    # ---- Contacts ------------------------------------------------------
    def export_contacts(self, serial: str, out_dir: Path) -> Optional[Path]:
        """Export contacts via content provider → VCF."""
        out_dir.mkdir(parents=True, exist_ok=True)
        vcf_path = out_dir / "contacts.vcf"

        # Strategy 1: content query
        out = self.adb.run_shell(
            "content query --uri content://com.android.contacts/contacts "
            "--projection display_name",
            serial, timeout=30,
        )
        if not out or "Error" in out:
            return None

        # Pull VCF via vcard lookup
        vcard_out = self.adb.run_shell(
            "content query --uri content://com.android.contacts/contacts "
            "--projection lookup --sort 'display_name ASC'",
            serial, timeout=30,
        )
        if not vcard_out:
            return None

        # Build VCF by querying each contact's vcard
        lookup_keys = re.findall(r"lookup=([^\s,}]+)", vcard_out)
        vcards: List[str] = []
        for key in lookup_keys:
            vcard = self.adb.run_shell(
                f"content read --uri content://com.android.contacts/contacts/as_vcard/{key}",
                serial, timeout=10,
            )
            if vcard and "BEGIN:VCARD" in vcard:
                vcards.append(vcard.strip())

        if vcards:
            vcf_path.write_text("\n".join(vcards), encoding="utf-8")
            return vcf_path
        return None

    def import_contacts(self, serial: str, vcf_path: Path) -> bool:
        """Push VCF to device and trigger contact import."""
        remote = "/sdcard/Download/_import_contacts.vcf"
        if not self.adb.push(str(vcf_path), remote, serial):
            return False
        # Open VCF with Contacts app
        self.adb.run_shell(
            f"am start -a android.intent.action.VIEW "
            f"-d 'file://{remote}' -t text/x-vcard",
            serial,
        )
        return True

    # ---- SMS -----------------------------------------------------------
    def export_sms(self, serial: str, out_dir: Path) -> Optional[Path]:
        """Export SMS via content provider → JSON."""
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / "sms.json"

        out = self.adb.run_shell(
            "content query --uri content://sms "
            "--projection address:body:date:type:read:thread_id",
            serial, timeout=60,
        )
        if not out or "Error" in out:
            return None

        import json
        messages: List[dict] = []
        for line in out.splitlines():
            entry: dict = {}
            for pair in re.findall(r"(\w+)=([^,}]*)", line):
                entry[pair[0]] = pair[1].strip()
            if "address" in entry and "body" in entry:
                messages.append(entry)

        if messages:
            json_path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
            return json_path
        return None

    def import_sms(self, serial: str, json_path: Path) -> bool:
        """Import SMS from JSON via content insert."""
        import json as _json
        data = _json.loads(json_path.read_text(encoding="utf-8"))
        success = 0
        for msg in data:
            address = msg.get("address", "")
            body = msg.get("body", "").replace("'", "'\\''")
            date = msg.get("date", "0")
            msg_type = msg.get("type", "1")
            read = msg.get("read", "1")
            try:
                self.adb.run_shell(
                    f"content insert --uri content://sms "
                    f"--bind address:s:{address} "
                    f"--bind body:s:'{body}' "
                    f"--bind date:l:{date} "
                    f"--bind type:i:{msg_type} "
                    f"--bind read:i:{read}",
                    serial, timeout=10,
                )
                success += 1
            except Exception:
                pass
        return success > 0

    # ---- Media paths ---------------------------------------------------
    def get_media_paths(self, serial: str) -> Dict[str, List[str]]:
        return dict(_ANDROID_MEDIA_PATHS)

    # ---- Storage -------------------------------------------------------
    def get_free_bytes(self, serial: str) -> int:
        df_out = self.adb.run_shell("df /data", serial)
        lines = df_out.splitlines()
        if len(lines) >= 2:
            cols = lines[1].split()
            if len(cols) >= 4:
                try:
                    return int(cols[3]) * 1024
                except ValueError:
                    pass
        return -1

    def get_total_bytes(self, serial: str) -> int:
        df_out = self.adb.run_shell("df /data", serial)
        lines = df_out.splitlines()
        if len(lines) >= 2:
            cols = lines[1].split()
            if len(cols) >= 2:
                try:
                    return int(cols[1]) * 1024
                except ValueError:
                    pass
        return -1

    # ---- Shell ---------------------------------------------------------
    def run_shell(self, cmd: str, serial: str, timeout: int = 60) -> str:
        return self.adb.run_shell(cmd, serial, timeout)
