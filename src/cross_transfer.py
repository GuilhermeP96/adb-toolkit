"""
cross_transfer.py - Cross-platform transfer manager (Android ↔ iOS).

Orchestrates data transfer between devices of different platforms:
  - Photos / Videos / Music / Documents (file-based, via staging)
  - Contacts (VCF as common format)
  - SMS (JSON as common format — iOS import limited)
  - Calendar (ICS as common format)

Does NOT transfer:
  - Apps (incompatible between platforms)

Uses:
  - DeviceInterface ABC for platform-agnostic file/data operations
  - FormatConverter for cross-platform format translations (HEIC→JPEG, etc.)
  - DeviceManager to discover and route to the correct interface
"""

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .device_interface import (
    DeviceInterface,
    DeviceManager,
    DevicePlatform,
    UnifiedDeviceInfo,
)
from .format_converter import (
    CalendarConverter,
    PhotoConverter,
    SMSConverter,
    VCardConverter,
)

log = logging.getLogger("adb_toolkit.cross_transfer")


# ---------------------------------------------------------------------------
# Progress model
# ---------------------------------------------------------------------------
@dataclass
class CrossTransferProgress:
    """Progress of a cross-platform transfer."""
    phase: str = ""
    sub_phase: str = ""
    current_item: str = ""
    items_done: int = 0
    items_total: int = 0
    percent: float = 0.0
    source_device: str = ""
    target_device: str = ""
    source_platform: str = ""
    target_platform: str = ""
    elapsed_seconds: float = 0.0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Transfer config
# ---------------------------------------------------------------------------
@dataclass
class CrossTransferConfig:
    """What to transfer cross-platform."""
    photos: bool = True
    videos: bool = True
    music: bool = True
    documents: bool = True
    contacts: bool = True
    sms: bool = True
    calendar: bool = True
    convert_heic: bool = True   # Auto-convert HEIC→JPEG when target is Android
    ignore_cache: bool = True
    ignore_thumbnails: bool = True


