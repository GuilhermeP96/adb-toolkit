"""
device_explorer.py - Device file tree browser widget for customtkinter.

Provides:
  - Lazy-loading tree view of an Android device's file system via ADB
  - Checkbox selection for backup/transfer operations
  - Handles path variations across different Android versions/OEMs
  - Detects common Android directory locations automatically
"""

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable, Dict, List, Optional, Set, Tuple

import customtkinter as ctk

log = logging.getLogger("adb_toolkit.device_explorer")


# ---------------------------------------------------------------------------
# Android path variations — auto-detection for common directories
# ---------------------------------------------------------------------------
ANDROID_KNOWN_DIRS = {
    "internal_storage": [
        "/sdcard",
        "/storage/emulated/0",
        "/storage/self/primary",
        "/mnt/sdcard",
    ],
    "dcim": [
        "/sdcard/DCIM",
        "/storage/emulated/0/DCIM",
    ],
    "pictures": [
        "/sdcard/Pictures",
        "/storage/emulated/0/Pictures",
    ],
    "downloads": [
        "/sdcard/Download",
        "/storage/emulated/0/Download",
        "/sdcard/Downloads",
    ],
    "documents": [
        "/sdcard/Documents",
        "/storage/emulated/0/Documents",
    ],
    "movies": [
        "/sdcard/Movies",
        "/storage/emulated/0/Movies",
    ],
    "music": [
        "/sdcard/Music",
        "/storage/emulated/0/Music",
    ],
    "external_sd": [
        "/storage/sdcard1",
        "/storage/extSdCard",
        "/mnt/extSdCard",
        "/mnt/external_sd",
    ],
}


# ---------------------------------------------------------------------------
# Messaging apps — known data paths (internal storage + app-specific)
# ---------------------------------------------------------------------------
MESSAGING_APPS = {
    "whatsapp": {
        "name": "WhatsApp",
        "icon": "💬",
        "packages": [
            "com.whatsapp",
        ],
        "media_paths": [
            "/sdcard/WhatsApp",
            "/sdcard/Android/media/com.whatsapp",
            "/storage/emulated/0/WhatsApp",
            "/storage/emulated/0/Android/media/com.whatsapp",
        ],
        "data_paths": [
            "/data/data/com.whatsapp",
        ],
        "description": "Mensagens, mídias, backups locais do WhatsApp",
    },
    "whatsapp_business": {
        "name": "WhatsApp Business",
        "icon": "💼",
        "packages": [
            "com.whatsapp.w4b",
        ],
        "media_paths": [
            "/sdcard/WhatsApp Business",
            "/sdcard/Android/media/com.whatsapp.w4b",
            "/storage/emulated/0/WhatsApp Business",
            "/storage/emulated/0/Android/media/com.whatsapp.w4b",
        ],
        "data_paths": [
            "/data/data/com.whatsapp.w4b",
        ],
        "description": "Mensagens, mídias, backups locais do WhatsApp Business",
    },
    "telegram": {
        "name": "Telegram",
        "icon": "✈️",
        "packages": [
            "org.telegram.messenger",
            "org.telegram.messenger.web",
            "org.thunderdog.challegram",  # Telegram X
        ],
        "media_paths": [
            "/sdcard/Telegram",
            "/storage/emulated/0/Telegram",
            "/sdcard/Android/media/org.telegram.messenger",
        ],
        "data_paths": [
            "/data/data/org.telegram.messenger",
        ],
        "description": "Mídias e cache do Telegram",
    },
    "signal": {
        "name": "Signal",
        "icon": "🔒",
        "packages": [
            "org.thoughtcrime.securesms",
        ],
        "media_paths": [
            "/sdcard/Signal",
            "/storage/emulated/0/Signal",
            "/sdcard/Android/media/org.thoughtcrime.securesms",
        ],
        "data_paths": [
            "/data/data/org.thoughtcrime.securesms",
        ],
        "description": "Backups criptografados do Signal",
    },
    "instagram": {
        "name": "Instagram",
        "icon": "📸",
        "packages": [
            "com.instagram.android",
        ],
        "media_paths": [
            "/sdcard/Instagram",
            "/storage/emulated/0/Instagram",
            "/sdcard/Pictures/Instagram",
            "/sdcard/Android/media/com.instagram.android",
        ],
        "data_paths": [
            "/data/data/com.instagram.android",
        ],
        "description": "Fotos e vídeos baixados do Instagram",
    },
    "facebook_messenger": {
        "name": "Messenger",
        "icon": "💭",
        "packages": [
            "com.facebook.orca",
            "com.facebook.mlite",  # Messenger Lite
        ],
        "media_paths": [
            "/sdcard/Messenger",
            "/sdcard/Pictures/Messenger",
            "/sdcard/Android/media/com.facebook.orca",
        ],
        "data_paths": [
            "/data/data/com.facebook.orca",
        ],
        "description": "Fotos, vídeos e áudios do Messenger",
    },
    "discord": {
        "name": "Discord",
        "icon": "🎮",
        "packages": [
            "com.discord",
        ],
        "media_paths": [
            "/sdcard/Discord",
            "/sdcard/Pictures/Discord",
            "/sdcard/Android/media/com.discord",
        ],
        "data_paths": [
            "/data/data/com.discord",
        ],
        "description": "Cache e downloads do Discord",
    },
    "viber": {
        "name": "Viber",
        "icon": "📞",
        "packages": [
            "com.viber.voip",
        ],
        "media_paths": [
            "/sdcard/Viber",
            "/sdcard/Android/media/com.viber.voip",
            "/storage/emulated/0/Viber",
        ],
        "data_paths": [
            "/data/data/com.viber.voip",
        ],
        "description": "Mensagens e mídias do Viber",
    },
    "wechat": {
        "name": "WeChat",
        "icon": "🟢",
        "packages": [
            "com.tencent.mm",
        ],
        "media_paths": [
            "/sdcard/tencent/MicroMsg",
            "/sdcard/Android/media/com.tencent.mm",
            "/storage/emulated/0/tencent/MicroMsg",
        ],
        "data_paths": [
            "/data/data/com.tencent.mm",
        ],
        "description": "Mensagens e mídias do WeChat",
    },
    "line": {
        "name": "LINE",
        "icon": "🟩",
        "packages": [
            "jp.naver.line.android",
        ],
        "media_paths": [
            "/sdcard/LINE",
            "/sdcard/Android/media/jp.naver.line.android",
            "/sdcard/Pictures/LINE",
        ],
        "data_paths": [
            "/data/data/jp.naver.line.android",
        ],
        "description": "Mensagens e mídias do LINE",
    },
    "tiktok": {
        "name": "TikTok",
        "icon": "🎵",
        "packages": [
            "com.zhiliaoapp.musically",
            "com.ss.android.ugc.trill",
        ],
        "media_paths": [
            "/sdcard/TikTok",
            "/sdcard/Pictures/TikTok",
            "/sdcard/Android/media/com.zhiliaoapp.musically",
            "/sdcard/Movies/TikTok",
        ],
        "data_paths": [
            "/data/data/com.zhiliaoapp.musically",
        ],
        "description": "Vídeos salvos do TikTok",
    },
    "twitter_x": {
        "name": "X (Twitter)",
        "icon": "🐦",
        "packages": [
            "com.twitter.android",
        ],
        "media_paths": [
            "/sdcard/Twitter",
            "/sdcard/Pictures/Twitter",
            "/sdcard/Android/media/com.twitter.android",
        ],
        "data_paths": [
            "/data/data/com.twitter.android",
        ],
        "description": "Mídias salvas do X/Twitter",
    },
}


