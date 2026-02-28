"""WhatsApp cross-platform media transfer.

Transfers WhatsApp **media files** (photos, videos, voice notes, documents,
stickers) between Android and iOS.  Chat history migration is delegated to
WhatsApp's official transfer tool (Move-to-iOS / Move-to-Android) because the
encrypted database formats (crypt14/15 on Android, ChatStorage.sqlite on iOS)
cannot be reliably converted without WhatsApp's private key infrastructure.

This module focuses on:
- Detecting WhatsApp installation & media paths on both platforms
- Pulling all media from the source device
- Pushing media to the matching directories on the target device
- Providing guidance on official chat migration
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

from .device_interface import DeviceInterface, DeviceManager, DevicePlatform

log = logging.getLogger(__name__)

# ── Android WhatsApp media structure ──────────────────────────────────────
# Newer Android (11+) stores under /sdcard/Android/media/com.whatsapp/WhatsApp/
# Older Android stores under /sdcard/WhatsApp/
_ANDROID_WA_ROOTS = [
    "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp",
    "/storage/emulated/0/WhatsApp",
    "/sdcard/Android/media/com.whatsapp/WhatsApp",
    "/sdcard/WhatsApp",
]

_ANDROID_WA_BIZ_ROOTS = [
    "/storage/emulated/0/Android/media/com.whatsapp.w4b/WhatsApp Business",
    "/storage/emulated/0/WhatsApp Business",
    "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business",
    "/sdcard/WhatsApp Business",
]

# Sub-folders that contain transferable media
_WA_MEDIA_SUBDIRS = [
    "Media/WhatsApp Images",
    "Media/WhatsApp Video",
    "Media/WhatsApp Audio",
    "Media/WhatsApp Voice Notes",
    "Media/WhatsApp Documents",
    "Media/WhatsApp Stickers",
    "Media/WhatsApp Animated Gifs",
]

# ── iOS WhatsApp media structure ──────────────────────────────────────────
# On iOS, WhatsApp media lives inside the app container accessible via iTunes
# backup or AFC (jailbroken).  Media within a backup is under:
#   AppDomainGroup-group.net.whatsapp.WhatsApp.shared/Message/Media/
_IOS_WA_BACKUP_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
_IOS_WA_MEDIA_REL = "Message/Media"
_IOS_WA_PROFILE_REL = "Media/Profile"

# When pushing media *to* an iPhone we place files in the Camera Roll via AFC
# or in /Downloads for documents.
_IOS_CAMERA_ROLL = "/DCIM"
_IOS_DOWNLOADS = "/Downloads"


# ── Progress ──────────────────────────────────────────────────────────────
@dataclass
class WhatsAppTransferProgress:
    """Progress update for WhatsApp transfer."""

    phase: str = ""           # "scan", "pull", "push", "done"
    sub_phase: str = ""       # e.g. "WhatsApp Images"
    current_item: str = ""    # file path being transferred
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    percent: float = 0.0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ── Configuration ─────────────────────────────────────────────────────────
@dataclass
class WhatsAppTransferConfig:
    """What to transfer."""

    images: bool = True
    video: bool = True
    audio: bool = True
    voice_notes: bool = True
    documents: bool = True
    stickers: bool = True
    animated_gifs: bool = True
    include_business: bool = False   # also transfer WhatsApp Business media

    @property
    def selected_subdirs(self) -> List[str]:
        """Return the sub-directories the user opted to transfer."""
        mapping = [
            (self.images, "Media/WhatsApp Images"),
            (self.video, "Media/WhatsApp Video"),
            (self.audio, "Media/WhatsApp Audio"),
            (self.voice_notes, "Media/WhatsApp Voice Notes"),
            (self.documents, "Media/WhatsApp Documents"),
            (self.stickers, "Media/WhatsApp Stickers"),
            (self.animated_gifs, "Media/WhatsApp Animated Gifs"),
        ]
        return [path for enabled, path in mapping if enabled]


# ── Transfer manager ──────────────────────────────────────────────────────
class WhatsAppTransferManager:
    """Orchestrates WhatsApp media transfer between Android and iOS."""

    def __init__(self, device_mgr: DeviceManager, temp_dir: Optional[Path] = None):
        self._device_mgr = device_mgr
        self._temp_dir = temp_dir or Path(tempfile.gettempdir()) / "adb_toolkit_wa"
        self._cancel = threading.Event()
        self._progress_cb: Optional[Callable[[WhatsAppTransferProgress], None]] = None
        self._errors: List[str] = []
        self._warnings: List[str] = []

    # ── Public API ────────────────────────────────────────────────────────
    def set_progress_callback(self, cb: Callable[[WhatsAppTransferProgress], None]):
        self._progress_cb = cb

    def cancel(self):
        self._cancel.set()

    def transfer(
        self,
        src_serial: str,
        tgt_serial: str,
        config: Optional[WhatsAppTransferConfig] = None,
    ) -> bool:
        """Run WhatsApp media transfer from *src_serial* to *tgt_serial*.

        Returns ``True`` if completed successfully (possibly with warnings).
        Raises on fatal errors.
        """
        cfg = config or WhatsAppTransferConfig()
        self._cancel.clear()
        self._errors.clear()
        self._warnings.clear()

        src_iface = self._device_mgr.get_interface(src_serial)
        tgt_iface = self._device_mgr.get_interface(tgt_serial)
        if not src_iface or not tgt_iface:
            raise RuntimeError("Dispositivo de origem ou destino não encontrado.")

        src_platform = self._detect_platform(src_serial)
        tgt_platform = self._detect_platform(tgt_serial)

        log.info("WhatsApp transfer %s(%s) -> %s(%s)",
                 src_serial, src_platform.name, tgt_serial, tgt_platform.name)

        # Always warn about chat history
        self._warnings.append(
            "💬 Histórico de conversas NÃO é transferido automaticamente. "
            "Use a ferramenta oficial do WhatsApp: no dispositivo de destino, "
            "durante a configuração do WhatsApp, selecione 'Transferir conversas'."
        )

        # Phase 1: Scan source
        self._emit(WhatsAppTransferProgress(phase="scan", sub_phase="Detectando mídia..."))
        src_root, file_map = self._scan_source(src_serial, src_iface, src_platform, cfg)

        if not file_map:
            self._warnings.append("Nenhuma mídia do WhatsApp encontrada no dispositivo de origem.")
            self._emit(WhatsAppTransferProgress(
                phase="done", percent=100.0, warnings=list(self._warnings),
            ))
            return True

        total_files = sum(len(files) for files in file_map.values())
        log.info("Found %d WhatsApp media files across %d categories",
                 total_files, len(file_map))

        # Phase 2: Pull from source to local temp
        staging = self._temp_dir / "wa_staging"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        pulled = self._pull_media(src_serial, src_iface, src_platform,
                                  src_root, file_map, staging, total_files)

        if self._cancel.is_set():
            self._emit(WhatsAppTransferProgress(phase="done", percent=100.0,
                                                 errors=["Cancelado pelo usuário."]))
            return False

        # Phase 3: Push to target
        self._push_media(tgt_serial, tgt_iface, tgt_platform,
                         file_map, staging, pulled)

        # Cleanup
        shutil.rmtree(staging, ignore_errors=True)

        success = len(self._errors) == 0
        self._emit(WhatsAppTransferProgress(
            phase="done", percent=100.0,
            files_done=pulled, files_total=total_files,
            errors=list(self._errors),
            warnings=list(self._warnings),
        ))
        return success

    def get_official_migration_guide(self, src_platform: DevicePlatform,
                                      tgt_platform: DevicePlatform) -> str:
        """Return user-facing instructions for official WhatsApp chat migration."""
        if src_platform == DevicePlatform.ANDROID and tgt_platform == DevicePlatform.IOS:
            return (
                "📱 Transferir conversas do Android para iPhone:\n"
                "1. Instale o app 'Move to iOS' no Android\n"
                "2. No iPhone, durante a configuração inicial, escolha 'Migrar do Android'\n"
                "3. No WhatsApp do iPhone, durante o setup, toque em 'Transferir conversas'\n"
                "4. Siga as instruções na tela — ambos dispositivos precisam estar na mesma rede Wi-Fi\n\n"
                "⚠️ O iPhone precisa estar em configuração inicial (novo ou resetado).\n"
                "📖 https://faq.whatsapp.com/530788685226498"
            )
        elif src_platform == DevicePlatform.IOS and tgt_platform == DevicePlatform.ANDROID:
            return (
                "📱 Transferir conversas do iPhone para Android:\n"
                "1. Atualize o WhatsApp para a versão mais recente em ambos\n"
                "2. No Android, instale o WhatsApp e abra\n"
                "3. No iPhone, vá em WhatsApp > Configurações > Conversas > Transferir conversas\n"
                "4. Escaneie o QR Code exibido no Android\n"
                "5. Aguarde — o cabo USB-C/Lightning pode acelerar a transferência\n\n"
                "⚠️ O Android precisa estar com conta Google logada.\n"
                "📖 https://faq.whatsapp.com/590889592348498"
            )
        else:
            return (
                "📱 Transferir conversas entre dispositivos da mesma plataforma:\n"
                "Use o backup do Google Drive (Android→Android) ou iCloud (iOS→iOS)."
            )

    # ── Internal ──────────────────────────────────────────────────────────
    def _detect_platform(self, serial: str) -> DevicePlatform:
        for dev in self._device_mgr.list_all_devices():
            if dev.serial == serial:
                return dev.platform
        return DevicePlatform.UNKNOWN

    def _emit(self, progress: WhatsAppTransferProgress):
        if self._progress_cb:
            try:
                self._progress_cb(progress)
            except Exception:
                pass

    # ── Scan ──────────────────────────────────────────────────────────────
    def _scan_source(
        self,
        serial: str,
        iface: DeviceInterface,
        platform: DevicePlatform,
        cfg: WhatsAppTransferConfig,
    ) -> Tuple[str, Dict[str, List[str]]]:
        """Scan source device for WhatsApp media.

        Returns (root_path, {subdir: [file_paths]}).
        """
        file_map: Dict[str, List[str]] = {}

        if platform == DevicePlatform.ANDROID:
            return self._scan_android(serial, iface, cfg)
        elif platform == DevicePlatform.IOS:
            return self._scan_ios(serial, iface, cfg)
        else:
            self._errors.append("Plataforma de origem desconhecida.")
            return "", file_map

    def _scan_android(
        self,
        serial: str,
        iface: DeviceInterface,
        cfg: WhatsAppTransferConfig,
    ) -> Tuple[str, Dict[str, List[str]]]:
        """Find WhatsApp media on Android device."""
        file_map: Dict[str, List[str]] = {}
        roots = list(_ANDROID_WA_ROOTS)
        if cfg.include_business:
            roots.extend(_ANDROID_WA_BIZ_ROOTS)

        found_root = ""
        for root in roots:
            if iface.file_exists(root, serial):
                found_root = root
                log.info("WhatsApp root found: %s", root)
                break

        if not found_root:
            return "", file_map

        for subdir in cfg.selected_subdirs:
            if self._cancel.is_set():
                break
            full_path = f"{found_root}/{subdir}"
            if not iface.file_exists(full_path, serial):
                continue

            self._emit(WhatsAppTransferProgress(
                phase="scan", sub_phase=subdir.split("/")[-1],
            ))

            try:
                entries = iface.list_dir(full_path, serial)
                files = [e for e in entries if not e.endswith("/")]
                if files:
                    file_map[subdir] = files
                    log.info(" %s: %d files", subdir, len(files))
            except Exception as exc:
                log.warning("Error scanning %s: %s", full_path, exc)

        return found_root, file_map

    def _scan_ios(
        self,
        serial: str,
        iface: DeviceInterface,
        cfg: WhatsAppTransferConfig,
    ) -> Tuple[str, Dict[str, List[str]]]:
        """Find WhatsApp media on iOS device.

        On a non-jailbroken iPhone, WhatsApp media is accessible through an
        iTunes-style backup.  If the backup doesn't exist we attempt to
        create one.  The media inside the backup is organized differently
        than Android — files are referenced by SHA hash in the Manifest.db.

        For simplicity, we look for media files in common photo/video
        extensions that are present in the DCIM or Downloads paths (which
        ARE accessible via AFC without backup).
        """
        file_map: Dict[str, List[str]] = {}

        # On non-jailbroken iOS, WhatsApp media isn't directly accessible.
        # However, photos/videos sent via WhatsApp that were saved to
        # Camera Roll ARE accessible via AFC.
        media_extensions = {
            ".jpg", ".jpeg", ".png", ".heic", ".gif",
            ".mp4", ".mov", ".3gp",
            ".opus", ".m4a", ".mp3", ".aac",
            ".pdf", ".docx", ".xlsx",
        }

        for ios_path in [_IOS_CAMERA_ROLL, _IOS_DOWNLOADS]:
            if self._cancel.is_set():
                break
            try:
                if not iface.file_exists(ios_path, serial):
                    continue
                entries = iface.list_dir(ios_path, serial)
                media_files = [
                    e for e in entries
                    if not e.endswith("/") and
                    os.path.splitext(e)[1].lower() in media_extensions
                ]
                if media_files:
                    file_map[ios_path] = media_files
            except Exception as exc:
                log.warning("Error scanning iOS %s: %s", ios_path, exc)

        if not file_map:
            self._warnings.append(
                "Mídia do WhatsApp no iOS não é diretamente acessível sem backup. "
                "Apenas fotos/vídeos salvos no Rolo da Câmera serão transferidos."
            )

        return "", file_map

    # ── Pull ──────────────────────────────────────────────────────────────
    def _pull_media(
        self,
        serial: str,
        iface: DeviceInterface,
        platform: DevicePlatform,
        src_root: str,
        file_map: Dict[str, List[str]],
        staging: Path,
        total_files: int,
    ) -> int:
        """Pull media from source device into local staging dir."""
        done = 0
        for subdir, files in file_map.items():
            if self._cancel.is_set():
                break

            cat_name = subdir.split("/")[-1] if "/" in subdir else subdir
            local_dir = staging / cat_name.replace(" ", "_")
            local_dir.mkdir(parents=True, exist_ok=True)

            for fname in files:
                if self._cancel.is_set():
                    break

                if platform == DevicePlatform.ANDROID:
                    remote_path = f"{src_root}/{subdir}/{fname}"
                else:
                    remote_path = f"{subdir}/{fname}" if subdir.startswith("/") \
                        else f"/{subdir}/{fname}"

                local_path = local_dir / fname
                pct = (done / total_files * 50) if total_files else 0  # 0-50%

                self._emit(WhatsAppTransferProgress(
                    phase="pull", sub_phase=cat_name,
                    current_item=fname, files_done=done,
                    files_total=total_files, percent=pct,
                ))

                try:
                    iface.pull(remote_path, str(local_path), serial)
                    done += 1
                except Exception as exc:
                    log.warning("Pull failed: %s — %s", remote_path, exc)
                    self._errors.append(f"Pull falhou: {fname}")

        return done

    # ── Push ──────────────────────────────────────────────────────────────
    def _push_media(
        self,
        serial: str,
        iface: DeviceInterface,
        platform: DevicePlatform,
        file_map: Dict[str, List[str]],
        staging: Path,
        total_files: int,
    ):
        """Push staged media to target device."""
        done = 0

        if platform == DevicePlatform.ANDROID:
            self._push_to_android(serial, iface, file_map, staging, total_files)
        elif platform == DevicePlatform.IOS:
            self._push_to_ios(serial, iface, file_map, staging, total_files)
        else:
            self._errors.append("Plataforma de destino desconhecida.")

    def _push_to_android(
        self,
        serial: str,
        iface: DeviceInterface,
        file_map: Dict[str, List[str]],
        staging: Path,
        total_files: int,
    ):
        """Push media to Android WhatsApp directories."""
        # Find or create WhatsApp media root
        tgt_root = ""
        for root in _ANDROID_WA_ROOTS:
            if iface.file_exists(root, serial):
                tgt_root = root
                break

        if not tgt_root:
            # Default to new-style path
            tgt_root = _ANDROID_WA_ROOTS[0]
            try:
                iface.mkdir(tgt_root, serial)
            except Exception:
                tgt_root = _ANDROID_WA_ROOTS[2]  # fallback to /sdcard/ variant
                iface.mkdir(tgt_root, serial)

        done = 0
        for subdir, files in file_map.items():
            if self._cancel.is_set():
                break

            cat_name = subdir.split("/")[-1] if "/" in subdir else subdir
            local_dir = staging / cat_name.replace(" ", "_")

            # Map iOS paths to Android WhatsApp subdirectories
            android_subdir = self._map_to_android_subdir(subdir, cat_name)
            remote_dir = f"{tgt_root}/{android_subdir}"

            try:
                iface.mkdir(remote_dir, serial)
            except Exception:
                pass

            for fname in files:
                if self._cancel.is_set():
                    break

                local_path = local_dir / fname
                if not local_path.exists():
                    continue

                remote_path = f"{remote_dir}/{fname}"
                pct = 50 + (done / total_files * 50) if total_files else 50

                self._emit(WhatsAppTransferProgress(
                    phase="push", sub_phase=cat_name,
                    current_item=fname, files_done=done,
                    files_total=total_files, percent=pct,
                ))

                try:
                    iface.push(str(local_path), remote_path, serial)
                    done += 1
                except Exception as exc:
                    log.warning("Push failed: %s — %s", remote_path, exc)
                    self._errors.append(f"Push falhou: {fname}")

    def _push_to_ios(
        self,
        serial: str,
        iface: DeviceInterface,
        file_map: Dict[str, List[str]],
        staging: Path,
        total_files: int,
    ):
        """Push media to iOS device.

        Since we cannot write directly to WhatsApp's container on a
        non-jailbroken iPhone, we push photos/videos to the Camera Roll
        and documents to Downloads.
        """
        self._warnings.append(
            "No iOS, as mídias do WhatsApp são salvas no Rolo da Câmera "
            "e em Arquivos/Downloads (documentos). Abra o WhatsApp no "
            "iPhone e re-envie ou salve conforme necessário."
        )

        photo_exts = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp"}
        video_exts = {".mp4", ".mov", ".3gp", ".avi"}
        done = 0

        for subdir, files in file_map.items():
            if self._cancel.is_set():
                break

            cat_name = subdir.split("/")[-1] if "/" in subdir else subdir
            local_dir = staging / cat_name.replace(" ", "_")

            for fname in files:
                if self._cancel.is_set():
                    break

                local_path = local_dir / fname
                if not local_path.exists():
                    continue

                ext = os.path.splitext(fname)[1].lower()
                if ext in photo_exts or ext in video_exts:
                    remote_path = f"{_IOS_CAMERA_ROLL}/{fname}"
                else:
                    remote_path = f"{_IOS_DOWNLOADS}/{fname}"

                pct = 50 + (done / total_files * 50) if total_files else 50

                self._emit(WhatsAppTransferProgress(
                    phase="push", sub_phase=cat_name,
                    current_item=fname, files_done=done,
                    files_total=total_files, percent=pct,
                ))

                try:
                    iface.push(str(local_path), remote_path, serial)
                    done += 1
                except Exception as exc:
                    log.warning("Push to iOS failed: %s — %s", remote_path, exc)
                    self._errors.append(f"Push falhou: {fname}")

    # ── Helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _map_to_android_subdir(original_subdir: str, cat_name: str) -> str:
        """Map a source sub-directory name to Android WhatsApp convention."""
        # If already Android-style, keep it
        if original_subdir.startswith("Media/WhatsApp"):
            return original_subdir

        # iOS → Android mapping: push photos/videos/docs into matching folders
        lower = cat_name.lower()
        if "dcim" in lower or "photo" in lower or "image" in lower:
            return "Media/WhatsApp Images"
        elif "video" in lower or "mov" in lower:
            return "Media/WhatsApp Video"
        elif "audio" in lower or "voice" in lower:
            return "Media/WhatsApp Audio"
        elif "document" in lower or "download" in lower:
            return "Media/WhatsApp Documents"
        elif "sticker" in lower:
            return "Media/WhatsApp Stickers"
        else:
            return "Media/WhatsApp Documents"  # default catch-all
