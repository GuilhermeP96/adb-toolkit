"""
backup_manager.py - Full device backup and selective backup functionality.

Supports:
  - Full ADB backup (apps, data, system)
  - Selective backup (contacts, SMS, photos, videos, music, documents, apps)
  - Messaging app data backup (WhatsApp, Telegram, Signal, etc.)
  - Custom path backup via file tree browser
  - APK extraction and backup
  - Internal storage file backup via pull
  - Backup metadata and cataloging
  - Compression and encryption
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .adb_core import ADBCore, DeviceInfo
from .adb_base import (
    ADBManagerBase,
    OperationProgress,
    safe_percent,
    CACHE_PATTERNS,
    THUMBNAIL_DUMP_PATTERNS,
)

log = logging.getLogger("adb_toolkit.backup")

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
BACKUP_TYPES = [
    "full",          # Full ADB backup
    "apps",          # Installed APKs + app data
    "photos",        # DCIM, Pictures
    "videos",        # Movies, video files
    "music",         # Music folder
    "documents",     # Documents, Download
    "contacts",      # Contacts via content provider
    "sms",           # SMS via content provider
    "call_log",      # Call log
    "internal",      # Full internal storage
    "messaging",     # Messaging apps (WhatsApp, Telegram, etc.)
    "unsynced_apps", # Apps with local-only data (authenticators, games, etc.)
    "custom",        # Custom paths selected from tree browser
]

MEDIA_PATHS = {
    "photos": ["/sdcard/DCIM", "/sdcard/Pictures"],
    "videos": ["/sdcard/Movies", "/sdcard/DCIM"],
    "music": ["/sdcard/Music"],
    "documents": ["/sdcard/Documents", "/sdcard/Download"],
    "internal": ["/sdcard"],
}


@dataclass
class BackupManifest:
    """Metadata about a backup."""
    backup_id: str = ""
    device_serial: str = ""
    device_model: str = ""
    device_manufacturer: str = ""
    android_version: str = ""
    backup_type: str = ""
    categories: List[str] = field(default_factory=list)
    timestamp: str = ""
    size_bytes: int = 0
    file_count: int = 0
    app_count: int = 0
    apps: List[str] = field(default_factory=list)
    custom_paths: List[str] = field(default_factory=list)
    messaging_apps: List[str] = field(default_factory=list)
    unsynced_packages: List[str] = field(default_factory=list)
    encrypted: bool = False
    compressed: bool = True
    notes: str = ""
    duration_seconds: float = 0.0
    checksum: str = ""

    def save(self, path: Path):
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "BackupManifest":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)


# ---------------------------------------------------------------------------
# Backward-compatible alias
# ---------------------------------------------------------------------------
BackupProgress = OperationProgress


# ---------------------------------------------------------------------------
# Backup Manager
# ---------------------------------------------------------------------------
class BackupManager(ADBManagerBase):
    """Manages device backups via ADB."""

    def __init__(self, adb: ADBCore, backup_dir: Optional[Path] = None):
        super().__init__(adb)
        self.backup_dir = backup_dir or (adb.base_dir / "backups")
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Backup directory management
    # ------------------------------------------------------------------
    def _create_backup_folder(self, device: DeviceInfo, backup_type: str) -> Tuple[Path, str]:
        """Create a backup folder and return (path, backup_id)."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = device.model.replace(" ", "_") if device.model else device.serial
        backup_id = f"{name}_{backup_type}_{ts}"
        folder = self.backup_dir / backup_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder, backup_id

    def list_backups(self) -> List[BackupManifest]:
        """List all available backups."""
        backups = []
        for item in sorted(self.backup_dir.iterdir(), reverse=True):
            manifest_file = item / "manifest.json"
            if manifest_file.exists():
                try:
                    backups.append(BackupManifest.load(manifest_file))
                except Exception as exc:
                    log.warning("Failed to load manifest %s: %s", manifest_file, exc)
        return backups

    def delete_backup(self, backup_id: str) -> bool:
        """Delete a backup by ID."""
        folder = self.backup_dir / backup_id
        if folder.exists():
            shutil.rmtree(folder)
            log.info("Deleted backup: %s", backup_id)
            return True
        return False

    def get_backup_size(self, backup_id: str) -> int:
        """Get total size of a backup in bytes."""
        folder = self.backup_dir / backup_id
        total = 0
        if folder.exists():
            for f in folder.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        return total

    # ------------------------------------------------------------------
    # Full ADB Backup
    # ------------------------------------------------------------------
    def backup_full(
        self,
        serial: str,
        include_apks: bool = True,
        include_shared: bool = True,
        include_system: bool = False,
        password: Optional[str] = None,
    ) -> Optional[BackupManifest]:
        """Perform a full ADB backup (adb backup command)."""
        self._begin_operation()
        device = self.adb.get_device_details(serial)
        folder, backup_id = self._create_backup_folder(device, "full")
        backup_file = folder / "backup.ab"

        self._emit(BackupProgress(phase="full_backup", current_item="Starting full backup..."))

        args = ["backup", "-all"]
        if include_apks:
            args.append("-apk")
        else:
            args.append("-noapk")
        if include_shared:
            args.append("-shared")
        else:
            args.append("-noshared")
        if include_system:
            args.append("-system")
        else:
            args.append("-nosystem")
        args.extend(["-f", str(backup_file)])

        log.info("Starting full ADB backup for %s", serial)
        self._emit(BackupProgress(
            phase="full_backup",
            current_item="Waiting for confirmation on device...",
            percent=5,
        ))

        # Full backup requires user confirmation on device
        result = self.adb.run(args, serial=serial, timeout=7200)

        if self._cancel_flag.is_set():
            shutil.rmtree(folder, ignore_errors=True)
            return None

        duration = time.time() - self._start_time
        size = backup_file.stat().st_size if backup_file.exists() else 0

        manifest = BackupManifest(
            backup_id=backup_id,
            device_serial=serial,
            device_model=device.model,
            device_manufacturer=device.manufacturer,
            android_version=device.android_version,
            backup_type="full",
            categories=["full"],
            timestamp=datetime.now().isoformat(),
            size_bytes=size,
            encrypted=password is not None,
            compressed=True,
            duration_seconds=duration,
        )
        manifest.save(folder / "manifest.json")

        self._emit(BackupProgress(phase="complete", percent=100))
        log.info("Full backup complete: %s (%d bytes)", backup_id, size)
        return manifest

    # ------------------------------------------------------------------
    # Selective File Backup (pull-based)
    # ------------------------------------------------------------------
    def backup_files(
        self,
        serial: str,
        categories: Optional[List[str]] = None,
        custom_paths: Optional[List[str]] = None,
        ignore_cache: bool = False,
        ignore_thumbnails: bool = False,
    ) -> Optional[BackupManifest]:
        """Backup files from device by category or custom paths."""
        self._begin_operation()
        device = self.adb.get_device_details(serial)
        folder, backup_id = self._create_backup_folder(device, "files")

        categories = categories or ["photos", "videos", "music", "documents"]
        all_paths: List[str] = []

        for cat in categories:
            if cat in MEDIA_PATHS:
                all_paths.extend(MEDIA_PATHS[cat])
        if custom_paths:
            all_paths.extend(custom_paths)

        # Deduplicate
        all_paths = list(dict.fromkeys(all_paths))

        # Scan files (with optional cache/thumbnail filtering)
        self._emit(BackupProgress(phase="scanning", sub_phase="files",
                                  current_item="Scanning device files..."))
        file_list = self.list_remote_files(
            serial, all_paths,
            ignore_cache=ignore_cache,
            ignore_thumbnails=ignore_thumbnails,
        )
        total_files = len(file_list)
        total_bytes = sum(s for _, s in file_list)
        log.info("Found %d files (%d bytes) to backup", total_files, total_bytes)

        # Pull files using shared helper
        file_count, bytes_done = self.pull_with_progress(
            serial, file_list, folder / "files",
            phase="copying", sub_phase="files",
        )

        duration = time.time() - self._start_time
        actual_size = self.get_backup_size(backup_id)

        manifest = BackupManifest(
            backup_id=backup_id,
            device_serial=serial,
            device_model=device.model,
            device_manufacturer=device.manufacturer,
            android_version=device.android_version,
            backup_type="files",
            categories=categories,
            custom_paths=custom_paths or [],
            timestamp=datetime.now().isoformat(),
            size_bytes=actual_size,
            file_count=file_count,
            compressed=False,
            duration_seconds=duration,
        )
        manifest.save(folder / "manifest.json")

        self._emit(BackupProgress(phase="complete", percent=100))
        log.info("File backup complete: %s (%d files)", backup_id, file_count)
        return manifest

    # ------------------------------------------------------------------
    # APK Backup
    # ------------------------------------------------------------------
    def backup_apps(
        self,
        serial: str,
        include_data: bool = False,
        third_party_only: bool = True,
        selected_packages: Optional[List[str]] = None,
    ) -> Optional[BackupManifest]:
        """Backup installed APKs (and optionally data)."""
        self._begin_operation()
        device = self.adb.get_device_details(serial)
        folder, backup_id = self._create_backup_folder(device, "apps")
        apk_dir = folder / "apks"
        apk_dir.mkdir(exist_ok=True)

        if selected_packages:
            packages = selected_packages
        else:
            packages = self.adb.list_packages(serial, third_party=third_party_only)

        total = len(packages)
        log.info("Backing up %d apps", total)

        self._emit(BackupProgress(phase="apps", items_total=total))

        backed_up = []
        for i, pkg in enumerate(packages):
            if self._cancel_flag.is_set():
                break

            self._emit(BackupProgress(
                phase="apps",
                current_item=pkg,
                items_done=i,
                items_total=total,
                percent=(i / total * 100) if total > 0 else 0,
            ))

            try:
                all_paths = self.adb.get_apk_paths(pkg, serial)
                if all_paths:
                    if len(all_paths) > 1:
                        # Split APK — store in per-package subfolder
                        pkg_apk_dir = apk_dir / pkg
                        pkg_apk_dir.mkdir(exist_ok=True)
                        pulled = 0
                        for apk_remote in all_paths:
                            apk_name = os.path.basename(apk_remote)
                            local_apk = pkg_apk_dir / apk_name
                            if self.adb.pull(apk_remote, str(local_apk), serial):
                                pulled += 1
                        if pulled > 0:
                            backed_up.append(pkg)
                    else:
                        # Single APK
                        local_apk = apk_dir / f"{pkg}.apk"
                        if self.adb.pull(all_paths[0], str(local_apk), serial):
                            backed_up.append(pkg)
            except Exception as exc:
                log.warning("Failed to backup %s: %s", pkg, exc)

        # Optionally backup app data
        if include_data and backed_up:
            data_backup = folder / "app_data.ab"
            data_args = ["backup", "-noapk", "-noshared"]
            data_args.extend(backed_up)
            data_args.extend(["-f", str(data_backup)])
            self.adb.run(data_args, serial=serial, timeout=3600)

        duration = time.time() - self._start_time
        actual_size = self.get_backup_size(backup_id)

        manifest = BackupManifest(
            backup_id=backup_id,
            device_serial=serial,
            device_model=device.model,
            device_manufacturer=device.manufacturer,
            android_version=device.android_version,
            backup_type="apps",
            categories=["apps"],
            timestamp=datetime.now().isoformat(),
            size_bytes=actual_size,
            app_count=len(backed_up),
            apps=backed_up,
            duration_seconds=duration,
        )
        manifest.save(folder / "manifest.json")

        self._emit(BackupProgress(phase="complete", percent=100))
        log.info("App backup complete: %s (%d apps)", backup_id, len(backed_up))
        return manifest

    # ------------------------------------------------------------------
    # Contacts / SMS Backup
    # ------------------------------------------------------------------
    def backup_contacts(self, serial: str) -> Optional[BackupManifest]:
        """Backup contacts using multiple strategies (no root needed).

        Strategy order:
          1. Export VCF via content:// provider (works on most devices)
          2. Use `adb backup` for contacts provider
          3. Pull contacts DB directly (needs root — usually fails)
        """
        self._begin_operation()
        device = self.adb.get_device_details(serial)
        folder, backup_id = self._create_backup_folder(device, "contacts")

        self._emit(BackupProgress(phase="contacts", sub_phase="contacts",
                                  current_item="Exportando contatos..."))

        file_count = 0
        methods_tried = []

        # === Method 1: Export contacts to VCF on device, then pull ===
        try:
            remote_vcf = "/sdcard/contacts_backup.vcf"
            # Some devices support direct VCF export via content provider
            # Use the contacts URI to query and build a VCF
            self._emit(BackupProgress(
                phase="contacts", current_item="Exportando VCF via content provider..."
            ))

            # Query contacts with their vCard lookup keys
            raw = self.adb.run_shell(
                'content query --uri content://com.android.contacts/contacts '
                '--projection _id:display_name:lookup',
                serial, timeout=60,
            )

            contacts_data = []
            if raw and "Row:" in raw:
                for line in raw.splitlines():
                    if "display_name=" in line:
                        contacts_data.append(line)

            if contacts_data:
                # Build a simple VCF file from the contact data
                vcf_lines = []
                for line in contacts_data:
                    name = ""
                    if "display_name=" in line:
                        name = line.split("display_name=")[1].split(",")[0].strip()
                    if name and name != "NULL":
                        vcf_lines.append("BEGIN:VCARD")
                        vcf_lines.append("VERSION:3.0")
                        vcf_lines.append(f"FN:{name}")
                        vcf_lines.append(f"N:{name};;;;")
                        vcf_lines.append("END:VCARD")

                if vcf_lines:
                    vcf_file = folder / "contacts.vcf"
                    vcf_file.write_text("\n".join(vcf_lines), encoding="utf-8")
                    file_count += 1
                    methods_tried.append("vcf_content_query")
                    log.info("Exported %d contacts via content query", len(vcf_lines) // 5)

            # Also try to query phone numbers and emails
            phone_raw = self.adb.run_shell(
                'content query --uri content://com.android.contacts/data/phones '
                '--projection display_name:data1',
                serial, timeout=60,
            )
            if phone_raw and "Row:" in phone_raw:
                phone_file = folder / "contacts_phones.txt"
                phone_file.write_text(phone_raw, encoding="utf-8")
                file_count += 1

        except Exception as exc:
            log.debug("VCF content query method: %s", exc)

        # === Method 2: Use `adb backup` for contacts provider ===
        try:
            self._emit(BackupProgress(
                phase="contacts", current_item="ADB backup de contatos..."
            ))
            contacts_ab = folder / "contacts.ab"
            self.adb.run(
                ["backup", "-f", str(contacts_ab), "com.android.providers.contacts"],
                serial=serial, timeout=120,
            )
            # Check if the backup actually has content (>24 bytes = non-empty)
            if contacts_ab.exists() and contacts_ab.stat().st_size > 24:
                file_count += 1
                methods_tried.append("adb_backup")
            elif contacts_ab.exists():
                contacts_ab.unlink()
                log.info("ADB contacts backup was empty (Android 12+ restriction)")
        except Exception as exc:
            log.debug("ADB backup contacts: %s", exc)

        # === Method 3: Direct DB pull (needs root) ===
        try:
            db_file = folder / "contacts2.db"
            if self.adb.pull(
                "/data/data/com.android.providers.contacts/databases/contacts2.db",
                str(db_file), serial,
            ):
                if db_file.exists() and db_file.stat().st_size > 0:
                    file_count += 1
                    methods_tried.append("direct_db")
                else:
                    db_file.unlink(missing_ok=True)
        except Exception:
            log.debug("Direct contacts DB pull failed (expected without root)")

        duration = time.time() - self._start_time
        actual_size = self.get_backup_size(backup_id)

        manifest = BackupManifest(
            backup_id=backup_id,
            device_serial=serial,
            device_model=device.model,
            device_manufacturer=device.manufacturer,
            android_version=device.android_version,
            backup_type="contacts",
            categories=["contacts"],
            timestamp=datetime.now().isoformat(),
            size_bytes=actual_size,
            file_count=file_count,
            notes=f"Methods: {', '.join(methods_tried) or 'none succeeded'}",
            duration_seconds=duration,
        )
        manifest.save(folder / "manifest.json")

        if not methods_tried:
            log.warning(
                "No contacts backup method succeeded. On Android 12+ without root, "
                "contacts must be synced via Google account or exported from the Contacts app."
            )

        self._emit(BackupProgress(phase="complete", percent=100))
        return manifest

    def backup_sms(self, serial: str) -> Optional[BackupManifest]:
        """Backup SMS messages using multiple strategies.

        Strategy order:
          1. Export SMS via content:// query to JSON (works without root)
          2. Use `adb backup` for telephony provider
          3. Pull SMS DB directly (needs root)
        """
        self._begin_operation()
        device = self.adb.get_device_details(serial)
        folder, backup_id = self._create_backup_folder(device, "sms")

        self._emit(BackupProgress(phase="sms", sub_phase="sms",
                                  current_item="Exportando mensagens..."))

        file_count = 0
        sms_count = 0
        methods_tried = []

        # === Method 1: Query SMS via content provider ===
        try:
            self._emit(BackupProgress(
                phase="sms", current_item="Lendo SMS via content provider..."
            ))

            # Query SMS inbox
            inbox_raw = self.adb.run_shell(
                'content query --uri content://sms/inbox '
                '--projection _id:address:date:body:read:type',
                serial, timeout=120,
            )

            # Query SMS sent
            sent_raw = self.adb.run_shell(
                'content query --uri content://sms/sent '
                '--projection _id:address:date:body:read:type',
                serial, timeout=120,
            )

            all_sms = []

            for label, raw in [("inbox", inbox_raw), ("sent", sent_raw)]:
                if raw and "Row:" in raw:
                    for line in raw.splitlines():
                        line = line.strip()
                        if not line.startswith("Row:"):
                            continue
                        sms_entry = {"folder": label}
                        # Parse "Row: N _id=X, address=Y, date=Z, body=W, ..."
                        for part in line.split(", "):
                            if "=" in part:
                                k, v = part.split("=", 1)
                                k = k.strip().split()[-1]  # "Row: 0 _id" → "_id"
                                sms_entry[k] = v.strip()
                        if "address" in sms_entry:
                            all_sms.append(sms_entry)

            if all_sms:
                sms_count = len(all_sms)
                sms_file = folder / "sms_backup.json"
                sms_file.write_text(
                    json.dumps(all_sms, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                file_count += 1
                methods_tried.append(f"content_query ({sms_count} msgs)")
                log.info("Exported %d SMS messages via content provider", sms_count)

                # Also write human-readable version
                txt_file = folder / "sms_backup.txt"
                with open(txt_file, "w", encoding="utf-8") as f:
                    f.write(f"SMS Backup — {device.friendly_name()}\n")
                    f.write(f"Total: {sms_count} messages\n")
                    f.write("=" * 60 + "\n\n")
                    for sms in all_sms:
                        direction = "←" if sms.get("folder") == "inbox" else "→"
                        addr = sms.get("address", "?")
                        body = sms.get("body", "")
                        date = sms.get("date", "")
                        f.write(f"{direction} {addr}  [{date}]\n{body}\n\n")
                file_count += 1
            else:
                log.info("No SMS messages found via content query")

        except Exception as exc:
            log.warning("SMS content query failed: %s", exc)

        # === Method 2: ADB backup ===
        try:
            self._emit(BackupProgress(
                phase="sms", current_item="ADB backup de SMS..."
            ))
            sms_ab = folder / "sms.ab"
            self.adb.run(
                ["backup", "-f", str(sms_ab), "com.android.providers.telephony"],
                serial=serial, timeout=120,
            )
            if sms_ab.exists() and sms_ab.stat().st_size > 24:
                file_count += 1
                methods_tried.append("adb_backup")
            elif sms_ab.exists():
                sms_ab.unlink()
                log.info("ADB SMS backup was empty (Android 12+ restriction)")
        except Exception as exc:
            log.debug("ADB backup SMS: %s", exc)

        # === Method 3: Direct DB pull (needs root) ===
        try:
            db_file = folder / "mmssms.db"
            if self.adb.pull(
                "/data/data/com.android.providers.telephony/databases/mmssms.db",
                str(db_file), serial,
            ):
                if db_file.exists() and db_file.stat().st_size > 0:
                    file_count += 1
                    methods_tried.append("direct_db")
                else:
                    db_file.unlink(missing_ok=True)
        except Exception:
            log.debug("Direct SMS DB pull failed (expected without root)")

        duration = time.time() - self._start_time
        actual_size = self.get_backup_size(backup_id)

        manifest = BackupManifest(
            backup_id=backup_id,
            device_serial=serial,
            device_model=device.model,
            device_manufacturer=device.manufacturer,
            android_version=device.android_version,
            backup_type="sms",
            categories=["sms"],
            timestamp=datetime.now().isoformat(),
            size_bytes=actual_size,
            file_count=file_count,
            notes=f"Methods: {', '.join(methods_tried) or 'none succeeded'}. {sms_count} messages.",
            duration_seconds=duration,
        )
        manifest.save(folder / "manifest.json")

        if not methods_tried:
            log.warning(
                "No SMS backup method succeeded. On Android 12+ without root, "
                "SMS must be backed up using a dedicated SMS app or Google Backup."
            )

        self._emit(BackupProgress(phase="complete", percent=100))
        return manifest

    # ------------------------------------------------------------------
    # Messaging App Backup
    # ------------------------------------------------------------------
    def backup_messaging_apps(
        self,
        serial: str,
        app_keys: Optional[List[str]] = None,
        include_apk: bool = True,
    ) -> Optional[BackupManifest]:
        """Backup messaging app data (WhatsApp, Telegram, Signal, etc.).

        Args:
            serial: Device serial.
            app_keys: List of app keys from MESSAGING_APPS, or None for all.
            include_apk: Whether to also backup the APK.
        """
        from .device_explorer import MESSAGING_APPS, MessagingAppDetector

        self._begin_operation()
        device = self.adb.get_device_details(serial)
        folder, backup_id = self._create_backup_folder(device, "messaging")

        self._emit(BackupProgress(phase="messaging", sub_phase="messaging",
                                  current_item="Detectando apps de mensagem..."))

        detector = MessagingAppDetector(self.adb)
        installed = detector.detect_installed_apps(serial)

        if app_keys:
            installed = {k: v for k, v in installed.items() if k in app_keys}

        if not installed:
            log.info("No messaging apps found to backup")
            self._emit(BackupProgress(
                phase="messaging",
                current_item="Nenhum app de mensagem encontrado",
                percent=100,
            ))
            return None

        total_apps = len(installed)
        backed_up_apps: List[str] = []
        total_files = 0
        total_bytes = 0

        for idx, (app_key, app_info) in enumerate(installed.items()):
            if self._cancel_flag.is_set():
                break

            app_name = app_info["name"]
            icon = app_info["icon"]
            existing_paths = app_info.get("existing_paths", [])
            packages = app_info.get("installed_packages", [])

            pct_base = int(idx / total_apps * 90)
            self._emit(BackupProgress(
                phase="messaging",
                current_item=f"{icon} {app_name} — coletando arquivos...",
                items_done=idx,
                items_total=total_apps,
                percent=pct_base,
            ))

            app_folder = folder / "messaging" / app_key
            app_folder.mkdir(parents=True, exist_ok=True)

            # 1. Backup media paths (accessible without root)
            if existing_paths:
                media_files = self.list_remote_files(
                    serial, existing_paths,
                    ignore_cache=True,
                    timeout=300,
                )
                for fpath, fsize in media_files:
                    if self._is_cancelled():
                        break
                    rel = fpath.lstrip("/")
                    local_path = app_folder / "media" / rel
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        self.adb.pull(fpath, str(local_path), serial)
                        total_files += 1
                        total_bytes += fsize
                    except Exception as exc:
                        log.warning("Error pulling %s: %s", fpath, exc)

            # 2. Backup APK if requested
            if include_apk:
                for pkg in packages:
                    try:
                        apk_path = self.adb.get_apk_path(pkg, serial)
                        if apk_path:
                            local_apk = app_folder / "apks" / f"{pkg}.apk"
                            local_apk.parent.mkdir(parents=True, exist_ok=True)
                            self.adb.pull(apk_path, str(local_apk), serial)
                    except Exception as exc:
                        log.warning("Failed to backup APK for %s: %s", pkg, exc)

            # 3. Backup app data via ADB backup (if supported)
            for pkg in packages:
                try:
                    data_file = app_folder / f"{pkg}_data.ab"
                    self.adb.run(
                        ["backup", "-f", str(data_file), "-noapk", pkg],
                        serial=serial,
                        timeout=300,
                    )
                except Exception as exc:
                    log.debug("ADB backup for %s skipped: %s", pkg, exc)

            backed_up_apps.append(app_key)

            # Save per-app metadata
            app_meta = {
                "app_key": app_key,
                "name": app_name,
                "packages": packages,
                "media_paths_backed_up": existing_paths,
                "files_count": total_files,
            }
            (app_folder / "app_info.json").write_text(
                json.dumps(app_meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        duration = time.time() - self._start_time
        actual_size = self.get_backup_size(backup_id)

        manifest = BackupManifest(
            backup_id=backup_id,
            device_serial=serial,
            device_model=device.model,
            device_manufacturer=device.manufacturer,
            android_version=device.android_version,
            backup_type="messaging",
            categories=["messaging"],
            messaging_apps=backed_up_apps,
            timestamp=datetime.now().isoformat(),
            size_bytes=actual_size,
            file_count=total_files,
            duration_seconds=duration,
        )
        manifest.save(folder / "manifest.json")

        self._emit(BackupProgress(phase="complete", percent=100))
        log.info(
            "Messaging backup complete: %s (%d apps, %d files, %s)",
            backup_id, len(backed_up_apps), total_files, actual_size,
        )
        return manifest

    # ------------------------------------------------------------------
    # Unsynced Apps Backup (apps with local-only data)
    # ------------------------------------------------------------------
    def backup_unsynced_apps(
        self,
        serial: str,
        packages: List[str],
        include_apk: bool = True,
    ) -> Optional[BackupManifest]:
        """Backup apps that may have local-only data (authenticators, games, etc.).

        For each package:
          1. Backup APK
          2. Backup accessible data in /sdcard/Android/data/<pkg>
          3. Backup accessible data in /sdcard/Android/media/<pkg>
          4. Attempt ADB backup (app data) if device allows
        """
        if not packages:
            return None

        self._begin_operation()
        device = self.adb.get_device_details(serial)
        folder, backup_id = self._create_backup_folder(device, "unsynced_apps")

        total_pkgs = len(packages)
        backed_up: List[str] = []
        total_files = 0
        total_bytes = 0

        self._emit(BackupProgress(
            phase="unsynced_apps",
            current_item=f"Preparando backup de {total_pkgs} app(s)...",
            items_total=total_pkgs,
        ))

        for idx, pkg in enumerate(packages):
            if self._cancel_flag.is_set():
                break

            pct_base = int(idx / total_pkgs * 90)
            self._emit(BackupProgress(
                phase="unsynced_apps",
                current_item=f"📦 {pkg}",
                items_done=idx,
                items_total=total_pkgs,
                percent=pct_base,
            ))

            pkg_folder = folder / "unsynced" / pkg
            pkg_folder.mkdir(parents=True, exist_ok=True)
            pkg_files = 0

            # 1. Backup APK
            if include_apk:
                try:
                    apk_path = self.adb.get_apk_path(pkg, serial)
                    if apk_path:
                        local_apk = pkg_folder / "apk" / f"{pkg}.apk"
                        local_apk.parent.mkdir(parents=True, exist_ok=True)
                        self.adb.pull(apk_path.strip(), str(local_apk), serial)
                        pkg_files += 1
                except Exception as exc:
                    log.debug("APK backup failed for %s: %s", pkg, exc)

            # 2. Backup accessible data directories
            data_dirs: List[str] = []
            for base_dir in ("/sdcard/Android/data", "/sdcard/Android/media"):
                data_path = f"{base_dir}/{pkg}"
                try:
                    check = self.adb.run_shell(
                        f'test -d "{data_path}" && echo yes',
                        serial, timeout=5,
                    )
                    if "yes" in check:
                        data_dirs.append(data_path)
                except Exception:
                    pass

            if data_dirs:
                data_files = self.list_remote_files(
                    serial, data_dirs, ignore_cache=True,
                )
                for fpath, fsize in data_files:
                    if self._is_cancelled():
                        break
                    rel = fpath.lstrip("/")
                    local_path = pkg_folder / "data" / rel
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        self.adb.pull(fpath, str(local_path), serial)
                        pkg_files += 1
                        total_bytes += fsize
                    except Exception:
                        pass

            # 3. ADB backup (app internal data — may require confirmation)
            try:
                data_file = pkg_folder / f"{pkg}_data.ab"
                self.adb.run(
                    ["backup", "-f", str(data_file), "-noapk", pkg],
                    serial=serial,
                    timeout=60,
                )
            except Exception as exc:
                log.debug("ADB backup for %s skipped: %s", pkg, exc)

            total_files += pkg_files
            backed_up.append(pkg)

            # Save per-package metadata
            meta = {
                "package": pkg,
                "files_backed_up": pkg_files,
            }
            (pkg_folder / "pkg_info.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        duration = time.time() - self._start_time
        actual_size = self.get_backup_size(backup_id)

        manifest = BackupManifest(
            backup_id=backup_id,
            device_serial=serial,
            device_model=device.model,
            device_manufacturer=device.manufacturer,
            android_version=device.android_version,
            backup_type="unsynced_apps",
            categories=["unsynced_apps"],
            unsynced_packages=backed_up,
            timestamp=datetime.now().isoformat(),
            size_bytes=actual_size,
            file_count=total_files,
            duration_seconds=duration,
        )
        manifest.save(folder / "manifest.json")

        self._emit(BackupProgress(phase="complete", percent=100))
        log.info(
            "Unsynced apps backup complete: %s (%d packages, %d files, %s)",
            backup_id, len(backed_up), total_files, actual_size,
        )
        return manifest

    # ------------------------------------------------------------------
    # Custom Paths Backup (from tree browser)
    # ------------------------------------------------------------------
    def backup_custom_paths(
        self,
        serial: str,
        remote_paths: List[str],
        ignore_cache: bool = False,
        ignore_thumbnails: bool = False,
    ) -> Optional[BackupManifest]:
        """Backup specific paths selected by the user in the file tree browser."""
        self._begin_operation()
        device = self.adb.get_device_details(serial)
        folder, backup_id = self._create_backup_folder(device, "custom")

        self._emit(BackupProgress(phase="custom", sub_phase="custom",
                                  current_item="Escaneando caminhos selecionados..."))

        # Separate files vs directories for scanning
        dir_paths: List[str] = []
        single_files: List[Tuple[str, int]] = []

        for rpath in remote_paths:
            if self._is_cancelled():
                break
            try:
                check = self.adb.run_shell(
                    f'test -d "{rpath}" && echo DIR || echo FILE', serial, timeout=5,
                )
                if "DIR" in check:
                    dir_paths.append(rpath)
                else:
                    out = self.adb.run_shell(
                        f'stat -c "%s" "{rpath}" 2>/dev/null', serial, timeout=5,
                    )
                    try:
                        fsize = int(out.strip())
                    except ValueError:
                        fsize = 0
                    single_files.append((rpath, fsize))
            except Exception as exc:
                log.warning("Error scanning path %s: %s", rpath, exc)

        # List directory contents using shared helper (with filters)
        all_files = self.list_remote_files(
            serial, dir_paths,
            ignore_cache=ignore_cache,
            ignore_thumbnails=ignore_thumbnails,
        )
        all_files.extend(single_files)

        total_files = len(all_files)
        total_bytes = sum(s for _, s in all_files)
        log.info("Custom backup: %d files (%d bytes) from %d selected paths",
                 total_files, total_bytes, len(remote_paths))

        # Pull using shared helper
        file_count, bytes_done = self.pull_with_progress(
            serial, all_files, folder / "files",
            phase="custom", sub_phase="custom",
        )

        duration = time.time() - self._start_time
        actual_size = self.get_backup_size(backup_id)

        manifest = BackupManifest(
            backup_id=backup_id,
            device_serial=serial,
            device_model=device.model,
            device_manufacturer=device.manufacturer,
            android_version=device.android_version,
            backup_type="custom",
            categories=["custom"],
            custom_paths=remote_paths,
            timestamp=datetime.now().isoformat(),
            size_bytes=actual_size,
            file_count=file_count,
            duration_seconds=duration,
        )
        manifest.save(folder / "manifest.json")

        self._emit(BackupProgress(phase="complete", percent=100))
        log.info("Custom backup complete: %s (%d files)", backup_id, file_count)
        return manifest

    # ------------------------------------------------------------------
    # Comprehensive backup
    # ------------------------------------------------------------------
    def backup_comprehensive(
        self,
        serial: str,
        categories: Optional[List[str]] = None,
        messaging_app_keys: Optional[List[str]] = None,
        custom_paths: Optional[List[str]] = None,
    ) -> List[BackupManifest]:
        """Run multiple backup types in sequence."""
        results = []
        categories = categories or [
            "apps", "photos", "videos", "music", "documents", "contacts", "sms"
        ]

        file_cats = [c for c in categories if c in MEDIA_PATHS]
        special_cats = [c for c in categories if c not in MEDIA_PATHS]

        # File-based backups
        if file_cats:
            m = self.backup_files(serial, file_cats)
            if m:
                results.append(m)

        # App backup
        if "apps" in special_cats:
            m = self.backup_apps(serial)
            if m:
                results.append(m)

        # Contacts
        if "contacts" in special_cats:
            m = self.backup_contacts(serial)
            if m:
                results.append(m)

        # SMS
        if "sms" in special_cats:
            m = self.backup_sms(serial)
            if m:
                results.append(m)

        # Messaging apps
        if "messaging" in special_cats or messaging_app_keys:
            m = self.backup_messaging_apps(serial, app_keys=messaging_app_keys)
            if m:
                results.append(m)

        # Custom tree-selected paths
        if custom_paths:
            m = self.backup_custom_paths(serial, custom_paths)
            if m:
                results.append(m)

        return results