# ---------------------------------------------------------------------------
# File entry for tree view
# ---------------------------------------------------------------------------
@dataclass
class FileEntry:
    """Represents a file or directory on the device."""
    name: str
    path: str  # full remote path
    is_dir: bool = False
    size: int = 0
    permissions: str = ""
    selected: bool = False
    children_loaded: bool = False
    children: List["FileEntry"] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DeviceTreeBrowser — reusable CTk widget
# ---------------------------------------------------------------------------
class DeviceTreeBrowser(ctk.CTkFrame):
    """A scrollable tree-view file browser for an Android device via ADB.

    Features:
      - Lazy loading (children fetched on expand)
      - Checkbox selection (with select-all / deselect-all)
      - Quick navigate buttons for common directories
      - Reports selected paths back to caller
    """

    INDENT_PX = 20

    def __init__(
        self,
        master,
        adb_core,
        serial: Optional[str] = None,
        root_path: str = "/sdcard",
        show_quick_nav: bool = True,
        height: int = 300,
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self._adb = adb_core
        self._serial = serial
        self._root_path = root_path
        self._entries: Dict[str, FileEntry] = {}
        self._check_vars: Dict[str, ctk.BooleanVar] = {}
        self._loading = False

        # --- Quick-nav bar ---
        if show_quick_nav:
            nav = ctk.CTkFrame(self, fg_color="transparent")
            nav.pack(fill="x", padx=4, pady=(4, 0))

            ctk.CTkButton(
                nav, text="📱 /sdcard", width=80, height=28,
                command=lambda: self.navigate("/sdcard"),
            ).pack(side="left", padx=2)
            ctk.CTkButton(
                nav, text="📷 DCIM", width=68, height=28,
                command=lambda: self.navigate("/sdcard/DCIM"),
            ).pack(side="left", padx=2)
            ctk.CTkButton(
                nav, text="📥 Download", width=80, height=28,
                command=lambda: self.navigate("/sdcard/Download"),
            ).pack(side="left", padx=2)
            ctk.CTkButton(
                nav, text="📦 Android", width=76, height=28,
                command=lambda: self.navigate("/sdcard/Android"),
            ).pack(side="left", padx=2)
            ctk.CTkButton(
                nav, text="💬 Apps", width=60, height=28,
                command=lambda: self.navigate("/sdcard/Android/media"),
            ).pack(side="left", padx=2)
            ctk.CTkButton(
                nav, text="/", width=30, height=28,
                command=lambda: self.navigate("/"),
            ).pack(side="left", padx=2)

            ctk.CTkButton(
                nav, text="✅ Todos", width=60, height=28,
                fg_color="#06d6a0", hover_color="#05c090",
                command=self.select_all_visible,
            ).pack(side="right", padx=2)
            ctk.CTkButton(
                nav, text="❌ Nenhum", width=68, height=28,
                fg_color="#ef476f", hover_color="#d63a5e",
                command=self.deselect_all,
            ).pack(side="right", padx=2)

        # --- Path entry ---
        path_frame = ctk.CTkFrame(self, fg_color="transparent")
        path_frame.pack(fill="x", padx=4, pady=2)

        ctk.CTkLabel(path_frame, text="📂", width=24).pack(side="left")
        self._path_entry = ctk.CTkEntry(path_frame)
        self._path_entry.pack(side="left", fill="x", expand=True, padx=4)
        self._path_entry.insert(0, root_path)
        self._path_entry.bind("<Return>", lambda _: self.navigate(self._path_entry.get()))

        ctk.CTkButton(
            path_frame, text="Ir", width=40, height=28,
            command=lambda: self.navigate(self._path_entry.get()),
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            path_frame, text="🔄", width=30, height=28,
            command=self.refresh,
        ).pack(side="left", padx=2)

        # --- Loading label ---
        self._lbl_loading = ctk.CTkLabel(
            self, text="", text_color="#8d99ae",
            font=ctk.CTkFont(size=11),
        )
        self._lbl_loading.pack(anchor="w", padx=8)

        # --- Scrollable content ---
        self._scroll = ctk.CTkScrollableFrame(self, height=height)
        self._scroll.pack(fill="both", expand=True, padx=4, pady=(0, 4))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_serial(self, serial: Optional[str]):
        """Set (or change) the target device serial and refresh."""
        changed = (serial != self._serial)
        self._serial = serial
        if changed and serial:
            self.refresh()

    def navigate(self, path: str):
        """Navigate to a path and list its contents."""
        path = path.strip()
        if not path:
            path = "/sdcard"
        self._root_path = path
        self._path_entry.delete(0, "end")
        self._path_entry.insert(0, path)
        self.refresh()

    def refresh(self):
        """Reload current directory from device."""
        if self._loading or not self._serial:
            return
        self._loading = True
        self._lbl_loading.configure(text="Carregando...")
        threading.Thread(target=self._load_dir, args=(self._root_path,), daemon=True).start()

    def get_selected_paths(self) -> List[str]:
        """Return list of selected remote paths."""
        selected = []
        for path, var in self._check_vars.items():
            if var.get():
                selected.append(path)
        return selected

    def select_all_visible(self):
        """Select all visible checkboxes."""
        for var in self._check_vars.values():
            var.set(True)

    def deselect_all(self):
        """Deselect all checkboxes."""
        for var in self._check_vars.values():
            var.set(False)

    def select_paths(self, paths: List[str]):
        """Programmatically select specific paths."""
        for path in paths:
            if path in self._check_vars:
                self._check_vars[path].set(True)

    # ------------------------------------------------------------------
    # Internal — loading
    # ------------------------------------------------------------------
    def _load_dir(self, remote_path: str):
        """Load directory listing from device (runs in background thread)."""
        try:
            entries = self._list_remote_dir(remote_path)
            # Schedule UI update on main thread
            self.after(0, lambda: self._render_entries(remote_path, entries))
        except Exception as exc:
            log.warning("Failed to list %s: %s", remote_path, exc)
            self.after(0, lambda: self._render_error(str(exc)))
        finally:
            self._loading = False

    def _list_remote_dir(self, remote_path: str) -> List[FileEntry]:
        """Use `adb shell ls -la` to list a remote directory."""
        cmd = f'ls -la "{remote_path}" 2>/dev/null'
        out = self._adb.run_shell(cmd, self._serial, timeout=15)
        entries: List[FileEntry] = []

        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("total"):
                continue
            entry = self._parse_ls_line(line, remote_path)
            if entry and entry.name not in (".", ".."):
                entries.append(entry)

        # Sort: dirs first, then files, alphabetical
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def _parse_ls_line(self, line: str, parent: str) -> Optional[FileEntry]:
        """Parse an `ls -la` output line into a FileEntry."""
        # Expected format: drwxrwx--x  4 root sdcard_rw  4096 2024-01-15 10:30 dirname
        # or:              -rw-rw----  1 root sdcard_rw 12345 2024-01-15 10:30 filename
        parts = line.split(None, 7)
        if len(parts) < 8:
            # Might be a simpler format; try with fewer columns
            parts = line.split(None, 6)
            if len(parts) < 7:
                return None

        perms = parts[0]
        is_dir = perms.startswith("d") or perms.startswith("l")
        # Name is the last field — may contain spaces
        name = parts[-1].strip()

        # Handle symlinks: "name -> target"
        if " -> " in name:
            name = name.split(" -> ")[0].strip()

        # Try to grab size
        size = 0
        try:
            # Size is typically parts[4] in ls -la
            size = int(parts[4])
        except (ValueError, IndexError):
            pass

        path = f"{parent.rstrip('/')}/{name}"

        return FileEntry(
            name=name,
            path=path,
            is_dir=is_dir,
            size=size,
            permissions=perms,
        )

    # ------------------------------------------------------------------
    # Internal — rendering
    # ------------------------------------------------------------------
    def _render_entries(self, parent_path: str, entries: List[FileEntry]):
        """Render directory entries in the scrollable frame."""
        self._lbl_loading.configure(text=f"{len(entries)} itens em {parent_path}")

        # Clear previous
        for w in self._scroll.winfo_children():
            w.destroy()
        self._check_vars.clear()
        self._entries.clear()

        if not entries:
            ctk.CTkLabel(
                self._scroll, text="(diretório vazio)",
                text_color="#8d99ae",
            ).pack(pady=10)
            return

        # Parent nav (..)
        if parent_path != "/":
            parent = str(PurePosixPath(parent_path).parent)
            row = ctk.CTkFrame(self._scroll, fg_color="transparent", height=28)
            row.pack(fill="x", padx=2, pady=1)
            row.pack_propagate(False)
            ctk.CTkButton(
                row, text="⬆️ ..", width=60, height=24,
                fg_color="transparent", hover_color="#2b2d42",
                anchor="w",
                command=lambda p=parent: self.navigate(p),
            ).pack(side="left", padx=4)

        for entry in entries:
            self._render_entry_row(entry)

    def _render_entry_row(self, entry: FileEntry):
        """Render a single file/dir row with checkbox."""
        row = ctk.CTkFrame(self._scroll, fg_color="transparent", height=28)
        row.pack(fill="x", padx=2, pady=1)
        row.pack_propagate(False)

        # Checkbox
        var = ctk.BooleanVar(value=False)
        self._check_vars[entry.path] = var
        self._entries[entry.path] = entry

        cb = ctk.CTkCheckBox(
            row, text="", variable=var,
            width=24, height=20,
            checkbox_width=18, checkbox_height=18,
        )
        cb.pack(side="left", padx=(4, 0))

        # Icon + name (clickable for dirs)
        if entry.is_dir:
            icon = "📁"
            btn = ctk.CTkButton(
                row,
                text=f"{icon} {entry.name}/",
                fg_color="transparent",
                hover_color="#2b2d42",
                anchor="w",
                height=24,
                font=ctk.CTkFont(size=12, weight="bold"),
                command=lambda p=entry.path: self.navigate(p),
            )
            btn.pack(side="left", padx=4, fill="x", expand=True)
        else:
            icon = self._file_icon(entry.name)
            lbl = ctk.CTkLabel(
                row,
                text=f"{icon} {entry.name}",
                anchor="w",
                font=ctk.CTkFont(size=12),
            )
            lbl.pack(side="left", padx=8, fill="x", expand=True)

        # Size label for files
        if not entry.is_dir and entry.size > 0:
            size_str = self._format_size(entry.size)
            ctk.CTkLabel(
                row, text=size_str,
                text_color="#8d99ae",
                font=ctk.CTkFont(size=11),
                width=70,
            ).pack(side="right", padx=8)

    def _render_error(self, msg: str):
        """Show an error message in the tree area."""
        self._lbl_loading.configure(text="Erro ao carregar")
        for w in self._scroll.winfo_children():
            w.destroy()
        ctk.CTkLabel(
            self._scroll,
            text=f"⚠️ {msg}",
            text_color="#ef476f",
        ).pack(pady=10)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _file_icon(name: str) -> str:
        ext = PurePosixPath(name).suffix.lower()
        icon_map = {
            ".jpg": "🖼️", ".jpeg": "🖼️", ".png": "🖼️", ".gif": "🖼️",
            ".webp": "🖼️", ".bmp": "🖼️", ".svg": "🖼️",
            ".mp4": "🎬", ".mkv": "🎬", ".avi": "🎬", ".mov": "🎬",
            ".3gp": "🎬", ".webm": "🎬",
            ".mp3": "🎵", ".flac": "🎵", ".wav": "🎵", ".aac": "🎵",
            ".ogg": "🎵", ".m4a": "🎵", ".opus": "🎵",
            ".pdf": "📕", ".doc": "📝", ".docx": "📝", ".txt": "📝",
            ".xls": "📊", ".xlsx": "📊", ".csv": "📊",
            ".ppt": "📊", ".pptx": "📊",
            ".zip": "📦", ".tar": "📦", ".gz": "📦", ".7z": "📦",
            ".rar": "📦",
            ".apk": "📲", ".aab": "📲",
            ".db": "🗄️", ".sqlite": "🗄️",
            ".json": "📋", ".xml": "📋", ".html": "🌐",
            ".vcf": "👤",
        }
        return icon_map.get(ext, "📄")

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        else:
            return f"{size / (1024 * 1024 * 1024):.2f} GB"


# ---------------------------------------------------------------------------
# MessagingAppDetector — detect installed messaging apps on device
# ---------------------------------------------------------------------------
class MessagingAppDetector:
    """Detects installed messaging apps and their data paths on a device."""

    def __init__(self, adb_core):
        self._adb = adb_core

    def detect_installed_apps(
        self, serial: str
    ) -> Dict[str, Dict]:
        """Return dict of messaging apps that are installed on the device.

        Returns: { app_key: { ...MESSAGING_APPS[key], 'existing_paths': [...] } }
        """
        installed_packages = set(self._adb.list_packages(serial, third_party=True))
        results: Dict[str, Dict] = {}

        for app_key, app_info in MESSAGING_APPS.items():
            # Check if any of the app's packages are installed
            app_packages = set(app_info["packages"])
            if app_packages & installed_packages:
                # Check which media paths actually exist on the device
                existing = self._find_existing_paths(serial, app_info["media_paths"])
                results[app_key] = {
                    **app_info,
                    "existing_paths": existing,
                    "installed_packages": list(app_packages & installed_packages),
                }

        return results

    def _find_existing_paths(
        self, serial: str, paths: List[str]
    ) -> List[str]:
        """Return only those remote paths that actually exist."""
        existing: List[str] = []
        # Batch check using a single shell command for efficiency
        checks = " || ".join(
            f'(test -d "{p}" && echo "EXISTS:{p}")'
            for p in paths
        )
        try:
            out = self._adb.run_shell(checks, serial, timeout=10)
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("EXISTS:"):
                    existing.append(line[7:])
        except Exception as exc:
            log.debug("Path check error: %s", exc)
            # Fallback: check one-by-one
            for p in paths:
                try:
                    r = self._adb.run_shell(f'test -d "{p}" && echo yes', serial, timeout=5)
                    if "yes" in r:
                        existing.append(p)
                except Exception:
                    pass
        return existing

    def get_app_backup_size(
        self, serial: str, app_key: str, paths: List[str]
    ) -> int:
        """Estimate total size of an app's media data."""
        total = 0
        for p in paths:
            try:
                out = self._adb.run_shell(
                    f'du -s "{p}" 2>/dev/null | cut -f1', serial, timeout=15
                )
                val = out.strip()
                if val.isdigit():
                    total += int(val) * 1024  # du returns KB
            except Exception:
                pass
        return total


# ---------------------------------------------------------------------------
# Cloud-synced / safe-to-skip packages — apps whose data is fully backed up
# online and does NOT need local backup.  Everything NOT in this set that has
# a non-trivial data footprint is a candidate for local backup.
# ---------------------------------------------------------------------------
CLOUD_SYNCED_PACKAGES: Set[str] = {
    # Google suite
    "com.google.android.gms",
    "com.google.android.gsf",
    "com.google.android.apps.gmail",
    "com.google.android.apps.maps",
    "com.google.android.apps.photos",
    "com.google.android.apps.docs",
    "com.google.android.apps.drive",
    "com.google.android.apps.calendar",
    "com.google.android.contacts",
    "com.google.android.apps.youtube",
    "com.google.android.apps.youtube.music",
    "com.google.android.apps.tachyon",  # Google Duo / Meet
    "com.google.android.apps.google.assistant",
    "com.google.android.googlequicksearchbox",
    "com.google.android.keep",
    "com.google.android.apps.translate",
    "com.google.android.apps.fitness",
    # System / carrier
    "com.android.vending",  # Play Store
    "com.android.chrome",
    "com.android.providers.downloads",
    "com.android.providers.contacts",
    "com.android.phone",
    "com.android.settings",
    "com.android.systemui",
    "com.android.launcher",
    # Samsung
    "com.samsung.android.app.smartcapture",
    "com.samsung.android.calendar",
    "com.samsung.android.contacts",
    "com.samsung.android.email.provider",
    "com.sec.android.app.launcher",
    # Streaming (no user-created data)
    "com.netflix.mediaclient",
    "com.spotify.music",
    "com.amazon.avod",
    "com.disney.disneyplus",
    "com.hbo.hbonow",
    "tv.twitch.android.app",
    "com.amazon.mp3",
    # Social (cloud-synced inherently)
    "com.facebook.katana",
    "com.linkedin.android",
    "com.pinterest",
    "com.snapchat.android",
    "com.reddit.frontpage",
    # Productivity (cloud-synced)
    "com.microsoft.office.outlook",
    "com.microsoft.teams",
    "com.microsoft.office.word",
    "com.microsoft.office.excel",
    "com.microsoft.office.powerpoint",
    "com.microsoft.skydrive",  # OneDrive
    "com.dropbox.android",
    "com.apple.android.music",
    # Delivery / ride
    "com.ubercab",
    "com.ubercab.eats",
    "com.mcdonalds.app",
    "com.contextlogic.wish",
    "com.shopify.mobile",
}

# Categories of apps that commonly have LOCAL-ONLY user data worth backing up
UNSYNCED_APP_CATEGORIES: Dict[str, Dict] = {
    "authenticator": {
        "name": "Autenticadores",
        "icon": "🔑",
        "description": "Tokens 2FA que serão perdidos se não forem salvos",
        "risk": "critical",
        "known_packages": {
            "com.google.android.apps.authenticator2": "Google Authenticator",
            "com.authy.authy": "Authy",
            "org.fedorahosted.freeotp": "FreeOTP",
            "com.azure.authenticator": "Microsoft Authenticator",
            "com.yubico.yubioath": "Yubico Authenticator",
            "org.shadowice.flocke.andotp": "andOTP",
            "com.beemdevelopment.aegis": "Aegis Authenticator",
            "me.jmh.authenticatorpro": "Authenticator Pro",
        },
    },
    "password_manager": {
        "name": "Gerenciadores de Senha",
        "icon": "🔐",
        "description": "Cofres de senha com dados locais",
        "risk": "critical",
        "known_packages": {
            "com.x8bit.bitwarden": "Bitwarden",
            "keepass2android.keepass2android": "KeePass2Android",
            "com.kunzisoft.keepass.free": "KeePassDX",
            "com.lastpass.lpandroid": "LastPass",
            "com.onepassword.android": "1Password",
            "com.dashlane": "Dashlane",
            "org.nicholasly.enpass": "Enpass",
        },
    },
    "notes": {
        "name": "Notas e Anotações",
        "icon": "📝",
        "description": "Notas que podem ter dados apenas locais",
        "risk": "high",
        "known_packages": {
            "com.simplemobiletools.notes.pro": "Simple Notes",
            "org.tasks.android": "Tasks.org",
            "com.orgzly": "Orgzly",
            "com.automattic.simplenote": "Simplenote",
            "com.samsung.android.app.notes": "Samsung Notes",
            "com.evernote": "Evernote",
            "com.ideashower.readitlater.pro": "Pocket",
            "md.obsidian": "Obsidian",
            "net.gsantner.markor": "Markor",
            "com.standardnotes": "Standard Notes",
            "org.joplinapp.mobile": "Joplin",
        },
    },
    "game": {
        "name": "Jogos (saves locais)",
        "icon": "🎮",
        "description": "Dados de progresso que podem ser locais",
        "risk": "medium",
        "known_packages": {
            "com.supercell.clashofclans": "Clash of Clans",
            "com.supercell.clashroyale": "Clash Royale",
            "com.king.candycrushsaga": "Candy Crush",
            "com.mojang.minecraftpe": "Minecraft PE",
            "com.roblox.client": "Roblox",
            "com.activision.callofduty.shooter": "Call of Duty Mobile",
            "com.garena.game.codm": "COD Mobile (Garena)",
            "com.tencent.ig": "PUBG Mobile",
            "com.dts.freefireth": "Free Fire",
            "com.miHoYo.GenshinImpact": "Genshin Impact",
            "com.innersloth.spacemafia": "Among Us",
        },
    },
    "health_fitness": {
        "name": "Saúde e Fitness",
        "icon": "❤️",
        "description": "Dados de saúde e exercícios locais",
        "risk": "high",
        "known_packages": {
            "com.sec.android.app.shealth": "Samsung Health",
            "com.strava": "Strava",
            "cc.runtastic.android": "Adidas Running",
            "com.myfitnesspal.android": "MyFitnessPal",
            "com.nike.plusgps": "Nike Run Club",
            "com.garmin.android.apps.connectmobile": "Garmin Connect",
            "com.fitbit.FitbitMobile": "Fitbit",
        },
    },
    "finance": {
        "name": "Finanças e Bancos",
        "icon": "🏦",
        "description": "Apps financeiros com dados de transação locais",
        "risk": "medium",
        "known_packages": {
            "com.nu.production": "Nubank",
            "com.btgpactual.pangea": "BTG Pactual",
            "br.com.intermedium": "Inter",
            "com.picpay": "PicPay",
            "br.com.itau": "Itaú",
            "com.bradesco": "Bradesco",
            "com.santander.app": "Santander",
            "br.com.bb.android": "Banco do Brasil",
            "br.gov.caixa.tem": "Caixa Tem",
            "com.mercadopago.wallet": "Mercado Pago",
            "com.paypal.android.p2pmobile": "PayPal",
        },
    },
    "voice_recorder": {
        "name": "Gravações de Voz",
        "icon": "🎙️",
        "description": "Gravações de áudio locais",
        "risk": "high",
        "known_packages": {
            "com.sec.android.app.voicenote": "Samsung Voice",
            "com.google.android.apps.recorder": "Google Recorder",
            "com.media.bestrecorder.audiorecorder": "Voice Recorder",
            "com.coffeebeanventures.easyvoicerecorder": "Easy Voice Recorder",
        },
    },
    "camera_gallery": {
        "name": "Câmera e Galeria",
        "icon": "📸",
        "description": "Fotos/vídeos em galerias alternativas",
        "risk": "high",
        "known_packages": {
            "com.sec.android.gallery3d": "Samsung Gallery",
            "com.google.android.apps.camera": "Google Camera",
            "org.codeaurora.snapcam": "Snap Camera",
            "com.simplemobiletools.gallery.pro": "Simple Gallery",
        },
    },
    "document_scanner": {
        "name": "Scanners de Documentos",
        "icon": "📄",
        "description": "Documentos digitalizados localmente",
        "risk": "high",
        "known_packages": {
            "com.microsoft.office.officelens": "Microsoft Lens",
            "net.doo.snap": "Adobe Scan",
            "com.intsig.camscanner": "CamScanner",
            "com.cv.docscanner": "Document Scanner",
        },
    },
    "ebook_reader": {
        "name": "Leitores de E-books",
        "icon": "📚",
        "description": "E-books baixados e anotações",
        "risk": "medium",
        "known_packages": {
            "com.amazon.kindle": "Kindle",
            "com.kobobooks.android": "Kobo",
            "org.coolreader": "Cool Reader",
            "com.fbreader.fbreader.premium": "FBReader",
            "org.readera": "ReadEra",
            "com.aldiko.android": "Aldiko",
        },
    },
    "vpn_network": {
        "name": "VPN e Rede",
        "icon": "🛡️",
        "description": "Perfis VPN e configurações de rede",
        "risk": "medium",
        "known_packages": {
            "com.wireguard.android": "WireGuard",
            "de.blinkt.openvpn": "OpenVPN for Android",
            "net.openvpn.openvpn": "OpenVPN Connect",
            "org.torproject.torbrowser": "Tor Browser",
            "ch.protonvpn.android": "Proton VPN",
            "com.nordvpn.android": "NordVPN",
        },
    },
    "file_manager": {
        "name": "Gerenciadores de Arquivos",
        "icon": "📂",
        "description": "Favoritos e arquivos gerenciados",
        "risk": "low",
        "known_packages": {
            "com.ghisler.android.TotalCommander": "Total Commander",
            "com.mi.android.globalFileexplorer": "Mi File Manager",
            "com.sec.android.app.myfiles": "Samsung My Files",
            "com.lonelycatgames.Xplore": "X-plore",
            "com.mixplorer.silver": "MiXplorer",
        },
    },
}

# All known packages from MESSAGING_APPS + UNSYNCED_APP_CATEGORIES —
# used so the detector doesn't double-report them.
_ALL_KNOWN_PACKAGES: Set[str] = set()
for _app_info in MESSAGING_APPS.values():
    _ALL_KNOWN_PACKAGES.update(_app_info["packages"])
for _cat in UNSYNCED_APP_CATEGORIES.values():
    _ALL_KNOWN_PACKAGES.update(_cat["known_packages"].keys())


# ---------------------------------------------------------------------------
# UnsyncedAppDetector — scans for apps with local-only data
# ---------------------------------------------------------------------------
@dataclass
class DetectedApp:
    """An app discovered on the device that may have unsynced local data."""
    package: str
    app_name: str
    category: str           # key from UNSYNCED_APP_CATEGORIES or "unknown"
    category_name: str
    icon: str
    risk: str               # critical / high / medium / low / unknown
    apk_path: Optional[str] = None
    data_size_kb: int = 0
    version: str = ""
    description: str = ""


class UnsyncedAppDetector:
    """Scans a device for third-party apps whose data may NOT be
    cloud-synced and could be lost without a local backup.

    Two-pass approach:
      1. Check for known high-value apps (authenticators, password managers …)
      2. Scan ALL installed 3rd-party packages, subtract
         cloud-synced / system packages, and report the rest as candidates.
    """

    def __init__(self, adb_core):
        self._adb = adb_core

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def detect(
        self,
        serial: str,
        include_unknown: bool = True,
        min_data_size_kb: int = 256,
    ) -> List[DetectedApp]:
        """Return a list of apps with potentially unsynced local data.

        Args:
            serial:           Device serial.
            include_unknown:  If True, also report unknown 3rd-party apps whose
                              data folder is larger than *min_data_size_kb*.
            min_data_size_kb: Minimum /data/data/<pkg> size to consider unknown
                              apps worth reporting (avoids noise).
        """
        all_packages = set(self._adb.list_packages(serial, third_party=True))
        results: List[DetectedApp] = []

        # --- Pass 1: known high-value categories ---
        for cat_key, cat_info in UNSYNCED_APP_CATEGORIES.items():
            for pkg, friendly_name in cat_info["known_packages"].items():
                if pkg in all_packages:
                    det = DetectedApp(
                        package=pkg,
                        app_name=friendly_name,
                        category=cat_key,
                        category_name=cat_info["name"],
                        icon=cat_info["icon"],
                        risk=cat_info["risk"],
                        description=cat_info["description"],
                    )
                    # Optionally grab version
                    det.version = self._get_version(serial, pkg)
                    results.append(det)

        # --- Pass 2: unknown 3rd-party apps ---
        if include_unknown:
            already_known = (
                CLOUD_SYNCED_PACKAGES | _ALL_KNOWN_PACKAGES |
                {r.package for r in results}
            )
            unknown_pkgs = all_packages - already_known
            # Filter out common prefixes that are safe to skip
            safe_prefixes = (
                "com.android.", "com.google.", "com.samsung.", "com.sec.",
                "com.qualcomm.", "com.mediatek.", "com.miui.",
                "android.", "com.huawei.",
            )
            unknown_pkgs = {
                p for p in unknown_pkgs
                if not any(p.startswith(pf) for pf in safe_prefixes)
            }

            if unknown_pkgs:
                sizes = self._batch_data_sizes(serial, unknown_pkgs)
                for pkg in sorted(unknown_pkgs):
                    size_kb = sizes.get(pkg, 0)
                    if size_kb >= min_data_size_kb:
                        friendly = self._get_app_label(serial, pkg) or pkg.split(".")[-1].title()
                        results.append(DetectedApp(
                            package=pkg,
                            app_name=friendly,
                            category="unknown",
                            category_name="Outros Apps",
                            icon="📦",
                            risk="unknown",
                            data_size_kb=size_kb,
                            version=self._get_version(serial, pkg),
                            description=f"App com ~{self._fmt_size(size_kb)} de dados locais",
                        ))

        # Sort: critical → high → medium → low → unknown, then alphabetical
        risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}
        results.sort(key=lambda d: (risk_order.get(d.risk, 5), d.app_name.lower()))

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_version(self, serial: str, pkg: str) -> str:
        try:
            out = self._adb.run_shell(
                f'dumpsys package {pkg} | grep versionName | head -1',
                serial, timeout=5,
            )
            for line in out.splitlines():
                if "versionName=" in line:
                    return line.split("versionName=", 1)[1].strip()
        except Exception:
            pass
        return ""

    def _get_app_label(self, serial: str, pkg: str) -> Optional[str]:
        """Try to get the user-visible app name from the package."""
        try:
            out = self._adb.run_shell(
                f'dumpsys package {pkg} | grep -A1 "application-label:" | head -1',
                serial, timeout=5,
            )
            # Older: "application-label:'My App'"
            if "application-label:" in out:
                label = out.split("application-label:", 1)[1].strip().strip("'\"")
                if label:
                    return label
        except Exception:
            pass
        # Fallback: use aapt on device (if available)
        try:
            apk_path = self._adb.get_apk_path(pkg, serial)
            if apk_path:
                out = self._adb.run_shell(
                    f'dumpsys package {pkg} | grep "applicationInfo" | head -1',
                    serial, timeout=5,
                )
                # Try to extract label from applicationInfo
                if "labelRes=" in out:
                    pass  # Not easily extractable
        except Exception:
            pass
        return None

    def _batch_data_sizes(
        self, serial: str, packages: Set[str]
    ) -> Dict[str, int]:
        """Get data directory sizes for multiple packages in one shot."""
        sizes: Dict[str, int] = {}
        # Build a shell script that checks sizes in bulk
        pkg_list = sorted(packages)
        # Process in chunks to avoid command-line length limits
        chunk_size = 50
        for i in range(0, len(pkg_list), chunk_size):
            chunk = pkg_list[i:i + chunk_size]
            cmds = []
            for pkg in chunk:
                cmds.append(
                    f'du -s /data/data/{pkg} 2>/dev/null || '
                    f'du -s /data/user/0/{pkg} 2>/dev/null || '
                    f'echo "0\t/data/data/{pkg}"'
                )
            script = " ; ".join(cmds)
            try:
                out = self._adb.run_shell(script, serial, timeout=30)
                for line in out.splitlines():
                    line = line.strip()
                    if "\t" in line:
                        parts = line.split("\t", 1)
                        try:
                            sz = int(parts[0])
                        except ValueError:
                            sz = 0
                        path = parts[1]
                        # Extract package name from path
                        for pkg in chunk:
                            if pkg in path:
                                sizes[pkg] = sz
                                break
            except Exception as exc:
                log.debug("batch_data_sizes error: %s", exc)
                # On failure (non-root), try accessible media dirs instead
                for pkg in chunk:
                    for base in ("/sdcard/Android/data", "/sdcard/Android/media"):
                        try:
                            out2 = self._adb.run_shell(
                                f'du -s "{base}/{pkg}" 2>/dev/null | cut -f1',
                                serial, timeout=5,
                            )
                            val = out2.strip()
                            if val.isdigit() and int(val) > 0:
                                sizes[pkg] = sizes.get(pkg, 0) + int(val)
                        except Exception:
                            pass
        return sizes

    @staticmethod
    def _fmt_size(kb: int) -> str:
        if kb < 1024:
            return f"{kb} KB"
        elif kb < 1024 * 1024:
            return f"{kb / 1024:.1f} MB"
        else:
            return f"{kb / (1024 * 1024):.1f} GB"


