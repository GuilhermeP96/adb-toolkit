"""
transfer_manager.py - Device-to-device transfer via ADB.

Orchestrates backup from source device and restore to target device:
  - Apps (APKs)
  - Files (photos, videos, music, documents)
  - Contacts
  - SMS
  - Wi-Fi credentials (root)
  - Accounts list
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .adb_base import (
    ADBManagerBase,
    CACHE_PATTERNS,
    OperationProgress,
    THUMBNAIL_DUMP_PATTERNS,
    safe_percent,
)
from .adb_core import ADBCore, DeviceInfo
from .backup_manager import BackupManager, BackupManifest, BackupProgress
from .restore_manager import RestoreManager
from .accelerator import TransferAccelerator, verify_transfer, gpu_available, parallel_checksum
from .utils import format_bytes

log = logging.getLogger("adb_toolkit.transfer")


# ---------------------------------------------------------------------------
# Transfer config
# ---------------------------------------------------------------------------
@dataclass
class TransferConfig:
    """What to transfer between devices."""
    apps: bool = True
    app_data: bool = False
    photos: bool = True
    videos: bool = True
    music: bool = True
    documents: bool = True
    contacts: bool = True
    sms: bool = True
    messaging_apps: bool = False
    messaging_app_keys: List[str] = field(default_factory=list)
    unsynced_packages: List[str] = field(default_factory=list)
    wifi: bool = False           # Needs root
    custom_paths: List[str] = field(default_factory=list)
    ignore_cache: bool = True
    ignore_thumbnails: bool = True


# Backward-compatible alias — callers importing TransferProgress keep working.
TransferProgress = OperationProgress


# ---------------------------------------------------------------------------
# Transfer Manager
# ---------------------------------------------------------------------------
class TransferManager(ADBManagerBase):
    """Manages device-to-device transfer."""

    def __init__(self, adb: ADBCore, work_dir: Optional[Path] = None):
        super().__init__(adb)
        self.work_dir = work_dir or (adb.base_dir / "transfers")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._transfer_progress = TransferProgress()
        self.accelerator = TransferAccelerator()
        # Child managers created during operations; tracked for cancel cascade
        self._child_backup_mgr: Optional[BackupManager] = None
        self._child_restore_mgr: Optional[RestoreManager] = None

    def cancel(self):
        """Cancel current operation and cascade to child managers."""
        self._cancel_flag.set()
        if self._child_backup_mgr is not None:
            self._child_backup_mgr.cancel()
        if self._child_restore_mgr is not None:
            self._child_restore_mgr.cancel()

    def _emit(self, progress: Optional[OperationProgress] = None):
        """Emit progress — uses mutable _transfer_progress when no arg given."""
        p = progress if progress is not None else self._transfer_progress
        if self._start_time is not None:
            p.elapsed_seconds = time.time() - self._start_time
        if self._progress_cb:
            try:
                self._progress_cb(p)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------
    def validate_devices(
        self, source_serial: str, target_serial: str
    ) -> Tuple[bool, str]:
        """Verify both devices are connected and ready."""
        devices = self.adb.list_devices()
        serials = {d.serial: d for d in devices}

        if source_serial not in serials:
            return False, f"Source device {source_serial} not connected"
        if target_serial not in serials:
            return False, f"Target device {target_serial} not connected"
        if source_serial == target_serial:
            return False, "Source and target cannot be the same device"

        src = serials[source_serial]
        tgt = serials[target_serial]

        if src.state != "device":
            return False, f"Source device state: {src.state} (expected: device)"
        if tgt.state != "device":
            return False, f"Target device state: {tgt.state} (expected: device)"

        return True, "Both devices ready"

    def _get_free_bytes(self, serial: str) -> int:
        """Query free storage bytes on a device via ``df``."""
        try:
            df_out = self.adb.run_shell("df /data", serial)
            lines = df_out.splitlines()
            if len(lines) >= 2:
                cols = lines[1].split()
                if len(cols) >= 4:
                    return int(cols[3]) * 1024  # KB → bytes
        except Exception as exc:
            log.warning("Failed to query free space on %s: %s", serial, exc)
        return -1  # unknown

    def get_transfer_estimate(
        self, source_serial: str, config: TransferConfig
    ) -> Dict[str, int]:
        """Estimate transfer size by category."""
        estimates: Dict[str, int] = {}

        if config.apps:
            packages = self.adb.list_packages(source_serial, third_party=True)
            estimates["apps"] = len(packages)

        if config.photos:
            estimates["photos"] = self._count_remote_files(
                source_serial, ["/sdcard/DCIM", "/sdcard/Pictures"]
            )
        if config.videos:
            estimates["videos"] = self._count_remote_files(
                source_serial, ["/sdcard/Movies"]
            )
        if config.music:
            estimates["music"] = self._count_remote_files(
                source_serial, ["/sdcard/Music"]
            )
        if config.documents:
            estimates["documents"] = self._count_remote_files(
                source_serial, ["/sdcard/Documents", "/sdcard/Download"]
            )

        return estimates

    def _count_remote_files(self, serial: str, paths: List[str]) -> int:
        count = 0
        for p in paths:
            try:
                out = self.adb.run_shell(f"find {p} -type f 2>/dev/null | wc -l", serial)
                count += int(out.strip())
            except Exception:
                pass
        return count

    # ------------------------------------------------------------------
    # Main transfer operation
    # ------------------------------------------------------------------
    def transfer(
        self,
        source_serial: str,
        target_serial: str,
        config: TransferConfig,
    ) -> bool:
        """Transfer data from source device to target device."""
        self._begin_operation()

        # Validate
        valid, msg = self.validate_devices(source_serial, target_serial)
        if not valid:
            log.error("Transfer validation failed: %s", msg)
            self._transfer_progress.phase = "error"
            self._transfer_progress.errors.append(msg)
            self._emit()
            return False

        source_info = self.adb.get_device_details(source_serial)
        target_info = self.adb.get_device_details(target_serial)

        self._transfer_progress = TransferProgress(
            phase="initializing",
            source_device=source_info.friendly_name(),
            target_device=target_info.friendly_name(),
        )
        self._emit()

        log.info(
            "Starting transfer: %s -> %s",
            source_info.friendly_name(),
            target_info.friendly_name(),
        )

        # Create temp backup dir for this transfer
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        transfer_backup_dir = self.work_dir / f"transfer_{ts}"
        transfer_backup_dir.mkdir(parents=True, exist_ok=True)

        backup_mgr = BackupManager(self.adb, transfer_backup_dir)
        restore_mgr = RestoreManager(self.adb, transfer_backup_dir)
        self._child_backup_mgr = backup_mgr
        self._child_restore_mgr = restore_mgr

        # Propagate device-confirmation callbacks to child managers
        if self._confirmation_cb:
            backup_mgr.set_confirmation_callback(
                self._confirmation_cb, self._confirmation_dismiss_cb,
            )
            restore_mgr.set_confirmation_callback(
                self._confirmation_cb, self._confirmation_dismiss_cb,
            )

        overall_success = True
        steps_done = 0
        total_steps = sum([
            config.apps,
            config.photos or config.videos or config.music or config.documents,
            config.contacts,
            config.sms,
            config.messaging_apps,
            bool(config.unsynced_packages),
            bool(config.custom_paths),
        ])

        try:
            # ---- APPS ----
            if config.apps and not self._cancel_flag.is_set():
                self._update_progress("backing_up", "apps", steps_done, total_steps)
                manifest = backup_mgr.backup_apps(
                    source_serial, include_data=config.app_data
                )
                if manifest and not self._cancel_flag.is_set():
                    self._update_progress("restoring", "apps", steps_done, total_steps)
                    s, t = restore_mgr.restore_apps(
                        target_serial, manifest.backup_id,
                        restore_data=config.app_data,
                    )
                    if s < t:
                        self._transfer_progress.errors.append(
                            f"Apps: {s}/{t} installed"
                        )
                        overall_success = False
                steps_done += 1

            # ---- FILES (photos, videos, music, docs) ----
            file_categories = []
            if config.photos:
                file_categories.append("photos")
            if config.videos:
                file_categories.append("videos")
            if config.music:
                file_categories.append("music")
            if config.documents:
                file_categories.append("documents")

            if file_categories and not self._cancel_flag.is_set():
                self._update_progress("backing_up", "files", steps_done, total_steps)
                manifest = backup_mgr.backup_files(
                    source_serial,
                    categories=file_categories,
                    custom_paths=config.custom_paths or None,
                )
                if manifest and not self._cancel_flag.is_set():
                    self._update_progress("restoring", "files", steps_done, total_steps)
                    if not restore_mgr.restore_files(target_serial, manifest.backup_id):
                        self._transfer_progress.errors.append("Some files failed to restore")
                        overall_success = False
                steps_done += 1

            # ---- CONTACTS ----
            if config.contacts and not self._cancel_flag.is_set():
                self._update_progress("backing_up", "contacts", steps_done, total_steps)
                manifest = backup_mgr.backup_contacts(source_serial)
                if manifest and not self._cancel_flag.is_set():
                    self._update_progress("restoring", "contacts", steps_done, total_steps)
                    if not restore_mgr.restore_contacts(target_serial, manifest.backup_id):
                        self._transfer_progress.errors.append("Contacts restore may be incomplete")
                steps_done += 1

            # ---- SMS ----
            if config.sms and not self._cancel_flag.is_set():
                self._update_progress("backing_up", "sms", steps_done, total_steps)
                manifest = backup_mgr.backup_sms(source_serial)
                if manifest and not self._cancel_flag.is_set():
                    self._update_progress("restoring", "sms", steps_done, total_steps)
                    if not restore_mgr.restore_sms(target_serial, manifest.backup_id):
                        self._transfer_progress.errors.append("SMS restore may be incomplete")
                steps_done += 1

            # ---- MESSAGING APPS ----
            if config.messaging_apps and not self._cancel_flag.is_set():
                self._update_progress("backing_up", "messaging", steps_done, total_steps)
                manifest = backup_mgr.backup_messaging_apps(
                    source_serial,
                    app_keys=config.messaging_app_keys or None,
                )
                if manifest and not self._cancel_flag.is_set():
                    self._update_progress("restoring", "messaging", steps_done, total_steps)
                    if not restore_mgr.restore_messaging_apps(
                        target_serial, manifest.backup_id
                    ):
                        self._transfer_progress.errors.append(
                            "Messaging apps restore may be incomplete"
                        )
                steps_done += 1

            # ---- UNSYNCED APPS ----
            if config.unsynced_packages and not self._cancel_flag.is_set():
                self._update_progress("backing_up", "unsynced_apps", steps_done, total_steps)
                manifest = backup_mgr.backup_unsynced_apps(
                    source_serial,
                    packages=config.unsynced_packages,
                )
                if manifest and not self._cancel_flag.is_set():
                    self._update_progress("restoring", "unsynced_apps", steps_done, total_steps)
                    if not restore_mgr.restore_unsynced_apps(
                        target_serial, manifest.backup_id
                    ):
                        self._transfer_progress.errors.append(
                            "Unsynced apps restore may be incomplete"
                        )
                steps_done += 1

            # ---- CUSTOM PATHS ----
            if config.custom_paths and not self._cancel_flag.is_set():
                self._update_progress("backing_up", "custom", steps_done, total_steps)
                manifest = backup_mgr.backup_custom_paths(
                    source_serial, config.custom_paths
                )
                if manifest and not self._cancel_flag.is_set():
                    self._update_progress("restoring", "custom", steps_done, total_steps)
                    if not restore_mgr.restore_custom_paths(
                        target_serial, manifest.backup_id
                    ):
                        self._transfer_progress.errors.append(
                            "Custom paths restore may be incomplete"
                        )
                steps_done += 1

            # ---- WIFI (root) ----
            if config.wifi and not self._cancel_flag.is_set():
                self._transfer_wifi(source_serial, target_serial)

        except Exception as exc:
            log.exception("Transfer error: %s", exc)
            self._transfer_progress.errors.append(str(exc))
            overall_success = False

        elapsed = time.time() - self._start_time
        self._transfer_progress.phase = "complete" if overall_success else "complete_with_errors"
        self._transfer_progress.percent = 100
        self._transfer_progress.elapsed_seconds = elapsed
        self._emit()

        # Cleanup transfer temp files (keep for debugging)
        log.info(
            "Transfer %s in %.1fs. Errors: %s",
            "completed" if overall_success else "completed with errors",
            elapsed,
            self._transfer_progress.errors or "none",
        )

        self._child_backup_mgr = None
        self._child_restore_mgr = None
        return overall_success

    def _update_progress(self, phase: str, sub_phase: str, done: int, total: int):
        self._transfer_progress.phase = phase
        self._transfer_progress.sub_phase = sub_phase
        self._transfer_progress.percent = safe_percent(done, total)
        self._emit()

    def _transfer_wifi(self, source: str, target: str):
        """Transfer Wi-Fi credentials (requires root on both devices)."""
        try:
            # Android < 8: /data/misc/wifi/wpa_supplicant.conf
            # Android >= 8: /data/misc/wifi/WifiConfigStore.xml
            wifi_files = [
                "/data/misc/wifi/wpa_supplicant.conf",
                "/data/misc/wifi/WifiConfigStore.xml",
            ]

            for wf in wifi_files:
                check = self.adb.run_shell(f"su -c 'test -f {wf} && echo yes'", source)
                if "yes" in check:
                    local_tmp = self.work_dir / "wifi_tmp" / Path(wf).name
                    local_tmp.parent.mkdir(parents=True, exist_ok=True)

                    # Pull from source
                    self.adb.run_shell(
                        f"su -c 'cp {wf} /sdcard/wifi_backup_tmp'", source
                    )
                    self.adb.pull("/sdcard/wifi_backup_tmp", str(local_tmp), source)
                    self.adb.run_shell("rm /sdcard/wifi_backup_tmp", source)

                    # Push to target
                    self.adb.push(str(local_tmp), "/sdcard/wifi_backup_tmp", target)
                    self.adb.run_shell(
                        f"su -c 'cp /sdcard/wifi_backup_tmp {wf}'", target
                    )
                    self.adb.run_shell("rm /sdcard/wifi_backup_tmp", target)

                    log.info("Transferred Wi-Fi config: %s", wf)
                    break

        except Exception as exc:
            log.warning("Wi-Fi transfer failed (may need root): %s", exc)
            self._transfer_progress.errors.append(f"Wi-Fi: {exc}")

    # ------------------------------------------------------------------
    # Quick clone (everything)
    # ------------------------------------------------------------------
    def clone_device(self, source_serial: str, target_serial: str) -> bool:
        """Full device clone - transfer everything possible."""
        config = TransferConfig(
            apps=True,
            app_data=True,
            photos=True,
            videos=True,
            music=True,
            documents=True,
            contacts=True,
            sms=True,
            messaging_apps=True,
            wifi=False,  # Skip Wi-Fi by default (needs root)
        )
        return self.transfer(source_serial, target_serial, config)

    # ------------------------------------------------------------------
    # Full storage clone  (/storage/emulated/0 → /storage/emulated/0)
    # ------------------------------------------------------------------
    def clone_full_storage(
        self,
        source_serial: str,
        target_serial: str,
        storage_path: str = "/storage/emulated/0",
        ignore_cache: bool = True,
        ignore_thumbnails: bool = True,
    ) -> bool:
        """Clone entire internal storage + apps + contacts + SMS.

        1. Index all files under *storage_path* on the source device.
        2. Stream files in batches: pull batch → push batch → delete
           local staging to minimise disk usage.
        3. Additionally transfer apps, contacts, SMS and messaging apps
           using the normal backup→restore pipeline.

        Progress is reported through the callback set via
        ``set_progress_callback()``.
        """
        self._begin_operation()

        # --- Validate --------------------------------------------------
        valid, msg = self.validate_devices(source_serial, target_serial)
        if not valid:
            log.error("Clone validation failed: %s", msg)
            self._transfer_progress = TransferProgress(
                phase="error",
                errors=[msg],
            )
            self._emit()
            return False

        src_info = self.adb.get_device_details(source_serial)
        tgt_info = self.adb.get_device_details(target_serial)

        self._transfer_progress = TransferProgress(
            phase="initializing",
            source_device=src_info.friendly_name(),
            target_device=tgt_info.friendly_name(),
        )
        self._emit()

        log.info(
            "Full storage clone: %s (%s) -> %s (%s)  path=%s",
            src_info.friendly_name(), source_serial,
            tgt_info.friendly_name(), target_serial,
            storage_path,
        )

        overall_success = True

        # ---- 1. Index source storage ----------------------------------
        self._transfer_progress.phase = "indexing"
        self._transfer_progress.sub_phase = "Memória interna (origem)"
        self._emit()

        remote_files = self.list_remote_files(
            source_serial, [storage_path],
            ignore_cache=ignore_cache,
            ignore_thumbnails=ignore_thumbnails,
        )
        total_files = len(remote_files)
        total_bytes = sum(s for _, s in remote_files)
        log.info(
            "Indexed %d files (%s) on source (%s)",
            total_files, format_bytes(total_bytes), storage_path,
        )

        if total_files == 0:
            log.warning("No files found on source under %s", storage_path)
            self._transfer_progress.errors.append(
                f"Nenhum arquivo encontrado em {storage_path}"
            )

        # ---- 1b. Verify target has enough free space ------------------
        if total_files > 0 and not self._cancel_flag.is_set():
            self._transfer_progress.phase = "indexing"
            self._transfer_progress.sub_phase = "Verificando espaço no destino"
            self._emit()

            target_free = self._get_free_bytes(target_serial)
            # Add a 5 % safety margin
            required = int(total_bytes * 1.05)

            if target_free >= 0:
                log.info(
                    "Target free space: %s  |  Required: %s",
                    format_bytes(target_free), format_bytes(required),
                )
                if target_free < required:
                    msg = (
                        f"Espaço insuficiente no destino.\n"
                        f"Necessário: {format_bytes(required)}\n"
                        f"Disponível: {format_bytes(target_free)}\n"
                        f"Faltam: {format_bytes(required - target_free)}"
                    )
                    log.error("Insufficient space on target: %s", msg)
                    self._transfer_progress.phase = "error"
                    self._transfer_progress.errors.append(msg)
                    self._emit()
                    return False
            else:
                log.warning("Could not determine free space on target; proceeding anyway")

        # ---- 2. Streamed transfer: pull batch → push batch → cleanup --
        # Instead of staging ALL files locally, process in chunks to
        # minimise local disk usage.  Each batch is: pull → push → delete.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        staging_dir = self.work_dir / f"clone_{ts}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        storage_staging = staging_dir / "storage"

        BATCH_MAX_FILES = 200
        BATCH_MAX_BYTES = 256 * 1024 * 1024  # 256 MB

        pulled_total = 0
        pushed_total = 0
        pull_errors_total = 0
        push_errors_total = 0
        files_pulled = 0
        files_pushed = 0

        # Split into batches
        batches: List[List[Tuple[str, int]]] = []
        cur_batch: List[Tuple[str, int]] = []
        cur_bytes = 0
        for entry in remote_files:
            cur_batch.append(entry)
            cur_bytes += entry[1]
            if len(cur_batch) >= BATCH_MAX_FILES or cur_bytes >= BATCH_MAX_BYTES:
                batches.append(cur_batch)
                cur_batch = []
                cur_bytes = 0
        if cur_batch:
            batches.append(cur_batch)

        total_batches = len(batches)

        for batch_idx, batch in enumerate(batches, 1):
            if self._cancel_flag.is_set():
                break

            batch_count = len(batch)
            log.info(
                "Streaming batch %d/%d  (%d files)",
                batch_idx, total_batches, batch_count,
            )

            self._transfer_progress.phase = "streaming"
            self._transfer_progress.sub_phase = (
                f"Lote {batch_idx}/{total_batches} — baixando da origem"
            )
            self._emit()

            # --- 2a. Pull batch to staging ---
            _pull_lock = threading.Lock()
            batch_pulled = 0
            batch_pull_errors = 0

            # Pre-create local dirs
            for remote_path, _ in batch:
                rel = remote_path[len(storage_path):].lstrip("/")
                local_path = storage_staging / rel
                local_path.parent.mkdir(parents=True, exist_ok=True)

            def _pull_one(remote_path: str, rel: str) -> bool:
                if self._cancel_flag.is_set():
                    return False
                local_path = storage_staging / rel
                try:
                    return self.adb.pull(remote_path, str(local_path), source_serial)
                except Exception as exc:
                    log.warning("Error pulling %s: %s", remote_path, exc)
                    return False

            pull_workers = self.accelerator.optimal_workers(
                batch_count,
                avg_size_bytes=sum(s for _, s in batch) // max(batch_count, 1),
            )

            with ThreadPoolExecutor(max_workers=pull_workers) as pool:
                futures = {}
                for remote_path, _ in batch:
                    rel = remote_path[len(storage_path):].lstrip("/")
                    fut = pool.submit(_pull_one, remote_path, rel)
                    futures[fut] = rel

                for fut in as_completed(futures):
                    rel = futures[fut]
                    files_pulled += 1
                    try:
                        if fut.result():
                            with _pull_lock:
                                batch_pulled += 1
                        else:
                            with _pull_lock:
                                batch_pull_errors += 1
                    except Exception:
                        with _pull_lock:
                            batch_pull_errors += 1

                    self._transfer_progress.current_item = rel
                    self._transfer_progress.percent = (
                        files_pulled / total_files * 40  # 0-40% (pull)
                    )
                    self._emit()

            pulled_total += batch_pulled
            pull_errors_total += batch_pull_errors

            if self._cancel_flag.is_set():
                break

            # --- 2b. Push staged batch to target ---
            self._transfer_progress.sub_phase = (
                f"Lote {batch_idx}/{total_batches} — enviando para destino"
            )
            self._emit()

            local_files = [
                f for f in storage_staging.rglob("*") if f.is_file()
            ]
            if not local_files:
                continue

            # Pre-create remote parent dirs in one batch
            parent_dirs = set()
            for lf in local_files:
                rel = lf.relative_to(storage_staging).as_posix()
                target_remote = f"{storage_path}/{rel}"
                parent_dirs.add("/".join(target_remote.split("/")[:-1]))

            dir_list = sorted(parent_dirs)
            chunk_size = 50
            for i in range(0, len(dir_list), chunk_size):
                chunk = dir_list[i:i + chunk_size]
                dirs_str = "' '".join(chunk)
                try:
                    self.adb.run_shell(f"mkdir -p '{dirs_str}'", target_serial)
                except Exception:
                    for d in chunk:
                        try:
                            self.adb.run_shell(f"mkdir -p '{d}'", target_serial)
                        except Exception:
                            pass

            _push_lock = threading.Lock()
            batch_pushed = 0
            batch_push_errors = 0

            def _push_one(local_file: Path, rel: str) -> bool:
                if self._cancel_flag.is_set():
                    return False
                target_remote = f"{storage_path}/{rel}"
                try:
                    return self.adb.push(str(local_file), target_remote, target_serial)
                except Exception as exc:
                    log.warning("Error pushing %s: %s", target_remote, exc)
                    return False

            push_workers = self.accelerator.optimal_workers(
                len(local_files),
                avg_size_bytes=(
                    sum(f.stat().st_size for f in local_files)
                    // max(len(local_files), 1)
                ),
            )

            with ThreadPoolExecutor(max_workers=push_workers) as pool:
                futures = {}
                for lf in local_files:
                    rel = lf.relative_to(storage_staging).as_posix()
                    fut = pool.submit(_push_one, lf, rel)
                    futures[fut] = rel

                for fut in as_completed(futures):
                    rel = futures[fut]
                    files_pushed += 1
                    try:
                        if fut.result():
                            with _push_lock:
                                batch_pushed += 1
                        else:
                            with _push_lock:
                                batch_push_errors += 1
                    except Exception:
                        with _push_lock:
                            batch_push_errors += 1

                    self._transfer_progress.current_item = rel
                    self._transfer_progress.percent = (
                        40 + files_pushed / total_files * 40  # 40-80% (push)
                    )
                    self._emit()

            pushed_total += batch_pushed
            push_errors_total += batch_push_errors

            # --- 2c. Cleanup staging for this batch ---
            # Remove files that were successfully pushed to free disk space
            for lf in local_files:
                try:
                    lf.unlink(missing_ok=True)
                except Exception:
                    pass
            # Remove empty directories
            for d in sorted(
                (p for p in storage_staging.rglob("*") if p.is_dir()),
                reverse=True,
            ):
                try:
                    d.rmdir()  # only removes if empty
                except OSError:
                    pass

            log.info(
                "Batch %d/%d done: pulled=%d pushed=%d  (batch errors: pull=%d push=%d)",
                batch_idx, total_batches,
                batch_pulled, batch_pushed,
                batch_pull_errors, batch_push_errors,
            )

        log.info(
            "Streamed transfer totals: pulled=%d/%d pushed=%d  (errors: pull=%d push=%d)",
            pulled_total, total_files, pushed_total,
            pull_errors_total, push_errors_total,
        )

        if pull_errors_total or push_errors_total:
            self._transfer_progress.errors.append(
                f"Ficheiros: {pull_errors_total} erros ao copiar da origem, "
                f"{push_errors_total} erros ao copiar para destino"
            )
            overall_success = False

        # ---- 3. Transfer apps, contacts, SMS, messaging ---------------
        if not self._cancel_flag.is_set():
            transfer_backup_dir = staging_dir / "app_transfer"
            transfer_backup_dir.mkdir(parents=True, exist_ok=True)

            backup_mgr = BackupManager(self.adb, transfer_backup_dir)
            restore_mgr = RestoreManager(self.adb, transfer_backup_dir)
            self._child_backup_mgr = backup_mgr
            self._child_restore_mgr = restore_mgr

            extra_steps = [
                ("apps", "Aplicativos"),
                ("contacts", "Contatos"),
                ("sms", "SMS"),
                ("messaging", "Apps de Mensagem"),
            ]
            step_pct_each = 20 / len(extra_steps)  # 80-100% range

            for step_idx, (step_key, step_label) in enumerate(extra_steps):
                if self._cancel_flag.is_set():
                    break

                base_pct = 80 + step_idx * step_pct_each
                self._transfer_progress.phase = "backing_up"
                self._transfer_progress.sub_phase = step_label
                self._transfer_progress.percent = base_pct
                self._emit()

                try:
                    if step_key == "apps":
                        manifest = backup_mgr.backup_apps(
                            source_serial, include_data=True
                        )
                        if manifest and not self._cancel_flag.is_set():
                            self._transfer_progress.phase = "restoring"
                            self._transfer_progress.sub_phase = step_label
                            self._transfer_progress.percent = base_pct + step_pct_each / 2
                            self._emit()
                            s, t = restore_mgr.restore_apps(
                                target_serial, manifest.backup_id,
                                restore_data=True,
                            )
                            if s < t:
                                self._transfer_progress.errors.append(
                                    f"Apps: {s}/{t} instalados"
                                )
                                overall_success = False

                    elif step_key == "contacts":
                        manifest = backup_mgr.backup_contacts(source_serial)
                        if manifest and not self._cancel_flag.is_set():
                            self._transfer_progress.phase = "restoring"
                            self._transfer_progress.sub_phase = step_label
                            self._transfer_progress.percent = base_pct + step_pct_each / 2
                            self._emit()
                            if not restore_mgr.restore_contacts(
                                target_serial, manifest.backup_id
                            ):
                                self._transfer_progress.errors.append(
                                    "Contatos: restauração pode estar incompleta"
                                )

                    elif step_key == "sms":
                        manifest = backup_mgr.backup_sms(source_serial)
                        if manifest and not self._cancel_flag.is_set():
                            self._transfer_progress.phase = "restoring"
                            self._transfer_progress.sub_phase = step_label
                            self._transfer_progress.percent = base_pct + step_pct_each / 2
                            self._emit()
                            if not restore_mgr.restore_sms(
                                target_serial, manifest.backup_id
                            ):
                                self._transfer_progress.errors.append(
                                    "SMS: restauração pode estar incompleta"
                                )

                    elif step_key == "messaging":
                        manifest = backup_mgr.backup_messaging_apps(source_serial)
                        if manifest and not self._cancel_flag.is_set():
                            self._transfer_progress.phase = "restoring"
                            self._transfer_progress.sub_phase = step_label
                            self._transfer_progress.percent = base_pct + step_pct_each / 2
                            self._emit()
                            if not restore_mgr.restore_messaging_apps(
                                target_serial, manifest.backup_id
                            ):
                                self._transfer_progress.errors.append(
                                    "Apps de mensagem: restauração pode estar incompleta"
                                )

                except Exception as exc:
                    log.warning("Clone extra step '%s' failed: %s", step_key, exc)
                    self._transfer_progress.errors.append(f"{step_label}: {exc}")
                    overall_success = False

        # ---- 5. Finish -----------------------------------------------
        self._transfer_progress.phase = (
            "complete" if overall_success else "complete_with_errors"
        )
        self._transfer_progress.percent = 100
        self._emit()
        self._start_time = None

        log.info(
            "Full storage clone %s in %.1fs  |  files pulled=%d pushed=%d  |  errors=%s",
            "completed" if overall_success else "completed with errors",
            self._transfer_progress.elapsed_seconds,
            pulled_total,
            pushed_total,
            self._transfer_progress.errors or "none",
        )

        self._child_backup_mgr = None
        self._child_restore_mgr = None
        return overall_success

