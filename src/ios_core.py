"""
ios_core.py - iOS device communication via pymobiledevice3.

Implements the DeviceInterface ABC so that TransferManager can work
with iPhones / iPads the same way it works with Android devices.

Requirements:
    pip install pymobiledevice3

Notes:
    - pymobiledevice3 requires the Apple Mobile Device service on Windows
      (installed with iTunes) -OR- works natively on macOS / Linux.
    - iOS ≥ 17 requires pairing via "Trust This Computer" + lockdown tunnel.
    - File access is limited to AFC (Apple File Conduit) which exposes the
      media directory and app-specific Documents (if configured).
    - SMS export requires an iTunes-style backup (not encrypted) and then
      parsing the backup's sms.db SQLite file.
    - Contacts export uses the same backup → AddressBook.sqlitedb approach,
      then converts to VCF.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .device_interface import (
    DeviceInterface,
    DevicePlatform,
    DeviceState,
    UnifiedDeviceInfo,
)

log = logging.getLogger("adb_toolkit.ios_core")

# ---------------------------------------------------------------------------
# Lazy imports for pymobiledevice3 — the library is optional
# ---------------------------------------------------------------------------
_PYMOBILE_AVAILABLE = False
_import_error: Optional[str] = None

try:
    from pymobiledevice3.lockdown import create_using_usbmux, LockdownClient
    from pymobiledevice3.services.afc import AfcService
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    from pymobiledevice3.usbmux import list_devices as _usbmux_list
    _PYMOBILE_AVAILABLE = True
except ImportError as exc:
    _import_error = str(exc)
except Exception as exc:
    _import_error = str(exc)


def is_ios_available() -> bool:
    """Return True if pymobiledevice3 is importable."""
    return _PYMOBILE_AVAILABLE


def ios_import_error() -> Optional[str]:
    """Return import error message, or None."""
    return _import_error


# ---------------------------------------------------------------------------
# Helper: iOS backup SMS/Contacts extraction
# ---------------------------------------------------------------------------
def _extract_sms_from_backup(backup_dir: Path) -> List[dict]:
    """Parse sms.db from an iOS backup and return list of SMS dicts."""
    # In an iOS backup, sms.db is stored under a hash name.
    # The file hash for HomeDomain-Library/SMS/sms.db is:
    # 3d0d7e5fb2ce288813306e4d4636395e047a3d28
    sms_hash = "3d0d7e5fb2ce288813306e4d4636395e047a3d28"
    sms_db = backup_dir / sms_hash
    if not sms_db.exists():
        # Try to find it by scanning Manifest.db
        sms_db = _find_backup_file(backup_dir, "Library/SMS/sms.db")
    if not sms_db or not sms_db.exists():
        return []

    messages: List[dict] = []
    try:
        conn = sqlite3.connect(str(sms_db))
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("""
            SELECT
                h.id AS address,
                m.text AS body,
                m.date AS date,
                m.is_from_me AS is_from_me,
                m.is_read AS is_read
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text IS NOT NULL AND m.text != ''
            ORDER BY m.date ASC
        """)
        for row in cursor:
            # iOS stores dates as seconds since 2001-01-01
            # Convert to Unix ms: add 978307200 then * 1000
            date_unix_ms = (row["date"] // 1_000_000_000 + 978307200) * 1000 if row["date"] else 0
            messages.append({
                "address": row["address"] or "",
                "body": row["body"] or "",
                "date": str(date_unix_ms),
                "type": "2" if row["is_from_me"] else "1",
                "read": "1" if row["is_read"] else "0",
            })
        conn.close()
    except Exception as exc:
        log.warning("Failed to parse iOS sms.db: %s", exc)
    return messages


def _extract_contacts_from_backup(backup_dir: Path) -> str:
    """Parse AddressBook.sqlitedb from an iOS backup and return VCF text."""
    # Known hash for HomeDomain-Library/AddressBook/AddressBook.sqlitedb
    ab_hash = "31bb7ba8914766d4ba40d6dfb6113c8b614be442"
    ab_db = backup_dir / ab_hash
    if not ab_db.exists():
        ab_db = _find_backup_file(backup_dir, "Library/AddressBook/AddressBook.sqlitedb")
    if not ab_db or not ab_db.exists():
        return ""

    vcards: List[str] = []
    try:
        conn = sqlite3.connect(str(ab_db))
        conn.row_factory = sqlite3.Row

        # Get all people
        people = conn.execute("""
            SELECT ROWID, First, Last, Organization
            FROM ABPerson
        """).fetchall()

        for person in people:
            pid = person["ROWID"]
            first = person["First"] or ""
            last = person["Last"] or ""
            org = person["Organization"] or ""
            display = f"{first} {last}".strip() or org

            # Get phone numbers and emails
            mvs = conn.execute("""
                SELECT property, value
                FROM ABMultiValue
                WHERE record_id = ?
            """, (pid,)).fetchall()

            phones: List[str] = []
            emails: List[str] = []
            for mv in mvs:
                prop = mv["property"]
                val = mv["value"] or ""
                if prop == 3:   # phone
                    phones.append(val)
                elif prop == 4:  # email
                    emails.append(val)

            vcard = "BEGIN:VCARD\nVERSION:3.0\n"
            vcard += f"N:{last};{first};;;\n"
            vcard += f"FN:{display}\n"
            if org:
                vcard += f"ORG:{org}\n"
            for ph in phones:
                vcard += f"TEL:{ph}\n"
            for em in emails:
                vcard += f"EMAIL:{em}\n"
            vcard += "END:VCARD"
            vcards.append(vcard)

        conn.close()
    except Exception as exc:
        log.warning("Failed to parse iOS AddressBook: %s", exc)
    return "\n".join(vcards)


def _find_backup_file(backup_dir: Path, relative_domain_path: str) -> Optional[Path]:
    """Look up a file in an iOS backup via Manifest.db."""
    manifest = backup_dir / "Manifest.db"
    if not manifest.exists():
        return None
    try:
        conn = sqlite3.connect(str(manifest))
        row = conn.execute(
            "SELECT fileID FROM Files WHERE relativePath = ? LIMIT 1",
            (relative_domain_path,),
        ).fetchone()
        conn.close()
        if row:
            file_id = row[0]
            # iOS backups can store files flat or in subdirectories (first 2 chars)
            candidate = backup_dir / file_id[:2] / file_id
            if candidate.exists():
                return candidate
            candidate = backup_dir / file_id
            if candidate.exists():
                return candidate
    except Exception as exc:
        log.debug("Manifest.db lookup failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# iOS Core — DeviceInterface implementation
# ---------------------------------------------------------------------------
class iOSCore(DeviceInterface):
    """Communicate with iOS devices via pymobiledevice3."""

    def __init__(self, work_dir: Optional[Path] = None):
        if not _PYMOBILE_AVAILABLE:
            raise RuntimeError(
                f"pymobiledevice3 não disponível: {_import_error}\n"
                f"Instale com: pip install pymobiledevice3"
            )
        self.work_dir = work_dir or Path(
            os.environ.get("APPDATA", Path.home())
        ) / "adb-toolkit" / "ios"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._lockdown_cache: Dict[str, "LockdownClient"] = {}

    # ---- Internal helpers ----------------------------------------------

    def _get_lockdown(self, udid: str) -> "LockdownClient":
        """Get or create a LockdownClient for a device."""
        if udid in self._lockdown_cache:
            try:
                # Quick check: still alive?
                self._lockdown_cache[udid].product_type
                return self._lockdown_cache[udid]
            except Exception:
                del self._lockdown_cache[udid]

        lockdown = create_using_usbmux(serial=udid)
        self._lockdown_cache[udid] = lockdown
        return lockdown

    def _get_afc(self, udid: str) -> "AfcService":
        """Get an AFC (Apple File Conduit) service for file operations."""
        lockdown = self._get_lockdown(udid)
        return AfcService(lockdown=lockdown)

    # ---- Platform ------------------------------------------------------
    def platform(self) -> DevicePlatform:
        return DevicePlatform.IOS

    # ---- Discovery -----------------------------------------------------
    def list_devices(self) -> List[UnifiedDeviceInfo]:
        devices: List[UnifiedDeviceInfo] = []
        try:
            for usb_dev in _usbmux_list():
                udid = usb_dev.serial
                try:
                    lockdown = self._get_lockdown(udid)
                    info = UnifiedDeviceInfo(
                        serial=udid,
                        platform=DevicePlatform.IOS,
                        state=DeviceState.CONNECTED,
                        model=lockdown.product_type or "",
                        manufacturer="Apple",
                        os_version=lockdown.product_version or "",
                        product=lockdown.product_type or "",
                        udid=udid,
                        device_class=lockdown.all_values.get("DeviceClass", ""),
                        ios_build=lockdown.all_values.get("BuildVersion", ""),
                    )
                    # Friendly model name
                    marketing = lockdown.all_values.get("MarketingName", "")
                    if marketing:
                        info.model = marketing

                    # Storage info via disk_usage
                    try:
                        usage = lockdown.all_values.get("disk_usage", {})
                        if not usage:
                            # Try AFC for disk info
                            afc = self._get_afc(udid)
                            dev_info = afc.get_device_info()
                            total = int(dev_info.get("FSTotalBytes", 0))
                            free = int(dev_info.get("FSFreeBytes", 0))
                            info.storage_total = total
                            info.storage_free = free
                    except Exception:
                        pass

                    # Battery
                    try:
                        batt = lockdown.all_values.get("BatteryCurrentCapacity", -1)
                        if batt and int(batt) >= 0:
                            info.battery_level = int(batt)
                    except Exception:
                        pass

                    devices.append(info)
                except Exception as exc:
                    log.warning("Could not connect to iOS device %s: %s", udid, exc)
                    devices.append(UnifiedDeviceInfo(
                        serial=udid,
                        platform=DevicePlatform.IOS,
                        state=DeviceState.LOCKED,
                        manufacturer="Apple",
                    ))
        except Exception as exc:
            log.warning("Failed to enumerate iOS devices: %s", exc)
        return devices

    def get_device_details(self, serial: str) -> UnifiedDeviceInfo:
        devices = self.list_devices()
        for d in devices:
            if d.serial == serial:
                return d
        return UnifiedDeviceInfo(serial=serial, platform=DevicePlatform.IOS)

    # ---- File operations (via AFC) ------------------------------------

    def pull(self, remote: str, local: str, serial: str) -> bool:
        try:
            afc = self._get_afc(serial)
            data = afc.get_file_contents(remote)
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            Path(local).write_bytes(data)
            return True
        except Exception as exc:
            log.debug("iOS pull %s failed: %s", remote, exc)
            return False

    def push(self, local: str, remote: str, serial: str) -> bool:
        try:
            afc = self._get_afc(serial)
            data = Path(local).read_bytes()
            # Ensure parent directory exists
            parent = "/".join(remote.split("/")[:-1])
            if parent:
                try:
                    afc.makedirs(parent)
                except Exception:
                    pass
            afc.set_file_contents(remote, data)
            return True
        except Exception as exc:
            log.debug("iOS push to %s failed: %s", remote, exc)
            return False

    def list_dir(self, remote_path: str, serial: str) -> List[str]:
        try:
            afc = self._get_afc(serial)
            return [
                e for e in afc.listdir(remote_path)
                if e not in (".", "..")
            ]
        except Exception:
            return []

    def file_exists(self, remote_path: str, serial: str) -> bool:
        try:
            afc = self._get_afc(serial)
            afc.stat(remote_path)
            return True
        except Exception:
            return False

    def mkdir(self, remote_path: str, serial: str) -> bool:
        try:
            afc = self._get_afc(serial)
            afc.makedirs(remote_path)
            return True
        except Exception:
            return False

    def delete(self, remote_path: str, serial: str) -> bool:
        try:
            afc = self._get_afc(serial)
            afc.rm(remote_path)
            return True
        except Exception:
            return False

    def stat_file(self, remote_path: str, serial: str) -> Tuple[int, float]:
        try:
            afc = self._get_afc(serial)
            info = afc.stat(remote_path)
            size = int(info.get("st_size", 0))
            mtime = float(info.get("st_mtime", 0)) / 1e9  # ns → s
            return size, mtime
        except Exception:
            return 0, 0.0

    # ---- Contacts (via backup extraction) ------------------------------

    def export_contacts(self, serial: str, out_dir: Path) -> Optional[Path]:
        """Export contacts by creating a local backup and parsing AddressBook."""
        out_dir.mkdir(parents=True, exist_ok=True)
        vcf_path = out_dir / "contacts.vcf"

        backup_dir = self._create_backup(serial)
        if not backup_dir:
            return None

        vcf_text = _extract_contacts_from_backup(backup_dir)
        if vcf_text:
            vcf_path.write_text(vcf_text, encoding="utf-8")
            return vcf_path
        return None

    def import_contacts(self, serial: str, vcf_path: Path) -> bool:
        """Push VCF to iOS via AFC to Documents, then user completes import."""
        # Push to a location the user can access
        remote = "/Downloads/imported_contacts.vcf"
        return self.push(str(vcf_path), remote, serial)

    # ---- SMS -----------------------------------------------------------

    def export_sms(self, serial: str, out_dir: Path) -> Optional[Path]:
        """Export SMS by creating a backup and parsing sms.db."""
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / "sms.json"

        backup_dir = self._create_backup(serial)
        if not backup_dir:
            return None

        messages = _extract_sms_from_backup(backup_dir)
        if messages:
            json_path.write_text(
                json.dumps(messages, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return json_path
        return None

    def import_sms(self, serial: str, json_path: Path) -> bool:
        """SMS import on iOS is extremely limited.

        There is no public API to insert SMS. This method pushes the JSON
        for reference but cannot programmatically inject messages.
        """
        log.warning(
            "iOS does not support programmatic SMS import. "
            "Messages saved for reference only."
        )
        remote = "/Downloads/sms_import.json"
        return self.push(str(json_path), remote, serial)

    # ---- Media paths ---------------------------------------------------

    def get_media_paths(self, serial: str) -> Dict[str, List[str]]:
        """AFC-accessible media paths on iOS.

        AFC exposes /DCIM (photos/videos), /Downloads, /Books, etc.
        """
        return {
            "photos": ["/DCIM"],
            "videos": ["/DCIM"],
            "music": ["/iTunes_Control/Music", "/Music"],
            "documents": ["/Downloads", "/Books"],
        }

    # ---- Storage -------------------------------------------------------

    def get_free_bytes(self, serial: str) -> int:
        try:
            afc = self._get_afc(serial)
            info = afc.get_device_info()
            return int(info.get("FSFreeBytes", -1))
        except Exception:
            return -1

    def get_total_bytes(self, serial: str) -> int:
        try:
            afc = self._get_afc(serial)
            info = afc.get_device_info()
            return int(info.get("FSTotalBytes", -1))
        except Exception:
            return -1

    # ---- Backup helper -------------------------------------------------

    def _create_backup(self, udid: str) -> Optional[Path]:
        """Create a non-encrypted iTunes-style backup for data extraction.

        This is used to extract contacts and SMS which are not directly
        accessible via AFC.
        """
        try:
            from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

            backup_dir = self.work_dir / "backups" / udid
            backup_dir.mkdir(parents=True, exist_ok=True)

            lockdown = self._get_lockdown(udid)
            backup_svc = Mobilebackup2Service(lockdown=lockdown)
            backup_svc.backup(
                full=False,
                backup_directory=str(backup_dir),
            )
            return backup_dir
        except ImportError:
            log.error("pymobiledevice3 backup service not available")
            return None
        except Exception as exc:
            log.error("iOS backup failed for %s: %s", udid, exc)
            return None