# ---------------------------------------------------------------------------
# Cross-Platform Transfer Manager
# ---------------------------------------------------------------------------
class CrossPlatformTransferManager:
    """Manages transfers between devices of different platforms."""

    def __init__(self, device_manager: DeviceManager, work_dir: Optional[Path] = None):
        self.dm = device_manager
        self.work_dir = work_dir or Path("transfers")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._cancel_flag = threading.Event()
        self._progress_cb: Optional[Callable[[CrossTransferProgress], None]] = None
        self._progress = CrossTransferProgress()
        self._start_time: Optional[float] = None

    def set_progress_callback(self, cb: Callable[[CrossTransferProgress], None]):
        self._progress_cb = cb

    def cancel(self):
        self._cancel_flag.set()

    def _emit(self):
        if self._start_time is not None:
            self._progress.elapsed_seconds = time.time() - self._start_time
        if self._progress_cb:
            try:
                self._progress_cb(self._progress)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Main transfer entry point
    # ------------------------------------------------------------------
    def transfer(
        self,
        source_serial: str,
        target_serial: str,
        config: Optional[CrossTransferConfig] = None,
    ) -> bool:
        """Execute a cross-platform transfer.

        Returns True if everything succeeded, False otherwise.
        """
        self._cancel_flag.clear()
        self._start_time = time.time()
        config = config or CrossTransferConfig()

        src_iface = self.dm.get_interface(source_serial)
        tgt_iface = self.dm.get_interface(target_serial)
        if not src_iface or not tgt_iface:
            self._progress.phase = "error"
            self._progress.errors.append("Dispositivo(s) não encontrado(s)")
            self._emit()
            return False

        src_info = src_iface.get_device_details(source_serial)
        tgt_info = tgt_iface.get_device_details(target_serial)

        self._progress = CrossTransferProgress(
            phase="initializing",
            source_device=src_info.friendly_name(),
            target_device=tgt_info.friendly_name(),
            source_platform=src_info.platform.value,
            target_platform=tgt_info.platform.value,
        )
        self._emit()

        log.info(
            "Cross-platform transfer: %s (%s/%s) → %s (%s/%s)",
            src_info.friendly_name(), src_info.platform.value, source_serial,
            tgt_info.friendly_name(), tgt_info.platform.value, target_serial,
        )

        overall_ok = True

        # --- Staging directory ---
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        staging = self.work_dir / f"cross_{ts}"
        staging.mkdir(parents=True, exist_ok=True)

        steps: List[Tuple[str, str, Callable]] = []
        if config.contacts:
            steps.append(("contacts", "Contatos", lambda: self._transfer_contacts(
                src_iface, tgt_iface, source_serial, target_serial, staging
            )))
        if config.sms:
            steps.append(("sms", "SMS", lambda: self._transfer_sms(
                src_iface, tgt_iface, source_serial, target_serial, staging
            )))
        if config.calendar:
            steps.append(("calendar", "Calendário", lambda: self._transfer_calendar(
                src_iface, tgt_iface, source_serial, target_serial, staging
            )))
        if config.photos:
            steps.append(("photos", "Fotos", lambda: self._transfer_media(
                src_iface, tgt_iface, source_serial, target_serial,
                staging, "photos", config,
            )))
        if config.videos:
            steps.append(("videos", "Vídeos", lambda: self._transfer_media(
                src_iface, tgt_iface, source_serial, target_serial,
                staging, "videos", config,
            )))
        if config.music:
            steps.append(("music", "Músicas", lambda: self._transfer_media(
                src_iface, tgt_iface, source_serial, target_serial,
                staging, "music", config,
            )))
        if config.documents:
            steps.append(("documents", "Documentos", lambda: self._transfer_media(
                src_iface, tgt_iface, source_serial, target_serial,
                staging, "documents", config,
            )))

        total_steps = len(steps)
        for idx, (key, label, fn) in enumerate(steps):
            if self._cancel_flag.is_set():
                break
            self._progress.phase = key
            self._progress.sub_phase = label
            self._progress.percent = idx / total_steps * 100
            self._emit()

            try:
                ok = fn()
                if not ok:
                    overall_ok = False
            except Exception as exc:
                log.warning("Cross-transfer step '%s' failed: %s", key, exc)
                self._progress.errors.append(f"{label}: {exc}")
                overall_ok = False

        # --- Finish ---
        self._progress.phase = "complete" if overall_ok else "complete_with_errors"
        self._progress.percent = 100
        self._emit()
        self._start_time = None

        log.info(
            "Cross-platform transfer %s in %.1fs  errors=%s",
            "completed" if overall_ok else "completed with errors",
            self._progress.elapsed_seconds,
            self._progress.errors or "none",
        )
        return overall_ok

    # ------------------------------------------------------------------
    # Step: Contacts
    # ------------------------------------------------------------------
    def _transfer_contacts(
        self,
        src: DeviceInterface,
        tgt: DeviceInterface,
        src_serial: str,
        tgt_serial: str,
        staging: Path,
    ) -> bool:
        """Transfer contacts via VCF as the common format."""
        self._progress.current_item = "Exportando contatos..."
        self._emit()

        contact_dir = staging / "contacts"
        vcf_path = src.export_contacts(src_serial, contact_dir)
        if not vcf_path:
            self._progress.warnings.append("Nenhum contato encontrado na origem")
            return True  # Not a hard failure

        self._progress.current_item = "Importando contatos..."
        self._emit()

        # Verify the VCF has content
        contacts = VCardConverter.parse_vcf(vcf_path)
        log.info("Transferring %d contacts", len(contacts))

        return tgt.import_contacts(tgt_serial, vcf_path)

    # ------------------------------------------------------------------
    # Step: SMS
    # ------------------------------------------------------------------
    def _transfer_sms(
        self,
        src: DeviceInterface,
        tgt: DeviceInterface,
        src_serial: str,
        tgt_serial: str,
        staging: Path,
    ) -> bool:
        """Transfer SMS. Note: iOS import is very limited."""
        self._progress.current_item = "Exportando SMS..."
        self._emit()

        sms_dir = staging / "sms"
        json_path = src.export_sms(src_serial, sms_dir)
        if not json_path:
            self._progress.warnings.append("Nenhuma SMS encontrada na origem")
            return True

        entries = SMSConverter.parse_android_json(json_path)
        log.info("Transferring %d SMS messages", len(entries))

        self._progress.current_item = "Importando SMS..."
        self._emit()

        # Warn about iOS limitation
        if tgt.platform() == DevicePlatform.IOS:
            self._progress.warnings.append(
                "iOS não permite importação programática de SMS. "
                "As mensagens foram salvas para referência."
            )

        return tgt.import_sms(tgt_serial, json_path)

    # ------------------------------------------------------------------
    # Step: Calendar
    # ------------------------------------------------------------------
    def _transfer_calendar(
        self,
        src: DeviceInterface,
        tgt: DeviceInterface,
        src_serial: str,
        tgt_serial: str,
        staging: Path,
    ) -> bool:
        """Transfer calendar events via ICS."""
        self._progress.current_item = "Exportando calendário..."
        self._emit()

        cal_dir = staging / "calendar"
        cal_dir.mkdir(parents=True, exist_ok=True)

        # Try pulling calendar from typical locations
        if src.platform() == DevicePlatform.ANDROID:
            # Android: export via content provider
            from .adb_core import ADBCore
            if hasattr(src, "adb") and isinstance(src.adb, ADBCore):
                out = src.run_shell(
                    "content query --uri content://com.android.calendar/events "
                    "--projection title:dtstart:dtend:eventLocation:description",
                    src_serial, timeout=30,
                )
                if out and "Error" not in out:
                    events: List[CalendarEvent] = []
                    for line in out.splitlines():
                        ev = CalendarEvent()
                        m = re.search(r"title=([^,}]+)", line)
                        if m:
                            ev.summary = m.group(1).strip()
                        m = re.search(r"dtstart=(\d+)", line)
                        if m:
                            ev.dtstart = datetime.fromtimestamp(
                                int(m.group(1)) / 1000, tz=timezone.utc
                            ).strftime("%Y%m%dT%H%M%SZ")
                        m = re.search(r"dtend=(\d+)", line)
                        if m:
                            ev.dtend = datetime.fromtimestamp(
                                int(m.group(1)) / 1000, tz=timezone.utc
                            ).strftime("%Y%m%dT%H%M%SZ")
                        m = re.search(r"eventLocation=([^,}]+)", line)
                        if m:
                            ev.location = m.group(1).strip()
                        if ev.summary:
                            events.append(ev)

                    if events:
                        ics_path = CalendarConverter.write_ics(events, cal_dir / "calendar.ics")
                        self._progress.current_item = "Importando calendário..."
                        self._emit()
                        return tgt.push(str(ics_path), "/Downloads/calendar.ics", tgt_serial)

        self._progress.warnings.append("Exportação de calendário não disponível")
        return True

    # ------------------------------------------------------------------
    # Step: Media (photos/videos/music/documents)
    # ------------------------------------------------------------------
    def _transfer_media(
        self,
        src: DeviceInterface,
        tgt: DeviceInterface,
        src_serial: str,
        tgt_serial: str,
        staging: Path,
        category: str,
        config: CrossTransferConfig,
    ) -> bool:
        """Transfer media files with optional HEIC→JPEG conversion."""
        src_paths = src.get_media_paths(src_serial).get(category, [])
        tgt_paths = tgt.get_media_paths(tgt_serial).get(category, [])

        if not src_paths:
            return True
        if not tgt_paths:
            self._progress.warnings.append(
                f"Sem caminho de destino para {category}"
            )
            return True

        target_base = tgt_paths[0]
        tgt_platform = tgt.platform().value

        media_staging = staging / category
        media_staging.mkdir(parents=True, exist_ok=True)

        total_pulled = 0
        total_pushed = 0
        errors = 0

        for src_path in src_paths:
            if self._cancel_flag.is_set():
                break

            # List files in source directory
            try:
                entries = src.list_dir(src_path, src_serial)
            except Exception:
                entries = []

            for entry in entries:
                if self._cancel_flag.is_set():
                    break

                remote_file = f"{src_path}/{entry}"
                local_file = media_staging / entry

                self._progress.current_item = entry
                self._emit()

                # Pull from source
                if src.pull(remote_file, str(local_file), src_serial):
                    total_pulled += 1

                    # Convert if needed (HEIC → JPEG for Android targets)
                    if config.convert_heic:
                        local_file = PhotoConverter.convert_if_needed(
                            local_file, tgt_platform, media_staging
                        )

                    # Push to target
                    target_remote = f"{target_base}/{local_file.name}"
                    if tgt.push(str(local_file), target_remote, tgt_serial):
                        total_pushed += 1
                    else:
                        errors += 1

                    # Cleanup staging
                    try:
                        local_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                else:
                    errors += 1

        log.info(
            "Media '%s': pulled=%d pushed=%d errors=%d",
            category, total_pulled, total_pushed, errors,
        )
        if errors:
            self._progress.errors.append(
                f"{category}: {errors} erro(s)"
            )
            return False
        return True