# ---------------------------------------------------------------------------
# AndroidPathResolver — finds actual paths across OEM variations
# ---------------------------------------------------------------------------
class AndroidPathResolver:
    """Resolves actual paths on a device, handling OEM variations."""

    def __init__(self, adb_core):
        self._adb = adb_core
        self._cache: Dict[str, Dict[str, str]] = {}  # serial -> {key: resolved_path}

    def resolve(self, serial: str, key: str) -> Optional[str]:
        """Resolve a known directory key to its actual path on this device.

        Keys: internal_storage, dcim, pictures, downloads, documents, movies,
              music, external_sd
        """
        if serial in self._cache and key in self._cache[serial]:
            return self._cache[serial][key]

        candidates = ANDROID_KNOWN_DIRS.get(key, [])
        for path in candidates:
            try:
                r = self._adb.run_shell(
                    f'test -d "{path}" && echo yes', serial, timeout=5
                )
                if "yes" in r:
                    self._cache.setdefault(serial, {})[key] = path
                    return path
            except Exception:
                continue
        return None

    def resolve_all(self, serial: str) -> Dict[str, str]:
        """Resolve all known directory keys for a device."""
        results: Dict[str, str] = {}
        # Build a single shell command that checks all candidates
        all_checks = []
        key_map = []
        for key, candidates in ANDROID_KNOWN_DIRS.items():
            for path in candidates:
                all_checks.append(f'test -d "{path}" && echo "FOUND:{key}:{path}"')
                key_map.append((key, path))

        cmd = " ; ".join(all_checks)
        try:
            out = self._adb.run_shell(cmd, serial, timeout=20)
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("FOUND:"):
                    _, k, p = line.split(":", 2)
                    if k not in results:  # first match wins
                        results[k] = p
        except Exception as exc:
            log.debug("resolve_all error: %s — falling back to individual checks", exc)
            for key in ANDROID_KNOWN_DIRS:
                r = self.resolve(serial, key)
                if r:
                    results[key] = r

        self._cache[serial] = results
        return results
