"""
restore_manager.py - Restore backups to Android devices via ADB.

Supports:
  - Full ADB restore (.ab files)
  - Selective file restore (push files back to device)
  - APK reinstallation
  - Contacts / SMS restore
  - Restore from different device (cross-device transfer)
"""

import json
import logging
import os
import shutil
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .adb_core import ADBCore, DeviceInfo
from .adb_base import ADBManagerBase, OperationProgress, safe_percent
from .backup_manager import BackupManifest, BackupProgress, MEDIA_PATHS

log = logging.getLogger("adb_toolkit.restore")


# ---------------------------------------------------------------------------
# Restore Manager
# ---------------------------------------------------------------------------
class RestoreManager(ADBManagerBase):
    """Restores backups to Android devices."""

    def __init__(self, adb: ADBCore, backup_dir: Optional[Path] = None):
        super().__init__(adb)
        self.backup_dir = backup_dir or (adb.base_dir / "backups")

    # ------------------------------------------------------------------
    # Load backup info
    # ------------------------------------------------------------------
    def get_backup_manifest(self, backup_id: str) -> Optional[BackupManifest]:
        """Load manifest for a specific backup."""
        manifest_path = self.backup_dir / backup_id / "manifest.json"
        if manifest_path.exists():
            return BackupManifest.load(manifest_path)
        return None

    # ------------------------------------------------------------------
    # Full ADB Restore
    # ------------------------------------------------------------------
    def restore_full(self, serial: str, backup_id: str) -> bool:
        """Restore a full ADB backup (.ab file)."""
        self._begin_operation()
        manifest = self.get_backup_manifest(backup_id)
        if not manifest:
            log.error("Backup %s not found", backup_id)
            return False

        folder = self.backup_dir / backup_id
        backup_file = folder / "backup.ab"

        if not backup_file.exists():
            log.error("Backup file not found: %s", backup_file)
            return False

        self._emit(BackupProgress(
            phase="full_restore",
            current_item="Restaurando backup completo (confirme no dispositivo)...",
            percent=10,
        ))

        log.info("Restoring full backup %s to %s", backup_id, serial)
        result = self._run_with_confirmation(
            ["restore", str(backup_file)],
            serial,
            title="Restauração Completa",
            message=(
                "O dispositivo está exibindo uma tela de confirmação.\n\n"
                "📱 Toque em 'RESTAURAR MEUS DADOS' no seu dispositivo "
                "para continuar.\n\n"
                "A operação aguardará até você confirmar."
            ),
            timeout=7200,
        )

        success = result.returncode == 0
        self._emit(BackupProgress(
            phase="complete" if success else "error",
            percent=100,
        ))
        log.info("Full restore %s", "completed" if success else "failed")
        return success

    # ------------------------------------------------------------------
    # File Restore
    # ------------------------------------------------------------------
    def restore_files(
        self,
        serial: str,
        backup_id: str,
        categories: Optional[List[str]] = None,
        specific_files: Optional[List[str]] = None,
    ) -> bool:
        """Restore files from a file backup."""
        self._begin_operation()
        folder = self.backup_dir / backup_id / "files"

        if not folder.exists():
            log.error("File backup not found: %s", folder)
            return False

        # Collect files to restore
        files_to_restore: List[Tuple[Path, str]] = []

        if specific_files:
            for sf in specific_files:
                local = folder / sf.lstrip("/")
                if local.exists():
                    remote = "/" + sf.lstrip("/")
                    files_to_restore.append((local, remote))
        else:
            # Restore all files or by category
            target_prefixes = []
            if categories:
                for cat in categories:
                    if cat in MEDIA_PATHS:
                        for p in MEDIA_PATHS[cat]:
                            target_prefixes.append(p.lstrip("/"))

            for local_file in folder.rglob("*"):
                if local_file.is_file():
                    rel = local_file.relative_to(folder)
                    remote = "/" + str(rel).replace("\\", "/")

                    if target_prefixes:
                        if any(str(rel).replace("\\", "/").startswith(p) for p in target_prefixes):
                            files_to_restore.append((local_file, remote))
                    else:
                        files_to_restore.append((local_file, remote))

        total = len(files_to_restore)
        log.info("Restoring %d files to device %s", total, serial)

        # Push all files using shared helper (with elapsed/ETA auto-tracking)
        success_count, bytes_done = self.push_with_progress(
            serial, files_to_restore,
            phase="restoring_files", sub_phase="files",
        )

        self._emit(BackupProgress(phase="complete", percent=100))
        log.info("Restored %d/%d files", success_count, total)
        return success_count == total

    # ------------------------------------------------------------------
    # App Restore
    # ------------------------------------------------------------------
    def restore_apps(
        self,
        serial: str,
        backup_id: str,
        selected_packages: Optional[List[str]] = None,
        restore_data: bool = False,
    ) -> Tuple[int, int]:
        """Restore APKs to device. Handles both single and split APKs.

        Returns (success_count, total_count).
        """
        self._begin_operation()
        folder = self.backup_dir / backup_id / "apks"

        if not folder.exists():
            log.error("APK backup not found: %s", folder)
            return (0, 0)

        # Collect installable items: single APKs + split APK directories
        install_items: List[Tuple[str, Path]] = []  # (pkg_name, path)

        # Single APKs (flat files)
        for apk in folder.glob("*.apk"):
            pkg_name = apk.stem
            if selected_packages and pkg_name not in selected_packages:
                continue
            install_items.append((pkg_name, apk))

        # Split APK directories (pkg_name/ folder containing multiple .apk files)
        for sub_dir in folder.iterdir():
            if sub_dir.is_dir():
                pkg_name = sub_dir.name
                if selected_packages and pkg_name not in selected_packages:
                    continue
                apks_in_dir = list(sub_dir.glob("*.apk"))
                if apks_in_dir:
                    install_items.append((pkg_name, sub_dir))

        total = len(install_items)
        log.info("Restoring %d apps to %s", total, serial)
        self._emit(BackupProgress(phase="restoring_apps", sub_phase="apps",
                                  items_total=total))

        success = 0
        for i, (pkg_name, path) in enumerate(install_items):
            if self._cancel_flag.is_set():
                break

            self._emit(BackupProgress(
                phase="restoring_apps",
                sub_phase="apps",
                current_item=pkg_name,
                items_done=i,
                items_total=total,
                percent=safe_percent(i, total),
            ))

            try:
                if path.is_dir():
                    # Split APK — use install-multiple
                    apk_files = [str(a) for a in path.glob("*.apk")]
                    if self.adb.install_split_apks(apk_files, serial):
                        success += 1
                        log.info("Installed split APK %s (%d parts)", pkg_name, len(apk_files))
                    else:
                        log.warning("Failed to install split APK %s", pkg_name)
                else:
                    # Single APK
                    if self.adb.install_apk(str(path), serial):
                        success += 1
                        log.info("Installed %s", pkg_name)
                    else:
                        log.warning("Failed to install %s", pkg_name)
            except Exception as exc:
                log.warning("Install error for %s: %s", pkg_name, exc)

        # Restore app data if requested
        if restore_data:
            data_file = self.backup_dir / backup_id / "app_data.ab"
            if data_file.exists() and data_file.stat().st_size > 24:
                self._emit(BackupProgress(
                    phase="restoring_app_data",
                    sub_phase="app_data",
                    current_item="Restaurando dados dos apps...",
                    percent=90,
                ))
                self._run_with_confirmation(
                    ["restore", str(data_file)],
                    serial,
                    title="Restauração de Dados",
                    message=(
                        "📱 Confirme a restauração de dados no dispositivo.\n\n"
                        "Toque em 'RESTAURAR MEUS DADOS' na tela do aparelho."
                    ),
                    timeout=3600,
                )

        self._emit(BackupProgress(phase="complete", percent=100))
        log.info("Restored %d/%d apps", success, total)
        return (success, total)

    # ------------------------------------------------------------------
    # Contacts / SMS Restore
    # ------------------------------------------------------------------
    def restore_contacts(self, serial: str, backup_id: str) -> bool:
        """Restore contacts backup (supports VCF and .ab formats)."""
        self._begin_operation()
        folder = self.backup_dir / backup_id
        success = False

        self._emit(BackupProgress(phase="restoring_contacts", sub_phase="contacts",
                                  current_item="Restoring contacts..."))

        # --- Strategy 1: VCF file → push to device and import ---
        contacts_vcf = folder / "contacts.vcf"
        if contacts_vcf.exists() and contacts_vcf.stat().st_size > 10:
            try:
                self._emit(BackupProgress(
                    phase="restoring_contacts",
                    current_item="Importando contatos via VCF...",
                    percent=20,
                ))
                remote_vcf = "/sdcard/contacts_restore.vcf"
                if self.adb.push(str(contacts_vcf), remote_vcf, serial):
                    # Trigger VCF import via intent
                    self.adb.run_shell(
                        'am start -a android.intent.action.VIEW '
                        '-d "file:///sdcard/contacts_restore.vcf" '
                        '-t "text/x-vcard"',
                        serial, timeout=30,
                    )
                    log.info("Pushed contacts VCF to device — import intent sent")
                    success = True
            except Exception as exc:
                log.warning("VCF import failed: %s", exc)

        # --- Strategy 2: .ab file → ADB restore (only if non-empty) ---
        contacts_ab = folder / "contacts.ab"
        if contacts_ab.exists() and contacts_ab.stat().st_size > 24:
            try:
                self._emit(BackupProgress(
                    phase="restoring_contacts",
                    current_item="ADB restore de contatos...",
                    percent=50,
                ))
                result = self._run_with_confirmation(
                    ["restore", str(contacts_ab)],
                    serial,
                    title="Restauração de Contatos",
                    message=(
                        "📱 Confirme a restauração no dispositivo para recuperar os contatos.\n\n"
                        "Toque em 'RESTAURAR MEUS DADOS' na tela do aparelho."
                    ),
                    timeout=120,
                )
                if result.returncode == 0:
                    success = True
            except Exception as exc:
                log.warning("ADB contacts restore failed: %s", exc)
        elif contacts_ab.exists():
            log.info("Skipping contacts.ab — file is empty (Android 12+ restriction)")

        # --- Strategy 3: contacts2.db → push and restore (needs root) ---
        contacts_db = folder / "contacts2.db"
        if contacts_db.exists() and contacts_db.stat().st_size > 0 and not success:
            try:
                self._emit(BackupProgress(
                    phase="restoring_contacts",
                    current_item="Restaurando DB de contatos (root)...",
                    percent=75,
                ))
                db_remote = "/data/data/com.android.providers.contacts/databases/contacts2.db"
                if self.adb.push(str(contacts_db), db_remote, serial):
                    success = True
            except Exception as exc:
                log.debug("DB push failed (expected without root): %s", exc)

        if not success:
            log.warning(
                "No viable contacts backup found in %s. "
                "On Android 12+ without root, restore contacts via Google Sync "
                "or import the contacts.vcf manually in the Contacts app.",
                backup_id,
            )

        self._emit(BackupProgress(phase="complete", percent=100))
        return success

    def restore_sms(self, serial: str, backup_id: str) -> bool:
        """Restore SMS backup (supports JSON and .ab formats)."""
        self._begin_operation()
        folder = self.backup_dir / backup_id
        success = False
        restored_count = 0

        self._emit(BackupProgress(phase="restoring_sms", sub_phase="sms",
                                  current_item="Restoring messages..."))

        # --- Strategy 1: JSON file → content insert via content provider ---
        sms_json = folder / "sms_backup.json"
        if sms_json.exists() and sms_json.stat().st_size > 10:
            try:
                self._emit(BackupProgress(
                    phase="restoring_sms",
                    current_item="Restaurando SMS via content provider...",
                    percent=10,
                ))

                sms_data = json.loads(sms_json.read_text(encoding="utf-8"))
                total_sms = len(sms_data)
                log.info("Restoring %d SMS messages from JSON backup", total_sms)

                for i, sms in enumerate(sms_data):
                    if self._cancel_flag.is_set():
                        break

                    address = sms.get("address", "")
                    body = sms.get("body", "")
                    date = sms.get("date", "")
                    sms_type = sms.get("type", "1")  # 1=inbox, 2=sent
                    read_status = sms.get("read", "1")

                    if not address or not body:
                        continue

                    # Escape single quotes in body for shell
                    body_escaped = body.replace("'", "'\\''")
                    address_escaped = address.replace("'", "'\\''")

                    cmd = (
                        f"content insert --uri content://sms "
                        f"--bind address:s:'{address_escaped}' "
                        f"--bind body:s:'{body_escaped}' "
                        f"--bind type:i:{sms_type} "
                        f"--bind read:i:{read_status}"
                    )
                    if date:
                        cmd += f" --bind date:l:{date}"

                    result = self.adb.run_shell(cmd, serial, timeout=10)
                    if "Exception" not in result:
                        restored_count += 1

                    if i % 50 == 0:
                        pct = int(10 + (i / total_sms * 70)) if total_sms > 0 else 50
                        self._emit(BackupProgress(
                            phase="restoring_sms",
                            current_item=f"SMS {i+1}/{total_sms}",
                            items_done=i + 1,
                            items_total=total_sms,
                            percent=pct,
                        ))

                if restored_count > 0:
                    success = True
                    log.info("Restored %d/%d SMS messages via content provider",
                             restored_count, total_sms)

            except Exception as exc:
                log.warning("SMS JSON restore failed: %s", exc)

        # --- Strategy 2: .ab file → ADB restore (only if non-empty) ---
        sms_ab = folder / "sms.ab"
        if sms_ab.exists() and sms_ab.stat().st_size > 24:
            try:
                self._emit(BackupProgress(
                    phase="restoring_sms",
                    current_item="ADB restore de SMS...",
                    percent=85,
                ))
                result = self._run_with_confirmation(
                    ["restore", str(sms_ab)],
                    serial,
                    title="Restauração de SMS",
                    message=(
                        "📱 Confirme a restauração no dispositivo para recuperar as mensagens.\n\n"
                        "Toque em 'RESTAURAR MEUS DADOS' na tela do aparelho."
                    ),
                    timeout=120,
                )
                if result.returncode == 0:
                    success = True
            except Exception as exc:
                log.warning("ADB SMS restore failed: %s", exc)
        elif sms_ab.exists():
            log.info("Skipping sms.ab — file is empty (Android 12+ restriction)")

        # --- Strategy 3: mmssms.db → push (needs root) ---
        sms_db = folder / "mmssms.db"
        if sms_db.exists() and sms_db.stat().st_size > 0 and not success:
            try:
                db_remote = "/data/data/com.android.providers.telephony/databases/mmssms.db"
                if self.adb.push(str(sms_db), db_remote, serial):
                    success = True
            except Exception:
                log.debug("Direct SMS DB push failed (expected without root)")

        if not success:
            log.warning(
                "No viable SMS backup found in %s. "
                "On Android 12+ without root, SMS restore may require "
                "a dedicated SMS backup app or Google Backup.",
                backup_id,
            )
        else:
            log.info("SMS restore complete: %d messages restored", restored_count)

        self._emit(BackupProgress(phase="complete", percent=100))
        return success

    # ------------------------------------------------------------------
    # Messaging App Restore
    # ------------------------------------------------------------------
    def restore_messaging_apps(
        self,
        serial: str,
        backup_id: str,
        app_keys: Optional[List[str]] = None,
        restore_apk: bool = True,
    ) -> bool:
        """Restore messaging app data from a messaging backup."""
        self._begin_operation()
        messaging_dir = self.backup_dir / backup_id / "messaging"

        if not messaging_dir.exists():
            log.error("Messaging backup dir not found: %s", messaging_dir)
            return False

        app_dirs = [d for d in messaging_dir.iterdir() if d.is_dir()]
        if app_keys:
            app_dirs = [d for d in app_dirs if d.name in app_keys]

        total = len(app_dirs)
        if total == 0:
            log.warning("No messaging app data to restore in %s", backup_id)
            return False

        self._emit(BackupProgress(
            phase="restoring_messaging",
            sub_phase="messaging",
            current_item="Restaurando apps de mensagem...",
            items_total=total,
        ))

        success_count = 0
        for i, app_dir in enumerate(app_dirs):
            if self._cancel_flag.is_set():
                break

            app_key = app_dir.name
            self._emit(BackupProgress(
                phase="restoring_messaging",
                sub_phase="messaging",
                current_item=f"Restaurando {app_key}...",
                items_done=i,
                items_total=total,
                percent=safe_percent(i, total),
            ))

            try:
                # 1. Restore APKs (handles split APKs)
                if restore_apk:
                    apk_dir = app_dir / "apks"
                    if apk_dir.exists():
                        apk_files = list(apk_dir.glob("*.apk"))
                        if len(apk_files) > 1:
                            # Multiple APKs — likely split APKs, use install-multiple
                            self.adb.install_split_apks(
                                [str(a) for a in apk_files], serial,
                            )
                        elif apk_files:
                            self.adb.install_apk(str(apk_files[0]), serial)

                # 2. Restore media files — parallel via push_with_progress
                media_dir = app_dir / "media"
                if media_dir.exists():
                    files_to_push = []
                    for local_file in media_dir.rglob("*"):
                        if local_file.is_file():
                            rel = local_file.relative_to(media_dir)
                            remote = "/" + str(rel).replace("\\", "/")
                            files_to_push.append((local_file, remote))
                    if files_to_push:
                        self.push_with_progress(
                            serial, files_to_push,
                            phase="restoring_messaging",
                            sub_phase=app_key,
                            pct_range=(
                                safe_percent(i, total),
                                safe_percent(i + 0.7, total),
                            ),
                        )

                # 3. Restore app data (.ab files — skip empty ones)
                for ab_file in app_dir.glob("*_data.ab"):
                    if ab_file.stat().st_size > 24:
                        self._run_with_confirmation(
                            ["restore", str(ab_file)],
                            serial,
                            title="Restauração de Dados do App",
                            message=(
                                f"📱 Confirme a restauração de dados do app no dispositivo.\n\n"
                                f"App: {app_key}\n"
                                f"Toque em 'RESTAURAR MEUS DADOS' na tela do aparelho."
                            ),
                            timeout=300,
                        )

                success_count += 1

            except Exception as exc:
                log.warning("Failed to restore %s: %s", app_key, exc)

        self._emit(BackupProgress(phase="complete", percent=100))
        log.info("Messaging restore: %d/%d apps", success_count, total)
        return success_count == total

    # ------------------------------------------------------------------
    # Custom Paths Restore
    # ------------------------------------------------------------------
    def restore_custom_paths(
        self,
        serial: str,
        backup_id: str,
        target_paths: Optional[List[str]] = None,
    ) -> bool:
        """Restore files from a custom-path backup."""
        self._begin_operation()
        files_dir = self.backup_dir / backup_id / "files"

        if not files_dir.exists():
            log.error("Custom backup files not found: %s", files_dir)
            return False

        files_to_restore: List[Tuple[Path, str]] = []
        for local_file in files_dir.rglob("*"):
            if local_file.is_file():
                rel = local_file.relative_to(files_dir)
                remote = "/" + str(rel).replace("\\", "/")
                if target_paths:
                    if any(remote.startswith(t) for t in target_paths):
                        files_to_restore.append((local_file, remote))
                else:
                    files_to_restore.append((local_file, remote))

        if not files_to_restore:
            self._emit(BackupProgress(phase="complete", percent=100))
            return True

        success_count, _ = self.push_with_progress(
            serial, files_to_restore,
            phase="restoring_custom",
            sub_phase="custom_files",
        )
        total = len(files_to_restore)

        self._emit(BackupProgress(phase="complete", percent=100))
        log.info("Custom restore: %d/%d files", success_count, total)
        return success_count == total

    # ------------------------------------------------------------------
    # Unsynced Apps Restore
    # ------------------------------------------------------------------
    def restore_unsynced_apps(
        self,
        serial: str,
        backup_id: str,
        packages: Optional[List[str]] = None,
    ) -> bool:
        """Restore apps backed up by backup_unsynced_apps.

        For each package:
          1. Install APK (if present)
          2. Push data back to /sdcard/Android/data|media/<pkg>
          3. Attempt ADB restore from .ab file (if present)
        """
        self._begin_operation()
        unsynced_dir = self.backup_dir / backup_id / "unsynced"

        if not unsynced_dir.exists():
            log.error("Unsynced backup dir not found: %s", unsynced_dir)
            return False

        # Determine which packages to restore
        pkg_dirs = [d for d in unsynced_dir.iterdir() if d.is_dir()]
        if packages:
            pkg_dirs = [d for d in pkg_dirs if d.name in packages]

        total = len(pkg_dirs)
        success_count = 0

        self._emit(BackupProgress(
            phase="restoring_unsynced",
            sub_phase="unsynced",
            items_total=total,
            current_item=f"Restaurando {total} app(s)...",
        ))

        for idx, pkg_dir in enumerate(pkg_dirs):
            if self._cancel_flag.is_set():
                break

            pkg = pkg_dir.name
            self._emit(BackupProgress(
                phase="restoring_unsynced",
                sub_phase="unsynced",
                current_item=f"📦 {pkg}",
                items_done=idx,
                items_total=total,
                percent=safe_percent(idx, total),
            ))

            ok = True

            # 1. Install APK
            apk_dir = pkg_dir / "apk"
            if apk_dir.exists():
                for apk_file in apk_dir.glob("*.apk"):
                    try:
                        self.adb.install_apk(str(apk_file), serial)
                    except Exception as exc:
                        log.warning("APK install failed for %s: %s", pkg, exc)
                        ok = False

            # 2. Push data files back — parallel via push_with_progress
            data_dir = pkg_dir / "data"
            if data_dir.exists():
                files_to_push = []
                for local_file in data_dir.rglob("*"):
                    if local_file.is_file():
                        rel = local_file.relative_to(data_dir)
                        remote = "/" + str(rel).replace("\\", "/")
                        files_to_push.append((local_file, remote))
                if files_to_push:
                    self.push_with_progress(
                        serial, files_to_push,
                        phase="restoring_unsynced",
                        sub_phase=pkg,
                        pct_range=(
                            safe_percent(idx, total),
                            safe_percent(idx + 0.7, total),
                        ),
                    )

            # 3. ADB restore from .ab file
            ab_file = pkg_dir / f"{pkg}_data.ab"
            if ab_file.exists() and ab_file.stat().st_size > 24:
                try:
                    self._run_with_confirmation(
                        ["restore", str(ab_file)],
                        serial,
                        title="Restauração de Dados do App",
                        message=(
                            f"📱 Confirme a restauração de dados no dispositivo.\n\n"
                            f"App: {pkg}\n"
                            f"Toque em 'RESTAURAR MEUS DADOS' na tela do aparelho."
                        ),
                        timeout=120,
                    )
                except Exception as exc:
                    log.debug("ADB restore for %s skipped: %s", pkg, exc)

            if ok:
                success_count += 1

        self._emit(BackupProgress(phase="complete", percent=100))
        log.info("Unsynced apps restore: %d/%d packages", success_count, total)
        return success_count == total

    # ------------------------------------------------------------------
    # Smart Restore (auto-detect backup type)
    # ------------------------------------------------------------------
    def restore_smart(self, serial: str, backup_id: str) -> bool:
        """Automatically restore based on backup type."""
        manifest = self.get_backup_manifest(backup_id)
        if not manifest:
            log.error("Backup %s not found", backup_id)
            return False

        log.info("Smart restore: type=%s, categories=%s", manifest.backup_type, manifest.categories)

        if manifest.backup_type == "full":
            return self.restore_full(serial, backup_id)

        success = True

        if manifest.backup_type == "files":
            success = self.restore_files(serial, backup_id) and success

        if manifest.backup_type == "apps":
            s, t = self.restore_apps(serial, backup_id)
            success = (s == t) and success

        if manifest.backup_type == "contacts":
            success = self.restore_contacts(serial, backup_id) and success

        if manifest.backup_type == "sms":
            success = self.restore_sms(serial, backup_id) and success

        if manifest.backup_type == "messaging":
            success = self.restore_messaging_apps(serial, backup_id) and success

        if manifest.backup_type == "unsynced_apps":
            success = self.restore_unsynced_apps(serial, backup_id) and success

        if manifest.backup_type == "custom":
            success = self.restore_custom_paths(serial, backup_id) and success

        return success
