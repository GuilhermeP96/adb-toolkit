"""
gui.py - Main graphical user interface for ADB Toolkit.

Built with customtkinter for a modern dark-mode UI.
Tabs: Devices | Backup | Restore | Transfer | Drivers | Settings
"""

import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import customtkinter as ctk
from tkinter import filedialog, messagebox

from .adb_core import ADBCore, DeviceInfo
from .backup_manager import BackupManager, BackupProgress, BACKUP_TYPES, MEDIA_PATHS
from .restore_manager import RestoreManager
from .transfer_manager import TransferManager, TransferConfig, TransferProgress
from .cleanup_manager import (
    CleanupManager, CleanupMode, ModeEstimate, ModeProgress, ModeResult,
    MODE_LABELS, MODE_DESCRIPTIONS, MODE_ORDER,
)
from .toolbox_manager import ToolboxManager, ToolboxProgress
from .driver_manager import DriverManager, DriverStatus
from .device_explorer import (
    DeviceTreeBrowser, MessagingAppDetector, AndroidPathResolver, MESSAGING_APPS,
    UnsyncedAppDetector, DetectedApp, UNSYNCED_APP_CATEGORIES,
)
from .config import Config
from .utils import format_bytes, format_duration, open_folder, is_adb_in_path, add_adb_to_path, remove_adb_from_path, get_adb_dir, is_admin
from .accelerator import (
    TransferAccelerator,
    detect_all_gpus,
    detect_all_npus,
    detect_virtualization,
    EnergyProfile,
    TaskPriority,
)
from .device_interface import DeviceManager, DevicePlatform, UnifiedDeviceInfo
from .adb_adapter import ADBAdapter
from .cross_transfer import CrossPlatformTransferManager, CrossTransferConfig, CrossTransferProgress
from .i18n import t, set_language, get_language, available_languages, on_language_change

# Optional iOS support
try:
    from .ios_core import iOSCore, is_ios_available
    _IOS_AVAILABLE = is_ios_available()
except Exception:
    _IOS_AVAILABLE = False

log = logging.getLogger("adb_toolkit.gui")

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------
COLORS = {
    "bg": "#1a1a2e",
    "surface": "#16213e",
    "card": "#0f3460",
    "accent": "#e94560",
    "accent_hover": "#ff6b6b",
    "success": "#06d6a0",
    "warning": "#ffd166",
    "error": "#ef476f",
    "text": "#edf2f4",
    "text_dim": "#8d99ae",
    "border": "#2b2d42",
}


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
class ADBToolkitApp(ctk.CTk):
    """Main application window."""

    def __init__(self, config: Config, adb: ADBCore):
        super().__init__()

        self.config = config
        self.adb = adb
        self.backup_mgr = BackupManager(adb)
        self.restore_mgr = RestoreManager(adb)
        self.transfer_mgr = TransferManager(adb)
        self.cleanup_mgr = CleanupManager(adb)
        self.toolbox_mgr = ToolboxManager(adb)
        self.driver_mgr = DriverManager(adb.base_dir)

        # Register device-confirmation overlay callbacks on all managers
        # that may trigger adb backup / adb restore commands
        for mgr in (self.backup_mgr, self.restore_mgr, self.transfer_mgr):
            mgr.set_confirmation_callback(
                self._show_device_confirmation,
                self._dismiss_device_confirmation,
            )

        # Cross-platform device manager
        self.device_mgr = DeviceManager()
        self.device_mgr.register(ADBAdapter(adb))
        if _IOS_AVAILABLE:
            try:
                self.device_mgr.register(iOSCore(adb.base_dir / "ios"))
            except Exception as exc:
                log.warning("Could not initialize iOS support: %s", exc)
        self.cross_transfer_mgr = CrossPlatformTransferManager(
            self.device_mgr, adb.base_dir / "transfers",
        )

        self.devices: Dict[str, DeviceInfo] = {}
        self.unified_devices: Dict[str, UnifiedDeviceInfo] = {}
        self.selected_device: Optional[str] = None
        self._closing = False
        self._ready = False  # True after UI fully built
        self._driver_install_running = False
        self._ui_locked = False
        self._confirm_dlg = None  # Device confirmation overlay

        # Window setup
        self.title(t("app.window_title"))
        w = config.get("ui.window_width", 1100)
        h = config.get("ui.window_height", 750)
        self.geometry(f"{w}x{h}")
        self.minsize(900, 600)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._build_ui()
        # Delay monitor start so the UI is fully rendered first
        self.after(1500, self._start_device_monitor)
        self._ready = True

    # ==================================================================
    # UI Construction
    # ==================================================================
    def _build_ui(self):
        # Top bar
        self._build_topbar()
        # Tab view
        self.tabview = ctk.CTkTabview(self, corner_radius=10)
        self.tabview.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self._tab_devices = self.tabview.add(t("tabs.devices"))
        self._tab_cleanup = self.tabview.add(t("tabs.cleanup"))
        self._tab_toolbox = self.tabview.add(t("tabs.toolbox"))
        self._tab_backup = self.tabview.add(t("tabs.backup"))
        self._tab_restore = self.tabview.add(t("tabs.restore"))
        self._tab_transfer = self.tabview.add(t("tabs.transfer"))
        self._tab_drivers = self.tabview.add(t("tabs.drivers"))
        self._tab_settings = self.tabview.add(t("tabs.settings"))

        self._build_devices_tab()
        self._build_cleanup_tab()
        self._build_toolbox_tab()
        self._build_backup_tab()
        self._build_restore_tab()
        self._build_transfer_tab()
        self._build_drivers_tab()
        self._build_settings_tab()

        # Status bar
        self._build_statusbar()

    # ------------------------------------------------------------------
    # Top bar
    # ------------------------------------------------------------------
    def _build_topbar(self):
        frame = ctk.CTkFrame(self, height=50, corner_radius=0)
        frame.pack(fill="x", padx=0, pady=0)
        frame.pack_propagate(False)

        ctk.CTkLabel(
            frame,
            text=t("topbar.title"),
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left", padx=16)

        self.lbl_connection = ctk.CTkLabel(
            frame,
            text=t("topbar.no_device"),
            text_color=COLORS["text_dim"],
        )
        self.lbl_connection.pack(side="left", padx=20)

        self.btn_refresh = ctk.CTkButton(
            frame, text=t("topbar.refresh"), width=100,
            command=self._refresh_devices,
        )
        self.btn_refresh.pack(side="right", padx=8, pady=8)

    # ------------------------------------------------------------------
    # Status bar (footer) with acceleration toggles
    # ------------------------------------------------------------------
    def _build_statusbar(self):
        frame = ctk.CTkFrame(self, height=32, corner_radius=0)
        frame.pack(fill="x", side="bottom")
        frame.pack_propagate(False)

        # Left — status text
        self.lbl_status = ctk.CTkLabel(
            frame, text=t("status.ready"), anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self.lbl_status.pack(side="left", padx=12)

        # Right — version
        self.lbl_version = ctk.CTkLabel(
            frame, text="v1.2.0", anchor="e",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
        )
        self.lbl_version.pack(side="right", padx=12)

        # Separator
        ctk.CTkLabel(
            frame, text="|", text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=11),
        ).pack(side="right", padx=2)

        # GPU acceleration label + toggle
        self.lbl_gpu_status = ctk.CTkLabel(
            frame, text="⚡GPU: …", anchor="e",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
        )
        self.lbl_gpu_status.pack(side="right", padx=(4, 2))

        self._gpu_toggle_var = ctk.BooleanVar(
            value=self.config.get("acceleration.gpu_enabled", True),
        )
        self.sw_gpu = ctk.CTkSwitch(
            frame, text="", width=36,
            variable=self._gpu_toggle_var,
            command=self._on_gpu_toggle,
            onvalue=True, offvalue=False,
        )
        self.sw_gpu.pack(side="right", padx=(0, 2))

        # Separator
        ctk.CTkLabel(
            frame, text="|", text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=11),
        ).pack(side="right", padx=2)

        # NPU label + toggle
        self.lbl_npu_status = ctk.CTkLabel(
            frame, text="🧠NPU: …", anchor="e",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
        )
        self.lbl_npu_status.pack(side="right", padx=(4, 2))

        self._npu_toggle_var = ctk.BooleanVar(
            value=self.config.get("acceleration.npu_enabled", True),
        )
        self.sw_npu = ctk.CTkSwitch(
            frame, text="", width=36,
            variable=self._npu_toggle_var,
            command=self._on_npu_toggle,
            onvalue=True, offvalue=False,
        )
        self.sw_npu.pack(side="right", padx=(0, 2))

        # Separator
        ctk.CTkLabel(
            frame, text="|", text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=11),
        ).pack(side="right", padx=2)

        # Virtualization label + toggle
        self.lbl_virt_status = ctk.CTkLabel(
            frame, text="🖥️Virt: …", anchor="e",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
        )
        self.lbl_virt_status.pack(side="right", padx=(4, 2))

        self._virt_toggle_var = ctk.BooleanVar(
            value=self.config.get("virtualization.enabled", True),
        )
        self.sw_virt = ctk.CTkSwitch(
            frame, text="", width=36,
            variable=self._virt_toggle_var,
            command=self._on_virt_toggle,
            onvalue=True, offvalue=False,
        )
        self.sw_virt.pack(side="right", padx=(0, 2))

        # Populate labels asynchronously (detection may be slow)
        threading.Thread(target=self._init_accel_footer, daemon=True).start()

    def _init_accel_footer(self):
        """Background detection of GPU + virtualization for footer labels.

        Auto-enables GPU acceleration and virtualization when the
        corresponding hardware is detected, matching the new 'always on
        when available' policy.
        """
        try:
            accel = self.transfer_mgr.accelerator

            # --- GPU auto-enable ---
            gpus = accel.usable_gpus
            if gpus:
                # Hardware present → force ON
                self._safe_after(0, lambda: self._gpu_toggle_var.set(True))
                accel.set_gpu_enabled(True)
                self.config.set("acceleration.gpu_enabled", True)
                if len(gpus) > 1:
                    accel.set_multi_gpu(True)
                    self.config.set("acceleration.multi_gpu", True)

            gpu_enabled = self._gpu_toggle_var.get()
            if gpus and gpu_enabled:
                best = gpus[0]
                gpu_txt = f"⚡GPU: {best.name}"
                if len(gpus) > 1:
                    gpu_txt += f" +{len(gpus)-1}"
                color = COLORS["success"]
            elif gpus:
                gpu_txt = f"⚡GPU: OFF ({gpus[0].name})"
                color = COLORS["warning"]
            else:
                all_g = accel.gpus
                if all_g:
                    gpu_txt = f"⚡GPU: {all_g[0].name} (sem lib)"
                else:
                    gpu_txt = "⚡GPU: N/A"
                color = COLORS["text_dim"]

            self._safe_after(0, lambda: self.lbl_gpu_status.configure(
                text=gpu_txt, text_color=color,
            ))

            virt = accel.virt
            # --- Virt auto-enable ---
            has_virt = virt.vtx_enabled or virt.hyperv_running or virt.wsl_available
            if has_virt:
                self._safe_after(0, lambda: self._virt_toggle_var.set(True))
                accel.set_virt_enabled(True)
                self.config.set("virtualization.enabled", True)

            virt_enabled = self._virt_toggle_var.get()
            parts = []
            if virt.vtx_enabled:
                parts.append("VT-x")
            if virt.hyperv_running:
                parts.append("Hyper-V")
            if virt.wsl_available:
                parts.append("WSL")

            if parts and virt_enabled:
                virt_txt = f"🖥️Virt: {', '.join(parts)}"
                vcolor = COLORS["success"]
            elif parts:
                virt_txt = f"🖥️Virt: OFF ({', '.join(parts)})"
                vcolor = COLORS["warning"]
            else:
                virt_txt = "🖥️Virt: N/A"
                vcolor = COLORS["text_dim"]

            self._safe_after(0, lambda: self.lbl_virt_status.configure(
                text=virt_txt, text_color=vcolor,
            ))

            # --- NPU auto-enable ---
            npus = accel.usable_npus
            if npus:
                self._safe_after(0, lambda: self._npu_toggle_var.set(True))
                accel.set_npu_enabled(True)
                self.config.set("acceleration.npu_enabled", True)

            npu_enabled = self._npu_toggle_var.get()
            if npus and npu_enabled:
                best_n = npus[0]
                npu_txt = f"🧠NPU: {best_n.name}"
                ncolor = COLORS["success"]
            elif npus:
                npu_txt = f"🧠NPU: OFF ({npus[0].name})"
                ncolor = COLORS["warning"]
            else:
                all_n = accel.npus
                if all_n:
                    npu_txt = f"🧠NPU: {all_n[0].name} (no framework)"
                else:
                    npu_txt = "🧠NPU: N/A"
                ncolor = COLORS["text_dim"]

            self._safe_after(0, lambda: self.lbl_npu_status.configure(
                text=npu_txt, text_color=ncolor,
            ))
        except Exception as exc:
            log.debug("Footer accel init error: %s", exc)

    def _on_gpu_toggle(self):
        """GPU toggle callback."""
        on = self._gpu_toggle_var.get()
        self.config.set("acceleration.gpu_enabled", on)
        self.transfer_mgr.accelerator.set_gpu_enabled(on)
        # Refresh footer label
        threading.Thread(target=self._init_accel_footer, daemon=True).start()
        state = t("common.enabled") if on else t("common.disabled")
        self._set_status(t("status.gpu_toggle", state=state))

    def _on_npu_toggle(self):
        """NPU toggle callback."""
        on = self._npu_toggle_var.get()
        self.config.set("acceleration.npu_enabled", on)
        self.transfer_mgr.accelerator.set_npu_enabled(on)
        threading.Thread(target=self._init_accel_footer, daemon=True).start()
        state = t("common.enabled") if on else t("common.disabled")
        self._set_status(t("status.npu_toggle", state=state))

    def _on_virt_toggle(self):
        """Virtualization toggle callback."""
        on = self._virt_toggle_var.get()
        self.config.set("virtualization.enabled", on)
        self.transfer_mgr.accelerator.set_virt_enabled(on)
        threading.Thread(target=self._init_accel_footer, daemon=True).start()
        state = t("common.enabled") if on else t("common.disabled")
        self._set_status(t("settings.virt_status", state=state))

    # ==================================================================
    # DEVICES TAB
    # ==================================================================
    def _build_devices_tab(self):
        tab = self._tab_devices

        # Device list frame
        list_frame = ctk.CTkFrame(tab)
        list_frame.pack(fill="both", expand=True, padx=8, pady=8)

        ctk.CTkLabel(
            list_frame,
            text=t("devices.title"),
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 4))

        self.device_list_frame = ctk.CTkScrollableFrame(list_frame, height=200)
        self.device_list_frame.pack(fill="both", expand=True, padx=8, pady=8)

        self.lbl_no_devices = ctk.CTkLabel(
            self.device_list_frame,
            text=t("devices.no_device_instructions"),
            font=ctk.CTkFont(size=13),
            text_color=COLORS["text_dim"],
            justify="center",
        )
        self.lbl_no_devices.pack(expand=True, pady=40)

        # Device details panel
        details_frame = ctk.CTkFrame(tab)
        details_frame.pack(fill="x", padx=8, pady=(0, 8))

        ctk.CTkLabel(
            details_frame,
            text=t("devices.details_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        self.device_details_text = ctk.CTkTextbox(details_frame, height=120)
        self.device_details_text.pack(fill="x", padx=8, pady=(0, 8))
        self.device_details_text.insert("end", t("devices.select_device"))
        self.device_details_text.configure(state="disabled")

    # ==================================================================
    # CLEANUP TAB
    # ==================================================================
    def _build_cleanup_tab(self):
        tab = self._tab_cleanup

        cleanup_scroll = ctk.CTkScrollableFrame(tab)
        cleanup_scroll.pack(fill="both", expand=True, padx=4, pady=4)

        # Header
        header = ctk.CTkFrame(cleanup_scroll)
        header.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            header, text=t("cleanup.title"),
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 2))

        ctk.CTkLabel(
            header,
            text=t("cleanup.subtitle"),
            font=ctk.CTkFont(size=12),
            text_color=COLORS["text_dim"],
        ).pack(anchor="w", padx=12, pady=(0, 8))

        # ------ Per-mode rows ------
        self._cleanup_mode_vars: Dict[CleanupMode, ctk.BooleanVar] = {}
        self._cleanup_mode_rows: Dict[CleanupMode, Dict] = {}

        modes_frame = ctk.CTkFrame(cleanup_scroll)
        modes_frame.pack(fill="x", padx=4, pady=4)

        for mode in MODE_ORDER:
            self._build_cleanup_mode_row(modes_frame, mode)

        # ------ Action buttons ------
        btn_frame = ctk.CTkFrame(cleanup_scroll)
        btn_frame.pack(fill="x", padx=4, pady=4)

        btn_row = ctk.CTkFrame(btn_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=8)

        self.btn_scan_cleanup = ctk.CTkButton(
            btn_row, text=t("cleanup.btn_scan"),
            command=self._start_cleanup_scan,
            fg_color=COLORS["card"], hover_color=COLORS["accent"],
            width=200,
        )
        self.btn_scan_cleanup.pack(side="left", padx=(0, 8))

        self.btn_execute_cleanup = ctk.CTkButton(
            btn_row, text=t("cleanup.btn_clean"),
            command=self._start_cleanup_execute,
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            width=200, state="disabled",
        )
        self.btn_execute_cleanup.pack(side="left", padx=(0, 8))

        self.btn_cancel_cleanup = ctk.CTkButton(
            btn_row, text=t("cleanup.btn_cancel"),
            command=self._cancel_cleanup,
            fg_color=COLORS["error"], hover_color="#c0392b",
            width=120, state="disabled",
        )
        self.btn_cancel_cleanup.pack(side="left")

        # ------ Summary panel ------
        summary_frame = ctk.CTkFrame(cleanup_scroll)
        summary_frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            summary_frame, text=t("cleanup.summary_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        self.cleanup_summary_text = ctk.CTkTextbox(summary_frame, height=150)
        self.cleanup_summary_text.pack(fill="x", padx=8, pady=(0, 8))
        self.cleanup_summary_text.insert("end", t("cleanup.summary_placeholder"))
        self.cleanup_summary_text.configure(state="disabled")

        # State
        self._cleanup_estimates: Dict[CleanupMode, ModeEstimate] = {}

    def _build_cleanup_mode_row(self, parent: ctk.CTkFrame, mode: CleanupMode):
        """Build one row with toggle, label, description, and progress bar."""
        row = ctk.CTkFrame(parent, corner_radius=8)
        row.pack(fill="x", padx=8, pady=3)
        row.columnconfigure(1, weight=1)

        # Enable toggle
        var = ctk.BooleanVar(value=mode != CleanupMode.DUPLICATES)
        self._cleanup_mode_vars[mode] = var

        chk = ctk.CTkCheckBox(
            row, text="", variable=var, width=24,
            checkbox_width=20, checkbox_height=20,
        )
        chk.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="w")

        # Label + description
        info_frame = ctk.CTkFrame(row, fg_color="transparent")
        info_frame.grid(row=0, column=1, sticky="ew", padx=4, pady=4)

        label_text = MODE_LABELS[mode]
        if mode == CleanupMode.DUPLICATES:
            label_text += "  " + t("cleanup.duplicates_note")

        ctk.CTkLabel(
            info_frame, text=label_text,
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).pack(anchor="w")

        ctk.CTkLabel(
            info_frame, text=MODE_DESCRIPTIONS[mode],
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"], anchor="w",
        ).pack(anchor="w")

        # Status label (estimate result / cleaning status)
        status_lbl = ctk.CTkLabel(
            row, text="—", width=180, anchor="e",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["text_dim"],
        )
        status_lbl.grid(row=0, column=2, padx=8, pady=8, sticky="e")

        # Progress bar
        progress_bar = ctk.CTkProgressBar(row, height=6)
        progress_bar.grid(row=1, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 6))
        progress_bar.set(0)

        # Detail label under progress
        detail_lbl = ctk.CTkLabel(
            row, text="", anchor="w",
            font=ctk.CTkFont(size=10),
            text_color=COLORS["text_dim"],
        )
        detail_lbl.grid(row=2, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 4))

        self._cleanup_mode_rows[mode] = {
            "row": row,
            "checkbox": chk,
            "status_lbl": status_lbl,
            "progress_bar": progress_bar,
            "detail_lbl": detail_lbl,
        }

    # ------------------------------------------------------------------
    # Cleanup operations
    # ------------------------------------------------------------------

    def _get_enabled_cleanup_modes(self) -> List[CleanupMode]:
        """Return enabled cleanup modes in execution order."""
        return [m for m in MODE_ORDER if self._cleanup_mode_vars.get(m, ctk.BooleanVar()).get()]

    def _start_cleanup_scan(self):
        serial = self._get_selected_device()
        if not serial:
            return

        modes = self._get_enabled_cleanup_modes()
        if not modes:
            messagebox.showwarning(t("cleanup.warn_no_mode_title"), t("cleanup.warn_no_mode_msg"))
            return

        self._lock_ui()
        self.btn_cancel_cleanup.configure(state="normal")
        self.btn_execute_cleanup.configure(state="disabled")
        self._cleanup_estimates.clear()

        # Reset all rows
        for mode in MODE_ORDER:
            widgets = self._cleanup_mode_rows[mode]
            widgets["status_lbl"].configure(text="—", text_color=COLORS["text_dim"])
            widgets["progress_bar"].set(0)
            widgets["detail_lbl"].configure(text="")

        # Register per-mode progress callbacks
        for mode in modes:
            self.cleanup_mgr.set_mode_progress_callback(
                mode, lambda p, _m=mode: self._on_cleanup_mode_progress(p),
            )

        self._set_status(t("cleanup.scanning"))
        self.cleanup_summary_text.configure(state="normal")
        self.cleanup_summary_text.delete("1.0", "end")
        self.cleanup_summary_text.insert("end", t("cleanup.scanning_text") + "\n")
        self.cleanup_summary_text.configure(state="disabled")

        def _run():
            try:
                self.cleanup_mgr.reset()
                estimates = self.cleanup_mgr.estimate(serial, modes)
                self._safe_after(0, lambda: self._scan_finished(estimates))
            except Exception as exc:
                log.exception("Scan error: %s", exc)
                self._safe_after(0, lambda: self._scan_error(str(exc)))

        threading.Thread(target=_run, daemon=True).start()

    def _on_cleanup_mode_progress(self, p: ModeProgress):
        def _update():
            if self._closing:
                return
            widgets = self._cleanup_mode_rows.get(p.mode)
            if not widgets:
                return
            try:
                widgets["progress_bar"].set(p.percent / 100)
                widgets["detail_lbl"].configure(text=p.message)
                if p.phase == "complete":
                    widgets["status_lbl"].configure(
                        text=p.message, text_color=COLORS["success"],
                    )
                elif p.phase == "error":
                    widgets["status_lbl"].configure(
                        text=t("common.error"), text_color=COLORS["error"],
                    )
                elif p.phase == "cleaning":
                    if p.bytes_freed > 0:
                        widgets["status_lbl"].configure(
                            text=f"{format_bytes(p.bytes_freed)} {t('common.freed')}",
                            text_color=COLORS["warning"],
                        )
            except Exception:
                pass
        self._safe_after(0, _update)

    def _scan_finished(self, estimates: Dict[CleanupMode, ModeEstimate]):
        self._cleanup_estimates = estimates
        self._unlock_ui()
        self.btn_cancel_cleanup.configure(state="disabled")

        # Build summary text
        total_items = 0
        total_bytes = 0
        lines = [t("cleanup.scan_header") + "\n", "=" * 50 + "\n\n"]

        for mode in MODE_ORDER:
            est = estimates.get(mode)
            if est is None:
                continue
            label = MODE_LABELS[mode]
            if est.error:
                lines.append(f"  ❌ {label}: {est.error}\n")
                widgets = self._cleanup_mode_rows[mode]
                widgets["status_lbl"].configure(text=t("common.error"), text_color=COLORS["error"])
            else:
                lines.append(
                    f"  {'✅' if est.total_items else '⭕'} {label}: "
                    f"{t('cleanup.mode_items', items=est.total_items, bytes=format_bytes(est.total_bytes))}\n"
                )
                total_items += est.total_items
                total_bytes += est.total_bytes

                widgets = self._cleanup_mode_rows[mode]
                if est.total_items:
                    widgets["status_lbl"].configure(
                        text=t("cleanup.mode_items", items=est.total_items, bytes=format_bytes(est.total_bytes)),
                        text_color=COLORS["warning"],
                    )
                else:
                    widgets["status_lbl"].configure(
                        text=t("cleanup.mode_clean"), text_color=COLORS["success"],
                    )

        lines.append(f"\n{'=' * 50}\n")
        lines.append(f"  TOTAL: {total_items} {t('common.items')}  •  ~{format_bytes(total_bytes)} {t('cleanup.bytes_freed', bytes='')}\n")

        if total_items > 0:
            lines.append(f"\n  {t('cleanup.click_clean')}\n")
            self.btn_execute_cleanup.configure(state="normal")
        else:
            lines.append(f"\n  {t('cleanup.already_clean')}\n")

        self.cleanup_summary_text.configure(state="normal")
        self.cleanup_summary_text.delete("1.0", "end")
        self.cleanup_summary_text.insert("end", "".join(lines))
        self.cleanup_summary_text.configure(state="disabled")
        self._set_status(t("cleanup.scan_done", items=total_items, bytes=format_bytes(total_bytes)))

    def _scan_error(self, error: str):
        self._unlock_ui()
        self.btn_cancel_cleanup.configure(state="disabled")
        self.cleanup_summary_text.configure(state="normal")
        self.cleanup_summary_text.delete("1.0", "end")
        self.cleanup_summary_text.insert("end", t("cleanup.scan_error_text", error=error))
        self.cleanup_summary_text.configure(state="disabled")
        self._set_status(t("cleanup.scan_error_status"))

    def _start_cleanup_execute(self):
        if not self._cleanup_estimates:
            return

        serial = self._get_selected_device()
        if not serial:
            return

        # Confirm
        total_items = sum(e.total_items for e in self._cleanup_estimates.values())
        total_bytes = sum(e.total_bytes for e in self._cleanup_estimates.values())
        ok = messagebox.askyesno(
            t("cleanup.confirm_title"),
            t("cleanup.confirm_msg", items=total_items, bytes=format_bytes(total_bytes)),
        )
        if not ok:
            return

        self._lock_ui()
        self.btn_cancel_cleanup.configure(state="normal")
        self.btn_execute_cleanup.configure(state="disabled")

        # Reset progress bars for execution
        for mode in self._cleanup_estimates:
            widgets = self._cleanup_mode_rows[mode]
            widgets["progress_bar"].set(0)
            widgets["detail_lbl"].configure(text=t("cleanup.waiting"))

        # Register progress callbacks again for execution phase
        for mode in self._cleanup_estimates:
            self.cleanup_mgr.set_mode_progress_callback(
                mode, lambda p, _m=mode: self._on_cleanup_mode_progress(p),
            )

        self._set_status(t("cleanup.executing"))

        def _run():
            try:
                self.cleanup_mgr.reset()
                results = self.cleanup_mgr.execute(serial, self._cleanup_estimates)
                self._safe_after(0, lambda: self._execute_finished(results))
            except Exception as exc:
                log.exception("Cleanup error: %s", exc)
                self._safe_after(0, lambda: self._execute_error(str(exc)))

        threading.Thread(target=_run, daemon=True).start()

    def _execute_finished(self, results: Dict[CleanupMode, ModeResult]):
        self._unlock_ui()
        self.btn_cancel_cleanup.configure(state="disabled")
        self._cleanup_estimates.clear()

        total_freed = sum(r.bytes_freed for r in results.values())
        total_removed = sum(r.items_removed for r in results.values())
        total_errors = sum(len(r.errors) for r in results.values())

        lines = [t("cleanup.done_header") + "\n", "=" * 50 + "\n\n"]

        for mode in MODE_ORDER:
            res = results.get(mode)
            if res is None:
                continue
            label = MODE_LABELS[mode]
            if res.errors:
                lines.append(
                    f"  ⚠️ {label}: {res.items_removed} {t('common.removed')}, "
                    f"{format_bytes(res.bytes_freed)} {t('common.freed')}  "
                    f"({len(res.errors)} {t('common.errors')})\n"
                )
            else:
                lines.append(
                    f"  ✅ {label}: {res.items_removed} {t('common.removed')}, "
                    f"{format_bytes(res.bytes_freed)} {t('common.freed')}\n"
                )

        lines.append(f"\n{'=' * 50}\n")
        lines.append(
            f"  TOTAL: {total_removed} {t('common.items')} {t('common.removed')}  •  "
            f"~{format_bytes(total_freed)} {t('common.freed')}"
        )
        if total_errors:
            lines.append(f"  •  {total_errors} {t('common.errors')}\n")
        else:
            lines.append("\n")

        self.cleanup_summary_text.configure(state="normal")
        self.cleanup_summary_text.delete("1.0", "end")
        self.cleanup_summary_text.insert("end", "".join(lines))
        self.cleanup_summary_text.configure(state="disabled")
        self._set_status(t("cleanup.done_status", bytes=format_bytes(total_freed)))

    def _execute_error(self, error: str):
        self._unlock_ui()
        self.btn_cancel_cleanup.configure(state="disabled")
        self.cleanup_summary_text.configure(state="normal")
        self.cleanup_summary_text.delete("1.0", "end")
        self.cleanup_summary_text.insert("end", t("cleanup.exec_error_text", error=error))
        self.cleanup_summary_text.configure(state="disabled")
        self._set_status(t("cleanup.exec_error_status"))

    def _cancel_cleanup(self):
        self.cleanup_mgr.cancel()
        self._set_status(t("cleanup.cancelled"))

    # ==================================================================
    # TOOLBOX TAB
    # ==================================================================
    def _build_toolbox_tab(self):
        tab = self._tab_toolbox

        # ── Two-column layout: controls (left) | output (right) ──
        container = ctk.CTkFrame(tab, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=4, pady=4)
        container.columnconfigure(0, weight=3)
        container.columnconfigure(1, weight=2)
        container.rowconfigure(0, weight=1)

        # Left column — scrollable controls
        scroll = ctk.CTkScrollableFrame(container)
        scroll.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        # Right column — output console
        right_frame = ctk.CTkFrame(container)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        # Header
        hdr = ctk.CTkFrame(scroll)
        hdr.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(
            hdr, text=t("toolbox.title"),
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 2))
        ctk.CTkLabel(
            hdr,
            text=t("toolbox.subtitle"),
            font=ctk.CTkFont(size=12), text_color=COLORS["text_dim"],
        ).pack(anchor="w", padx=12, pady=(0, 8))

        # ── Device Info section ──
        self._tb_section(scroll, t("toolbox.section.info"))

        info_btns = ctk.CTkFrame(scroll, fg_color="transparent")
        info_btns.pack(fill="x", padx=16, pady=4)

        self.btn_tb_device_info = ctk.CTkButton(
            info_btns, text=t("toolbox.btn.device_info"),
            command=self._tb_device_info, width=150,
        )
        self.btn_tb_device_info.pack(side="left", padx=4)

        self.btn_tb_battery = ctk.CTkButton(
            info_btns, text=t("toolbox.btn.battery"),
            command=self._tb_battery_info, width=150,
        )
        self.btn_tb_battery.pack(side="left", padx=4)

        self.btn_tb_storage = ctk.CTkButton(
            info_btns, text=t("toolbox.btn.storage"),
            command=self._tb_storage_info, width=150,
        )
        self.btn_tb_storage.pack(side="left", padx=4)

        self.btn_tb_network = ctk.CTkButton(
            info_btns, text=t("toolbox.btn.network"),
            command=self._tb_network_info, width=150,
        )
        self.btn_tb_network.pack(side="left", padx=4)

        # ── App Management section ──
        self._tb_section(scroll, t("toolbox.section.apps"))

        app_row1 = ctk.CTkFrame(scroll, fg_color="transparent")
        app_row1.pack(fill="x", padx=16, pady=4)

        self.btn_tb_list_apps = ctk.CTkButton(
            app_row1, text=t("toolbox.btn.list_apps"),
            command=self._tb_list_apps, width=150,
        )
        self.btn_tb_list_apps.pack(side="left", padx=4)

        self.btn_tb_clear_all_cache = ctk.CTkButton(
            app_row1, text=t("toolbox.btn.clear_all_cache"),
            command=self._tb_clear_all_cache, width=160,
        )
        self.btn_tb_clear_all_cache.pack(side="left", padx=4)

        self.btn_tb_force_stop_all = ctk.CTkButton(
            app_row1, text=t("toolbox.btn.force_stop_all"),
            command=self._tb_force_stop_all, width=150,
        )
        self.btn_tb_force_stop_all.pack(side="left", padx=4)

        # Single-app operations
        app_single = ctk.CTkFrame(scroll, fg_color="transparent")
        app_single.pack(fill="x", padx=16, pady=4)
        ctk.CTkLabel(app_single, text=t("toolbox.label.package")).pack(side="left")
        self.entry_tb_package = ctk.CTkEntry(app_single, width=300, placeholder_text="com.example.app")
        self.entry_tb_package.pack(side="left", padx=4)

        self.btn_tb_uninstall = ctk.CTkButton(
            app_single, text=t("toolbox.btn.uninstall"), width=120,
            fg_color=COLORS["error"], hover_color="#c0392b",
            command=self._tb_uninstall_app,
        )
        self.btn_tb_uninstall.pack(side="left", padx=4)

        self.btn_tb_force_stop = ctk.CTkButton(
            app_single, text=t("toolbox.btn.force_stop"), width=100,
            command=self._tb_force_stop_app,
        )
        self.btn_tb_force_stop.pack(side="left", padx=4)

        self.btn_tb_clear_data = ctk.CTkButton(
            app_single, text=t("toolbox.btn.clear_data"), width=120,
            fg_color=COLORS["warning"], hover_color="#e5a100",
            command=self._tb_clear_data,
        )
        self.btn_tb_clear_data.pack(side="left", padx=4)

        # ── Performance section ──
        self._tb_section(scroll, t("toolbox.section.perf"))

        perf_btns = ctk.CTkFrame(scroll, fg_color="transparent")
        perf_btns.pack(fill="x", padx=16, pady=4)

        self.btn_tb_kill_bg = ctk.CTkButton(
            perf_btns, text=t("toolbox.btn.kill_bg"),
            command=self._tb_kill_background, width=180,
        )
        self.btn_tb_kill_bg.pack(side="left", padx=4)

        self.btn_tb_fstrim = ctk.CTkButton(
            perf_btns, text="🔧 FSTRIM",
            command=self._tb_fstrim, width=120,
        )
        self.btn_tb_fstrim.pack(side="left", padx=4)

        self.btn_tb_reset_battery = ctk.CTkButton(
            perf_btns, text=t("toolbox.btn.reset_battery"),
            command=self._tb_reset_battery, width=160,
        )
        self.btn_tb_reset_battery.pack(side="left", padx=4)

        perf_btns2 = ctk.CTkFrame(scroll, fg_color="transparent")
        perf_btns2.pack(fill="x", padx=16, pady=4)

        # Animation scale
        ctk.CTkLabel(perf_btns2, text=t("toolbox.label.animations")).pack(side="left")
        self.tb_anim_var = ctk.StringVar(value="1.0")
        ctk.CTkOptionMenu(
            perf_btns2, values=["0", "0.25", "0.5", "1.0"],
            variable=self.tb_anim_var, width=80,
        ).pack(side="left", padx=4)
        self.btn_tb_set_anim = ctk.CTkButton(
            perf_btns2, text=t("toolbox.btn.apply_anim"), width=80,
            command=self._tb_set_animation,
        )
        self.btn_tb_set_anim.pack(side="left", padx=4)

        # ── Screen Capture section ──
        self._tb_section(scroll, t("toolbox.section.capture"))

        cap_btns = ctk.CTkFrame(scroll, fg_color="transparent")
        cap_btns.pack(fill="x", padx=16, pady=4)

        self.btn_tb_screenshot = ctk.CTkButton(
            cap_btns, text=t("toolbox.btn.screenshot"),
            command=self._tb_screenshot, width=150,
        )
        self.btn_tb_screenshot.pack(side="left", padx=4)

        self.btn_tb_screenrecord = ctk.CTkButton(
            cap_btns, text=t("toolbox.btn.screenrecord"),
            command=self._tb_screenrecord, width=180,
        )
        self.btn_tb_screenrecord.pack(side="left", padx=4)

        self.btn_tb_open_output = ctk.CTkButton(
            cap_btns, text=t("toolbox.btn.open_output"), width=120,
            command=lambda: open_folder(str(self.toolbox_mgr.output_dir)),
        )
        self.btn_tb_open_output.pack(side="left", padx=4)

        # ── WiFi ADB section ──
        self._tb_section(scroll, t("toolbox.section.wifi"))

        wifi_btns = ctk.CTkFrame(scroll, fg_color="transparent")
        wifi_btns.pack(fill="x", padx=16, pady=4)

        self.btn_tb_wifi_on = ctk.CTkButton(
            wifi_btns, text=t("toolbox.btn.wifi_on"),
            command=self._tb_enable_wifi_adb, width=160,
            fg_color=COLORS["success"], hover_color="#05c090",
        )
        self.btn_tb_wifi_on.pack(side="left", padx=4)

        self.btn_tb_wifi_off = ctk.CTkButton(
            wifi_btns, text=t("toolbox.btn.wifi_off"),
            command=self._tb_disable_wifi_adb, width=160,
        )
        self.btn_tb_wifi_off.pack(side="left", padx=4)

        self.lbl_tb_wifi_status = ctk.CTkLabel(
            wifi_btns, text="", font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
        )
        self.lbl_tb_wifi_status.pack(side="left", padx=8)

        # ── Developer Tools section ──
        self._tb_section(scroll, t("toolbox.section.dev"))

        dev_btns = ctk.CTkFrame(scroll, fg_color="transparent")
        dev_btns.pack(fill="x", padx=16, pady=4)

        self.tb_stay_awake_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            dev_btns, text=t("toolbox.btn.stay_awake"),
            variable=self.tb_stay_awake_var,
            command=self._tb_toggle_stay_awake,
        ).pack(side="left", padx=4)

        self.tb_show_touches_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            dev_btns, text=t("toolbox.btn.show_touches"),
            variable=self.tb_show_touches_var,
            command=self._tb_toggle_show_touches,
        ).pack(side="left", padx=16)

        dev_btns2 = ctk.CTkFrame(scroll, fg_color="transparent")
        dev_btns2.pack(fill="x", padx=16, pady=4)

        self.btn_tb_shell = ctk.CTkButton(
            dev_btns2, text=t("toolbox.btn.shell"),
            command=self._tb_open_shell, width=180,
            fg_color=COLORS["success"], hover_color="#05c090",
        )
        self.btn_tb_shell.pack(side="left", padx=4)

        # ── Reboot section ──
        self._tb_section(scroll, t("toolbox.section.reboot"))

        reboot_btns = ctk.CTkFrame(scroll, fg_color="transparent")
        reboot_btns.pack(fill="x", padx=16, pady=4)

        self.btn_tb_reboot = ctk.CTkButton(
            reboot_btns, text=t("toolbox.btn.reboot_normal"),
            command=self._tb_reboot_normal, width=120,
        )
        self.btn_tb_reboot.pack(side="left", padx=4)

        self.btn_tb_reboot_recovery = ctk.CTkButton(
            reboot_btns, text=t("toolbox.btn.reboot_recovery"),
            command=self._tb_reboot_recovery, width=120,
            fg_color=COLORS["warning"], hover_color="#e5a100",
        )
        self.btn_tb_reboot_recovery.pack(side="left", padx=4)

        self.btn_tb_reboot_bootloader = ctk.CTkButton(
            reboot_btns, text=t("toolbox.btn.reboot_bootloader"),
            command=self._tb_reboot_bootloader, width=120,
            fg_color=COLORS["warning"], hover_color="#e5a100",
        )
        self.btn_tb_reboot_bootloader.pack(side="left", padx=4)

        self.btn_tb_reboot_fastboot = ctk.CTkButton(
            reboot_btns, text=t("toolbox.btn.reboot_fastboot"),
            command=self._tb_reboot_fastboot, width=120,
            fg_color=COLORS["warning"], hover_color="#e5a100",
        )
        self.btn_tb_reboot_fastboot.pack(side="left", padx=4)

        self.btn_tb_shutdown = ctk.CTkButton(
            reboot_btns, text=t("toolbox.btn.shutdown"),
            command=self._tb_shutdown, width=120,
            fg_color=COLORS["error"], hover_color="#c0392b",
        )
        self.btn_tb_shutdown.pack(side="left", padx=4)

        # ── Logcat section ──
        self._tb_section(scroll, t("toolbox.section.logcat"))

        log_btns = ctk.CTkFrame(scroll, fg_color="transparent")
        log_btns.pack(fill="x", padx=16, pady=4)

        self.btn_tb_logcat = ctk.CTkButton(
            log_btns, text=t("toolbox.btn.logcat"),
            command=self._tb_capture_logcat, width=160,
        )
        self.btn_tb_logcat.pack(side="left", padx=4)

        self.btn_tb_clear_logcat = ctk.CTkButton(
            log_btns, text=t("toolbox.btn.clear_logcat"),
            command=self._tb_clear_logcat, width=140,
        )
        self.btn_tb_clear_logcat.pack(side="left", padx=4)

        ctk.CTkLabel(log_btns, text=t("toolbox.label.filter")).pack(side="left", padx=(8, 2))
        self.entry_tb_logcat_filter = ctk.CTkEntry(
            log_btns, width=200, placeholder_text=t("toolbox.placeholder.filter"),
        )
        self.entry_tb_logcat_filter.pack(side="left", padx=4)

        # ── Output console (right column) ──
        ctk.CTkLabel(
            right_frame, text=t("toolbox.output_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        self.tb_output = ctk.CTkTextbox(right_frame, font=ctk.CTkFont(family="Consolas", size=11))
        self.tb_output.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.tb_output.insert("end", t("toolbox.output_placeholder") + "\n")
        self.tb_output.configure(state="disabled")

        # Progress bar
        self.tb_progress = ctk.CTkProgressBar(right_frame, height=6)
        self.tb_progress.pack(fill="x", padx=8, pady=(0, 8))
        self.tb_progress.set(0)

    # -- helpers -----------------------------------------------------------
    def _tb_section(self, parent, title: str):
        """Render a section header inside the ToolBox tab."""
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", padx=8, pady=(12, 2))
        ctk.CTkLabel(
            f, text=title, font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=4)

    def _tb_write(self, text: str, clear: bool = False):
        """Append text to the ToolBox output console (thread-safe)."""
        def _do():
            self.tb_output.configure(state="normal")
            if clear:
                self.tb_output.delete("1.0", "end")
            self.tb_output.insert("end", text + "\n")
            self.tb_output.see("end")
            self.tb_output.configure(state="disabled")
        self._safe_after(0, _do)

    def _tb_serial(self) -> Optional[str]:
        """Get selected device serial, with user warning."""
        s = self._get_selected_device()
        if not s:
            return None
        return s

    # -- Device Info callbacks -----------------------------------------------
    def _tb_device_info(self):
        serial = self._tb_serial()
        if not serial:
            return
        self._tb_write(t("toolbox.msg.collecting_info"), clear=True)

        def _run():
            try:
                info = self.toolbox_mgr.get_device_overview(serial)
                lines = [
                    t("toolbox.msg.info_header"),
                    f"  {t('toolbox.msg.model')}       {info.manufacturer} {info.model}",
                    f"  {t('toolbox.msg.brand')}        {info.brand}",
                    f"  {t('toolbox.msg.android')}      {info.android_version}  (SDK {info.sdk_level})",
                    f"  {t('toolbox.msg.build')}        {info.build_number}",
                    f"  {t('toolbox.msg.security')}    {info.security_patch}",
                    f"  {t('toolbox.msg.kernel')}       {info.kernel}",
                    f"  {t('toolbox.msg.cpu')}          {info.cpu_hardware or info.cpu_abi}  ({info.cpu_cores} cores)",
                    f"  {t('toolbox.msg.ram')}          {t('toolbox.msg.ram_detail', total=info.ram_total_mb, free=info.ram_available_mb)}",
                    f"  {t('toolbox.msg.display')}         {info.display_resolution}  ({info.display_density} DPI)",
                    f"  {t('toolbox.msg.serial')}       {info.serial_number}",
                    f"  {t('toolbox.msg.uptime')}       {info.uptime}",
                ]
                self._tb_write("\n".join(lines), clear=True)
            except Exception as exc:
                self._tb_write(f"{t('toolbox.msg.error', exc=exc)}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    def _tb_battery_info(self):
        serial = self._tb_serial()
        if not serial:
            return
        self._tb_write(t("toolbox.msg.reading_battery"), clear=True)

        def _run():
            try:
                b = self.toolbox_mgr.get_battery_info(serial)
                lines = [
                    t("toolbox.msg.battery_header"),
                    f"  {t('toolbox.msg.battery_level')}        {b.level}%",
                    f"  {t('toolbox.msg.battery_status')}       {b.status}",
                    f"  {t('toolbox.msg.battery_health')}        {b.health}",
                    f"  {t('toolbox.msg.battery_temp')}  {b.temperature:.1f} °C",
                    f"  {t('toolbox.msg.battery_voltage')}       {b.voltage:.2f} V",
                    f"  {t('toolbox.msg.battery_tech')}   {b.technology}",
                    f"  {t('toolbox.msg.battery_power')}  {b.plugged}",
                ]
                if b.current_now:
                    current_ma = b.current_now / 1000
                    lines.append(f"  {t('toolbox.msg.battery_current')}     {current_ma:.0f} mA")
                if b.capacity:
                    lines.append(f"  {t('toolbox.msg.battery_capacity')}   {b.capacity} mAh")
                self._tb_write("\n".join(lines), clear=True)
            except Exception as exc:
                self._tb_write(f"{t('common.error')}: {exc}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    def _tb_storage_info(self):
        serial = self._tb_serial()
        if not serial:
            return
        self._tb_write(t("toolbox.msg.reading_storage"), clear=True)

        def _run():
            try:
                parts = self.toolbox_mgr.get_storage_info(serial)
                lines = [t("toolbox.msg.storage_header")]
                lines.append(f"  {t('toolbox.msg.storage_partition'):<20} {t('common.total'):>8} {t('toolbox.msg.storage_used'):>8} {t('toolbox.msg.storage_free'):>8} {t('toolbox.msg.storage_usage'):>6}")
                lines.append("  " + "─" * 56)
                for p in parts:
                    lines.append(
                        f"  {p.partition:<20} {p.total_mb:>6} MB {p.used_mb:>6} MB "
                        f"{p.available_mb:>6} MB {p.use_percent:>5.1f}%"
                    )
                self._tb_write("\n".join(lines), clear=True)
            except Exception as exc:
                self._tb_write(f"{t('common.error')}: {exc}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    def _tb_network_info(self):
        serial = self._tb_serial()
        if not serial:
            return
        self._tb_write(t("toolbox.msg.reading_network"), clear=True)

        def _run():
            try:
                net = self.toolbox_mgr.get_network_info(serial)
                lines = [
                    t("toolbox.msg.network_header"),
                    f"  IP WiFi:      {net.get('ip_wifi', 'N/A')}",
                    f"  SSID:         {net.get('ssid', 'N/A')}",
                    f"  {t('toolbox.msg.net_mobile')}: {net.get('mobile_type', 'N/A')}",
                    f"  Bluetooth:    {net.get('bluetooth', 'N/A')}",
                    f"  {t('toolbox.msg.net_airplane')}:   {net.get('airplane_mode', 'N/A')}",
                ]
                self._tb_write("\n".join(lines), clear=True)
            except Exception as exc:
                self._tb_write(f"{t('common.error')}: {exc}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    # -- App Management callbacks --------------------------------------------
    def _tb_list_apps(self):
        serial = self._tb_serial()
        if not serial:
            return
        self._tb_write(t("toolbox.msg.listing_apps"), clear=True)

        def _run():
            try:
                apps = self.toolbox_mgr.list_apps(serial)
                lines = [f"═══ {len(apps)} {t('toolbox.msg.apps_count', count=len(apps))} ═══"]
                for a in apps:
                    ver = f" v{a.version_name}" if a.version_name else ""
                    lines.append(f"  {a.package}{ver}")
                self._tb_write("\n".join(lines), clear=True)
            except Exception as exc:
                self._tb_write(f"{t('common.error')}: {exc}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    def _tb_clear_all_cache(self):
        serial = self._tb_serial()
        if not serial:
            return
        self._tb_write(t("toolbox.msg.clearing_cache"), clear=True)
        self.tb_progress.set(0)

        def _progress(p: ToolboxProgress):
            self._safe_after(0, lambda: self.tb_progress.set(p.percent / 100))
            if p.detail:
                self._tb_write(f"  {p.detail}")

        def _run():
            try:
                count = self.toolbox_mgr.clear_all_apps_cache(serial, _progress)
                self._tb_write(f"\n{t('toolbox.msg.cache_cleared', count=count)}")
                self._safe_after(0, lambda: self.tb_progress.set(1))
            except Exception as exc:
                self._tb_write(f"{t('common.error')}: {exc}")

        threading.Thread(target=_run, daemon=True).start()

    def _tb_force_stop_all(self):
        serial = self._tb_serial()
        if not serial:
            return
        self._tb_write(t("toolbox.msg.stopping_all"), clear=True)
        self.tb_progress.set(0)

        def _progress(p: ToolboxProgress):
            self._safe_after(0, lambda: self.tb_progress.set(p.percent / 100))

        def _run():
            try:
                count = self.toolbox_mgr.bulk_force_stop(serial, _progress)
                self._tb_write(t("toolbox.msg.all_stopped", count=count), clear=True)
                self._safe_after(0, lambda: self.tb_progress.set(1))
            except Exception as exc:
                self._tb_write(f"{t('common.error')}: {exc}")

        threading.Thread(target=_run, daemon=True).start()

    def _tb_uninstall_app(self):
        serial = self._tb_serial()
        if not serial:
            return
        pkg = self.entry_tb_package.get().strip()
        if not pkg:
            self._tb_write(t("toolbox.msg.no_package"), clear=True)
            return
        if not messagebox.askyesno(t("toolbox.msg.uninstall_confirm_title"), t("toolbox.msg.uninstall_confirm_msg", pkg=pkg)):
            return

        def _run():
            ok, msg = self.toolbox_mgr.uninstall_app(serial, pkg)
            icon = "✅" if ok else "❌"
            self._tb_write(f"{icon} {pkg}: {msg}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    def _tb_force_stop_app(self):
        serial = self._tb_serial()
        if not serial:
            return
        pkg = self.entry_tb_package.get().strip()
        if not pkg:
            self._tb_write(t("toolbox.msg.no_package"), clear=True)
            return

        def _run():
            self.toolbox_mgr.force_stop_app(serial, pkg)
            self._tb_write(t("toolbox.msg.app_stopped", pkg=pkg), clear=True)

        threading.Thread(target=_run, daemon=True).start()

    def _tb_clear_data(self):
        serial = self._tb_serial()
        if not serial:
            return
        pkg = self.entry_tb_package.get().strip()
        if not pkg:
            self._tb_write(t("toolbox.msg.no_package"), clear=True)
            return
        if not messagebox.askyesno(t("toolbox.msg.clearing_data_confirm_title"), t("toolbox.msg.clearing_data_confirm_msg", pkg=pkg)):
            return

        def _run():
            ok, msg = self.toolbox_mgr.clear_app_data(serial, pkg)
            icon = "✅" if ok else "❌"
            self._tb_write(f"{icon} {pkg}: {msg}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    # -- Performance callbacks -----------------------------------------------
    def _tb_kill_background(self):
        serial = self._tb_serial()
        if not serial:
            return

        def _run():
            count = self.toolbox_mgr.kill_background_apps(serial)
            procs = self.toolbox_mgr.get_running_processes_count(serial)
            self._tb_write(
                f"💀 {t('toolbox.msg.bg_killed')}\n"
                f"   {t('toolbox.msg.running_processes')}: {procs}",
                clear=True,
            )

        threading.Thread(target=_run, daemon=True).start()

    def _tb_fstrim(self):
        serial = self._tb_serial()
        if not serial:
            return
        self._tb_write(t("toolbox.msg.running_fstrim"), clear=True)

        def _run():
            result = self.toolbox_mgr.run_fstrim(serial)
            self._tb_write(f"🔧 FSTRIM:\n{result}")

        threading.Thread(target=_run, daemon=True).start()

    def _tb_reset_battery(self):
        serial = self._tb_serial()
        if not serial:
            return

        def _run():
            ok = self.toolbox_mgr.reset_battery_stats(serial)
            icon = "✅" if ok else "❌"
            self._tb_write(f"{icon} {t('toolbox.msg.battery_reset')}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    def _tb_set_animation(self):
        serial = self._tb_serial()
        if not serial:
            return
        try:
            scale = float(self.tb_anim_var.get())
        except ValueError:
            scale = 1.0

        def _run():
            self.toolbox_mgr.set_animation_scale(serial, scale)
            label = t("common.disabled") if scale == 0 else f"{scale}x"
            self._tb_write(t("toolbox.msg.animation_set", scale=label), clear=True)

        threading.Thread(target=_run, daemon=True).start()

    # -- Screen Capture callbacks --------------------------------------------
    def _tb_screenshot(self):
        serial = self._tb_serial()
        if not serial:
            return
        self._tb_write(t("toolbox.msg.taking_screenshot"), clear=True)

        def _run():
            ok, path = self.toolbox_mgr.take_screenshot(serial)
            if ok:
                self._tb_write(t("toolbox.msg.screenshot_saved", path=path))
            else:
                self._tb_write(t("toolbox.msg.screenshot_failed"))

        threading.Thread(target=_run, daemon=True).start()

    def _tb_screenrecord(self):
        serial = self._tb_serial()
        if not serial:
            return
        self._tb_write(t("toolbox.msg.recording_screen"), clear=True)

        def _run():
            ok, path = self.toolbox_mgr.start_screenrecord(serial, duration=30)
            if ok:
                self._tb_write(t("toolbox.msg.screen_recorded", path=path))
            else:
                self._tb_write(t("toolbox.msg.screen_record_failed"))

        threading.Thread(target=_run, daemon=True).start()

    # -- WiFi ADB callbacks --------------------------------------------------
    def _tb_enable_wifi_adb(self):
        serial = self._tb_serial()
        if not serial:
            return
        self._tb_write(t("toolbox.msg.enabling_wifi"), clear=True)

        def _run():
            ok, addr = self.toolbox_mgr.enable_wifi_adb(serial)
            if ok:
                self._tb_write(t("toolbox.msg.wifi_enabled", ip=addr))
                self._safe_after(0, lambda: self.lbl_tb_wifi_status.configure(
                    text=f"{t('toolbox.msg.wifi_connected')}: {addr}", text_color=COLORS["success"],
                ))
            else:
                self._tb_write(f"❌ {addr}")
                self._safe_after(0, lambda: self.lbl_tb_wifi_status.configure(
                    text=t("toolbox.msg.wifi_failed"), text_color=COLORS["error"],
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _tb_disable_wifi_adb(self):
        serial = self._tb_serial()
        if not serial:
            return

        def _run():
            ok = self.toolbox_mgr.disable_wifi_adb(serial)
            icon = "✅" if ok else "❌"
            self._tb_write(f"{icon} {t('toolbox.msg.wifi_disabled')}", clear=True)
            self._safe_after(0, lambda: self.lbl_tb_wifi_status.configure(text=""))

        threading.Thread(target=_run, daemon=True).start()

    # -- Developer tools callbacks -------------------------------------------
    def _tb_toggle_stay_awake(self):
        serial = self._tb_serial()
        if not serial:
            return
        on = self.tb_stay_awake_var.get()

        def _run():
            self.toolbox_mgr.toggle_stay_awake(serial, on)
            state = t("common.enabled") if on else t("common.disabled")
            self._tb_write(f"🖥️ {t('toolbox.msg.stay_awake_on') if on else t('toolbox.msg.stay_awake_off')}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    def _tb_toggle_show_touches(self):
        serial = self._tb_serial()
        if not serial:
            return
        on = self.tb_show_touches_var.get()

        def _run():
            self.toolbox_mgr.toggle_show_touches(serial, on)
            state = t("common.enabled") if on else t("common.disabled")
            self._tb_write(f"👆 {t('toolbox.msg.show_touches_on') if on else t('toolbox.msg.show_touches_off')}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    def _tb_open_shell(self):
        serial = self._tb_serial()
        if not serial:
            return
        adb = self.adb.adb_path
        if not adb:
            self._tb_write(t("toolbox.msg.shell_no_adb"), clear=True)
            return
        cmd = [adb, "-s", serial, "shell"]
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "cmd", "/k"] + cmd,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self._tb_write(
                t("toolbox.msg.shell_opened", serial=serial),
                clear=True,
            )
        except Exception as exc:
            self._tb_write(t("toolbox.msg.shell_error", exc=exc), clear=True)

    # -- Reboot callbacks ----------------------------------------------------
    def _tb_reboot_normal(self):
        serial = self._tb_serial()
        if not serial:
            return
        if messagebox.askyesno(t("toolbox.msg.reboot_confirm_title"), t("toolbox.msg.reboot_confirm_msg")):
            self.toolbox_mgr.reboot_normal(serial)
            self._tb_write(t("toolbox.msg.rebooting"), clear=True)

    def _tb_reboot_recovery(self):
        serial = self._tb_serial()
        if not serial:
            return
        if messagebox.askyesno(t("toolbox.msg.recovery_confirm_title"), t("toolbox.msg.recovery_confirm_msg")):
            self.toolbox_mgr.reboot_recovery(serial)
            self._tb_write(t("toolbox.msg.recovery_rebooting"), clear=True)

    def _tb_reboot_bootloader(self):
        serial = self._tb_serial()
        if not serial:
            return
        if messagebox.askyesno(t("toolbox.msg.bootloader_confirm_title"), t("toolbox.msg.bootloader_confirm_msg")):
            self.toolbox_mgr.reboot_bootloader(serial)
            self._tb_write(t("toolbox.msg.bootloader_rebooting"), clear=True)

    def _tb_reboot_fastboot(self):
        serial = self._tb_serial()
        if not serial:
            return
        if messagebox.askyesno(t("toolbox.msg.fastboot_confirm_title"), t("toolbox.msg.fastboot_confirm_msg")):
            self.toolbox_mgr.reboot_fastboot(serial)
            self._tb_write(t("toolbox.msg.fastboot_rebooting"), clear=True)

    def _tb_shutdown(self):
        serial = self._tb_serial()
        if not serial:
            return
        if messagebox.askyesno(t("toolbox.msg.shutdown_confirm_title"), t("toolbox.msg.shutdown_confirm_msg")):
            self.toolbox_mgr.shutdown(serial)
            self._tb_write(t("toolbox.msg.shutting_down"), clear=True)

    # -- Logcat callbacks ----------------------------------------------------
    def _tb_capture_logcat(self):
        serial = self._tb_serial()
        if not serial:
            return
        tag = self.entry_tb_logcat_filter.get().strip()
        self._tb_write(t("toolbox.msg.capturing_logcat"), clear=True)

        def _run():
            text, path = self.toolbox_mgr.capture_logcat(serial, lines=500, filter_tag=tag)
            self._tb_write(f"═══ Logcat (salvo em {path}) ═══\n{text}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    def _tb_clear_logcat(self):
        serial = self._tb_serial()
        if not serial:
            return

        def _run():
            ok = self.toolbox_mgr.clear_logcat(serial)
            icon = "✅" if ok else "❌"
            self._tb_write(f"{icon} {t('toolbox.msg.logcat_cleared')}", clear=True)

        threading.Thread(target=_run, daemon=True).start()

    # ==================================================================
    # BACKUP TAB
    # ==================================================================
    def _build_backup_tab(self):
        tab = self._tab_backup

        # Main scrollable container for backup tab
        backup_scroll = ctk.CTkScrollableFrame(tab)
        backup_scroll.pack(fill="both", expand=True, padx=4, pady=4)

        # ------ Options section ------
        opts_frame = ctk.CTkFrame(backup_scroll)
        opts_frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            opts_frame,
            text=t("backup.title"),
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        # Backup type selection
        type_frame = ctk.CTkFrame(opts_frame, fg_color="transparent")
        type_frame.pack(fill="x", padx=12, pady=4)

        self.backup_type_var = ctk.StringVar(value="selective")
        ctk.CTkRadioButton(
            type_frame, text=t("backup.type_selective"), variable=self.backup_type_var,
            value="selective", command=self._on_backup_type_change,
        ).pack(side="left", padx=(0, 20))
        ctk.CTkRadioButton(
            type_frame, text=t("backup.type_full"), variable=self.backup_type_var,
            value="full", command=self._on_backup_type_change,
        ).pack(side="left", padx=(0, 20))
        ctk.CTkRadioButton(
            type_frame, text=t("backup.type_custom"), variable=self.backup_type_var,
            value="custom", command=self._on_backup_type_change,
        ).pack(side="left")

        # ------ Standard category checkboxes ------
        self.backup_cats_frame = ctk.CTkFrame(opts_frame, fg_color="transparent")
        self.backup_cats_frame.pack(fill="x", padx=12, pady=8)

        self.backup_cat_vars: Dict[str, ctk.BooleanVar] = {}
        categories = [
            ("apps", t("backup.category.apps")),
            ("photos", t("backup.category.photos")),
            ("videos", t("backup.category.videos")),
            ("music", t("backup.category.music")),
            ("documents", t("backup.category.documents")),
            ("contacts", t("backup.category.contacts")),
            ("sms", t("backup.category.sms")),
        ]
        for i, (key, label) in enumerate(categories):
            var = ctk.BooleanVar(value=True)
            self.backup_cat_vars[key] = var
            ctk.CTkCheckBox(
                self.backup_cats_frame, text=label, variable=var,
            ).grid(row=i // 4, column=i % 4, sticky="w", padx=8, pady=4)

        # ------ Messaging Apps section ------
        msg_frame = ctk.CTkFrame(backup_scroll)
        msg_frame.pack(fill="x", padx=4, pady=4)

        msg_header = ctk.CTkFrame(msg_frame, fg_color="transparent")
        msg_header.pack(fill="x", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            msg_header,
            text=t("backup.messaging_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")

        ctk.CTkButton(
            msg_header, text=t("backup.btn_detect"), width=120, height=28,
            command=self._detect_messaging_apps,
        ).pack(side="right", padx=4)

        self.messaging_app_vars: Dict[str, ctk.BooleanVar] = {}
        self.backup_msg_frame = ctk.CTkFrame(msg_frame, fg_color="transparent")
        self.backup_msg_frame.pack(fill="x", padx=12, pady=(0, 8))

        # Pre-populate with all known apps (unchecked)
        self._build_messaging_checkboxes(self.backup_msg_frame, self.messaging_app_vars)

        # ------ Unsynced / Local-Only Apps section ------
        unsync_frame = ctk.CTkFrame(backup_scroll)
        unsync_frame.pack(fill="x", padx=4, pady=4)

        unsync_header = ctk.CTkFrame(unsync_frame, fg_color="transparent")
        unsync_header.pack(fill="x", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            unsync_header,
            text=t("backup.unsynced_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")

        self.btn_detect_unsynced = ctk.CTkButton(
            unsync_header,
            text=t("backup.btn_detect_unsynced"),
            width=220,
            height=28,
            command=self._detect_unsynced_apps,
        )
        self.btn_detect_unsynced.pack(side="right", padx=4)

        ctk.CTkLabel(
            unsync_frame,
            text=t("backup.unsynced_desc"),
            font=ctk.CTkFont(size=11),
            text_color="#8d99ae",
            wraplength=700,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 4))

        self.unsynced_app_vars: Dict[str, ctk.BooleanVar] = {}
        self._detected_apps: List[DetectedApp] = []
        self.backup_unsynced_frame = ctk.CTkScrollableFrame(
            unsync_frame, height=160, fg_color="transparent",
        )
        self.backup_unsynced_frame.pack(fill="x", padx=8, pady=(0, 8))

        # Placeholder text
        self._unsynced_placeholder = ctk.CTkLabel(
            self.backup_unsynced_frame,
            text=t("backup.unsynced_placeholder"),
            text_color="#8d99ae",
            font=ctk.CTkFont(size=11),
        )
        self._unsynced_placeholder.pack(pady=10)

        # ------ File Tree Browser (for custom mode) ------
        tree_label_frame = ctk.CTkFrame(backup_scroll)
        tree_label_frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            tree_label_frame,
            text=t("backup.tree_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            tree_label_frame,
            text=t("backup.tree_desc"),
            font=ctk.CTkFont(size=11),
            text_color="#8d99ae",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        self.backup_tree = DeviceTreeBrowser(
            tree_label_frame,
            adb_core=self.adb,
            serial=None,
            root_path="/sdcard",
            height=200,
        )
        self.backup_tree.pack(fill="x", padx=8, pady=(0, 8))

        # ------ Action buttons ------
        btn_frame = ctk.CTkFrame(backup_scroll, fg_color="transparent")
        btn_frame.pack(fill="x", padx=4, pady=4)

        self.btn_start_backup = ctk.CTkButton(
            btn_frame,
            text=t("backup.btn_start"),
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["success"],
            hover_color="#05c090",
            height=42,
            command=self._start_backup,
        )
        self.btn_start_backup.pack(side="left", padx=8)

        self.btn_cancel_backup = ctk.CTkButton(
            btn_frame, text=t("backup.btn_cancel"), fg_color=COLORS["error"],
            hover_color="#d63a5e", height=42, state="disabled",
            command=self._cancel_backup,
        )
        self.btn_cancel_backup.pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text=t("backup.btn_open_folder"),
            height=42, command=self._open_backup_folder,
        ).pack(side="right", padx=8)

        # ------ Progress section ------
        progress_frame = ctk.CTkFrame(backup_scroll)
        progress_frame.pack(fill="x", padx=4, pady=4)

        self.backup_progress_label = ctk.CTkLabel(
            progress_frame, text=t("backup.progress_waiting"), anchor="w",
        )
        self.backup_progress_label.pack(fill="x", padx=12, pady=(8, 2))

        self.backup_progress_bar = ctk.CTkProgressBar(progress_frame)
        self.backup_progress_bar.pack(fill="x", padx=12, pady=(0, 4))
        self.backup_progress_bar.set(0)

        self.backup_progress_detail = ctk.CTkLabel(
            progress_frame, text="", anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
        )
        self.backup_progress_detail.pack(fill="x", padx=12, pady=(0, 8))

        # ------ Backup list ------
        list_frame = ctk.CTkFrame(backup_scroll)
        list_frame.pack(fill="x", padx=4, pady=(0, 4))

        ctk.CTkLabel(
            list_frame,
            text=t("backup.saved_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        self.backup_list_frame = ctk.CTkScrollableFrame(list_frame, height=150)
        self.backup_list_frame.pack(fill="x", padx=8, pady=(0, 8))

    def _on_backup_type_change(self):
        btype = self.backup_type_var.get()
        is_selective = btype == "selective"
        state = "normal" if is_selective else "disabled"
        for child in self.backup_cats_frame.winfo_children():
            try:
                child.configure(state=state)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Messaging app detection helpers (shared by backup + transfer tabs)
    # ------------------------------------------------------------------
    def _build_messaging_checkboxes(
        self, parent_frame, var_dict: Dict[str, ctk.BooleanVar]
    ):
        """Populate messaging app checkboxes in a grid layout."""
        for w in parent_frame.winfo_children():
            w.destroy()
        var_dict.clear()

        apps = list(MESSAGING_APPS.items())
        for i, (key, info) in enumerate(apps):
            var = ctk.BooleanVar(value=False)
            var_dict[key] = var
            ctk.CTkCheckBox(
                parent_frame,
                text=f"{info['icon']} {info['name']}",
                variable=var,
            ).grid(row=i // 4, column=i % 4, sticky="w", padx=8, pady=3)

    def _detect_messaging_apps(self):
        """Detect which messaging apps are installed on the selected device."""
        serial = self._get_selected_device()
        if not serial:
            return

        self._set_status(t("backup.detecting_messaging"))

        def _run():
            try:
                detector = MessagingAppDetector(self.adb)
                installed = detector.detect_installed_apps(serial)
                self.after(0, lambda: self._on_messaging_detected(installed))
            except Exception as exc:
                log.warning("Messaging detection error: %s", exc)
                self.after(0, lambda: self._set_status(f"{t('common.error')}: {exc}"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_messaging_detected(self, installed: Dict):
        """Update messaging checkboxes based on detection results."""
        count = len(installed)
        self._set_status(t("backup.messaging_detected", count=count))

        # Check the detected apps
        for key in self.messaging_app_vars:
            self.messaging_app_vars[key].set(key in installed)

        # Also update transfer messaging vars if they exist
        if hasattr(self, "transfer_msg_vars"):
            for key in self.transfer_msg_vars:
                self.transfer_msg_vars[key].set(key in installed)

    # ------------------------------------------------------------------
    # Unsynced app detection
    # ------------------------------------------------------------------
    def _detect_unsynced_apps(self):
        """Scan device for apps with local-only data (runs in background)."""
        serial = self._get_selected_device()
        if not serial:
            return

        self.btn_detect_unsynced.configure(state="disabled", text=t("backup.scanning_btn"))
        self._set_status(t("backup.scanning_local_data"))

        def _run():
            try:
                detector = UnsyncedAppDetector(self.adb)
                detected = detector.detect(serial, include_unknown=True, min_data_size_kb=256)
                self.after(0, lambda: self._on_unsynced_detected(detected))
            except Exception as exc:
                log.warning("Unsynced app detection error: %s", exc)
                self.after(0, lambda: self._set_status(f"{t('common.error')}: {exc}"))
                self.after(0, lambda: self.btn_detect_unsynced.configure(
                    state="normal", text=t("backup.btn_detect_unsynced"),
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _on_unsynced_detected(self, detected: List[DetectedApp]):
        """Render detected unsynced apps as checkboxes grouped by category."""
        self._detected_apps = detected
        self.btn_detect_unsynced.configure(
            state="normal", text=t("backup.btn_detect_unsynced"),
        )

        # Clear previous content
        for w in self.backup_unsynced_frame.winfo_children():
            w.destroy()
        self.unsynced_app_vars.clear()

        if not detected:
            ctk.CTkLabel(
                self.backup_unsynced_frame,
                text=t("backup.no_unsynced_apps"),
                text_color="#06d6a0",
            ).pack(pady=10)
            self._set_status(t("backup.scan_no_apps"))
            return

        # Risk color mapping
        risk_colors = {
            "critical": "#ef476f",
            "high": "#ffd166",
            "medium": "#06d6a0",
            "low": "#8d99ae",
            "unknown": "#a8a8a8",
        }
        risk_labels = {
            "critical": t("common.risk_critical"),
            "high": t("common.risk_high"),
            "medium": t("common.risk_medium"),
            "low": t("common.risk_low"),
            "unknown": "?",
        }

        # Header with select all / none
        hdr = ctk.CTkFrame(self.backup_unsynced_frame, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 4))

        total_label = ctk.CTkLabel(
            hdr,
            text=t("backup.apps_detected", count=len(detected)),
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        total_label.pack(side="left", padx=4)

        ctk.CTkButton(
            hdr, text=t("common.select_all"), width=60, height=24,
            fg_color="#06d6a0", hover_color="#05c090",
            command=lambda: self._toggle_all_unsynced(True),
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            hdr, text=t("common.select_none"), width=68, height=24,
            fg_color="#ef476f", hover_color="#d63a5e",
            command=lambda: self._toggle_all_unsynced(False),
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            hdr, text=t("common.select_critical"), width=90, height=24,
            fg_color="#ffd166", hover_color="#e6b84d", text_color="#1a1a2e",
            command=self._select_critical_unsynced,
        ).pack(side="right", padx=2)

        # Group by category
        prev_category = None
        for app in detected:
            if app.category != prev_category:
                prev_category = app.category
                cat_lbl = ctk.CTkLabel(
                    self.backup_unsynced_frame,
                    text=f"{app.icon} {app.category_name}",
                    font=ctk.CTkFont(size=12, weight="bold"),
                    text_color="#e94560",
                )
                cat_lbl.pack(anchor="w", padx=4, pady=(6, 2))

            row = ctk.CTkFrame(self.backup_unsynced_frame, fg_color="transparent", height=28)
            row.pack(fill="x", padx=4, pady=1)
            row.pack_propagate(False)

            # Pre-check critical items
            default_on = app.risk in ("critical", "high")
            var = ctk.BooleanVar(value=default_on)
            self.unsynced_app_vars[app.package] = var

            cb = ctk.CTkCheckBox(
                row,
                text=f"{app.app_name}",
                variable=var,
                font=ctk.CTkFont(size=12),
            )
            cb.pack(side="left", padx=(4, 8))

            # Risk badge
            risk_color = risk_colors.get(app.risk, "#a8a8a8")
            risk_text = risk_labels.get(app.risk, "?")
            ctk.CTkLabel(
                row,
                text=f" {risk_text} ",
                font=ctk.CTkFont(size=10, weight="bold"),
                text_color="#1a1a2e",
                fg_color=risk_color,
                corner_radius=4,
                width=60,
            ).pack(side="left", padx=4)

            # Version / data size
            extra = ""
            if app.version:
                extra += f"v{app.version}"
            if app.data_size_kb > 0:
                if extra:
                    extra += " · "
                extra += UnsyncedAppDetector._fmt_size(app.data_size_kb)

            if extra:
                ctk.CTkLabel(
                    row, text=extra,
                    text_color="#8d99ae",
                    font=ctk.CTkFont(size=10),
                ).pack(side="right", padx=8)

        # Count critical
        n_crit = sum(1 for a in detected if a.risk == "critical")
        n_high = sum(1 for a in detected if a.risk == "high")
        status = t("backup.apps_detected", count=len(detected))
        if n_crit:
            status += f" — ⚠️ {n_crit} {t('common.risk_critical')}"
        if n_high:
            status += f", {n_high} {t('common.risk_high')}"
        self._set_status(status)

    def _toggle_all_unsynced(self, state: bool):
        for var in self.unsynced_app_vars.values():
            var.set(state)

    def _select_critical_unsynced(self):
        """Select only critical + high risk apps."""
        crit_pkgs = {a.package for a in self._detected_apps if a.risk in ("critical", "high")}
        for pkg, var in self.unsynced_app_vars.items():
            var.set(pkg in crit_pkgs)

    # ==================================================================
    # RESTORE TAB
    # ==================================================================
    def _build_restore_tab(self):
        tab = self._tab_restore

        restore_scroll = ctk.CTkScrollableFrame(tab)
        restore_scroll.pack(fill="both", expand=True, padx=4, pady=4)

        # Backup selection
        sel_frame = ctk.CTkFrame(restore_scroll)
        sel_frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            sel_frame,
            text=t("restore.title"),
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        sel_row = ctk.CTkFrame(sel_frame, fg_color="transparent")
        sel_row.pack(fill="x", padx=12, pady=(0, 8))

        self.restore_backup_menu = ctk.CTkOptionMenu(
            sel_row, values=[t("restore.no_backup")],
            width=400,
        )
        self.restore_backup_menu.pack(side="left")

        ctk.CTkButton(
            sel_row, text=t("restore.btn_refresh"), width=120,
            command=self._refresh_backup_list,
        ).pack(side="left", padx=8)

        # Restore options
        opts_frame = ctk.CTkFrame(restore_scroll)
        opts_frame.pack(fill="x", padx=4, pady=(0, 4))

        self.restore_apps_var = ctk.BooleanVar(value=True)
        self.restore_files_var = ctk.BooleanVar(value=True)
        self.restore_data_var = ctk.BooleanVar(value=False)
        self.restore_messaging_var = ctk.BooleanVar(value=True)

        opts_inner = ctk.CTkFrame(opts_frame, fg_color="transparent")
        opts_inner.pack(fill="x", padx=12, pady=8)

        ctk.CTkCheckBox(
            opts_inner, text=t("restore.restore_apps"), variable=self.restore_apps_var,
        ).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ctk.CTkCheckBox(
            opts_inner, text=t("restore.restore_files"), variable=self.restore_files_var,
        ).grid(row=0, column=1, sticky="w", padx=8, pady=4)
        ctk.CTkCheckBox(
            opts_inner, text=t("restore.restore_messaging"),
            variable=self.restore_messaging_var,
        ).grid(row=0, column=2, sticky="w", padx=8, pady=4)
        ctk.CTkCheckBox(
            opts_inner, text=t("restore.restore_data"),
            variable=self.restore_data_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=4)

        # File tree browser for destination device preview
        tree_frame = ctk.CTkFrame(restore_scroll)
        tree_frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            tree_frame,
            text=t("restore.tree_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            tree_frame,
            text=t("restore.tree_desc"),
            font=ctk.CTkFont(size=11),
            text_color="#8d99ae",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        self.restore_tree = DeviceTreeBrowser(
            tree_frame,
            adb_core=self.adb,
            serial=None,
            root_path="/sdcard",
            height=180,
        )
        self.restore_tree.pack(fill="x", padx=8, pady=(0, 8))

        # Buttons
        btn_frame = ctk.CTkFrame(restore_scroll, fg_color="transparent")
        btn_frame.pack(fill="x", padx=4, pady=4)

        self.btn_start_restore = ctk.CTkButton(
            btn_frame,
            text=t("restore.btn_start"),
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["warning"],
            text_color="black",
            hover_color="#e6bc5a",
            height=42,
            command=self._start_restore,
        )
        self.btn_start_restore.pack(side="left", padx=8)

        self.btn_cancel_restore = ctk.CTkButton(
            btn_frame, text=t("restore.btn_cancel"), fg_color=COLORS["error"],
            height=42, state="disabled",
            command=self._cancel_restore,
        )
        self.btn_cancel_restore.pack(side="left", padx=8)

        # Progress
        progress_frame = ctk.CTkFrame(restore_scroll)
        progress_frame.pack(fill="x", padx=4, pady=4)

        self.restore_progress_label = ctk.CTkLabel(
            progress_frame, text=t("restore.progress_waiting"), anchor="w",
        )
        self.restore_progress_label.pack(fill="x", padx=12, pady=(8, 2))

        self.restore_progress_bar = ctk.CTkProgressBar(progress_frame)
        self.restore_progress_bar.pack(fill="x", padx=12, pady=(0, 4))
        self.restore_progress_bar.set(0)

        self.restore_progress_detail = ctk.CTkLabel(
            progress_frame, text="", anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
        )
        self.restore_progress_detail.pack(fill="x", padx=12, pady=(0, 8))

    # ==================================================================
    # TRANSFER TAB
    # ==================================================================
    def _build_transfer_tab(self):
        tab = self._tab_transfer

        transfer_scroll = ctk.CTkScrollableFrame(tab)
        transfer_scroll.pack(fill="both", expand=True, padx=4, pady=4)

        # Device selection
        dev_frame = ctk.CTkFrame(transfer_scroll)
        dev_frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            dev_frame,
            text=t("transfer.title"),
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        row_frame = ctk.CTkFrame(dev_frame, fg_color="transparent")
        row_frame.pack(fill="x", padx=12, pady=4)

        # Source
        ctk.CTkLabel(row_frame, text=t("transfer.label_source")).grid(row=0, column=0, sticky="w", padx=4)
        self.transfer_source_menu = ctk.CTkOptionMenu(
            row_frame, values=[t("transfer.no_device")], width=300,
            command=self._on_transfer_source_changed,
        )
        self.transfer_source_menu.grid(row=0, column=1, padx=8, pady=4)

        # Arrow
        ctk.CTkLabel(
            row_frame, text="  ➡️  ",
            font=ctk.CTkFont(size=20),
        ).grid(row=0, column=2, padx=4)

        # Target
        ctk.CTkLabel(row_frame, text=t("transfer.label_target")).grid(row=0, column=3, sticky="w", padx=4)
        self.transfer_target_menu = ctk.CTkOptionMenu(
            row_frame, values=[t("transfer.no_device")], width=300,
            command=self._on_transfer_target_changed,
        )
        self.transfer_target_menu.grid(row=0, column=4, padx=8, pady=4)

        # Transfer categories
        cats_frame = ctk.CTkFrame(transfer_scroll)
        cats_frame.pack(fill="x", padx=4, pady=(0, 4))

        ctk.CTkLabel(
            cats_frame,
            text=t("transfer.what_transfer"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        self.transfer_cat_vars: Dict[str, ctk.BooleanVar] = {}
        t_cats = [
            ("apps", t("transfer.category.apps")),
            ("photos", t("transfer.category.photos")),
            ("videos", t("transfer.category.videos")),
            ("music", t("transfer.category.music")),
            ("documents", t("transfer.category.documents")),
            ("contacts", t("transfer.category.contacts")),
            ("sms", t("transfer.category.sms")),
            ("messaging_apps", t("transfer.category.messaging_apps")),
        ]

        cats_inner = ctk.CTkFrame(cats_frame, fg_color="transparent")
        cats_inner.pack(fill="x", padx=12, pady=(0, 4))

        for i, (key, label) in enumerate(t_cats):
            var = ctk.BooleanVar(value=True if key != "messaging_apps" else False)
            self.transfer_cat_vars[key] = var
            ctk.CTkCheckBox(
                cats_inner, text=label, variable=var,
            ).grid(row=i // 4, column=i % 4, sticky="w", padx=8, pady=4)

        # --- Filter toggles (ignore cache / thumbnails) ----------------
        filter_frame = ctk.CTkFrame(cats_frame, fg_color="transparent")
        filter_frame.pack(fill="x", padx=12, pady=(4, 8))

        ctk.CTkLabel(
            filter_frame, text=t("transfer.filter_label"),
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left", padx=(0, 8))

        self.var_ignore_cache = ctk.BooleanVar(
            value=self.config.get("transfer.ignore_cache", True),
        )
        ctk.CTkCheckBox(
            filter_frame, text=t("transfer.filter_cache"),
            variable=self.var_ignore_cache,
        ).pack(side="left", padx=8)

        self.var_ignore_thumbnails = ctk.BooleanVar(
            value=self.config.get("transfer.ignore_thumbnails", True),
        )
        ctk.CTkCheckBox(
            filter_frame, text=t("transfer.filter_thumbs"),
            variable=self.var_ignore_thumbnails,
        ).pack(side="left", padx=8)

        # Messaging apps selection for transfer
        msg_transfer_frame = ctk.CTkFrame(transfer_scroll)
        msg_transfer_frame.pack(fill="x", padx=4, pady=4)

        msg_hdr = ctk.CTkFrame(msg_transfer_frame, fg_color="transparent")
        msg_hdr.pack(fill="x", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            msg_hdr,
            text=t("transfer.msg_transfer_title"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        ctk.CTkButton(
            msg_hdr, text=t("transfer.btn_detect"), width=90, height=28,
            command=self._detect_messaging_apps_transfer,
        ).pack(side="right", padx=4)

        self.transfer_msg_vars: Dict[str, ctk.BooleanVar] = {}
        self.transfer_msg_frame = ctk.CTkFrame(msg_transfer_frame, fg_color="transparent")
        self.transfer_msg_frame.pack(fill="x", padx=12, pady=(0, 8))
        self._build_messaging_checkboxes(self.transfer_msg_frame, self.transfer_msg_vars)

        # Unsynced apps selection for transfer
        unsync_transfer = ctk.CTkFrame(transfer_scroll)
        unsync_transfer.pack(fill="x", padx=4, pady=4)

        unsync_t_hdr = ctk.CTkFrame(unsync_transfer, fg_color="transparent")
        unsync_t_hdr.pack(fill="x", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            unsync_t_hdr,
            text=t("transfer.unsynced_title"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        self.btn_detect_unsynced_transfer = ctk.CTkButton(
            unsync_t_hdr,
            text=t("transfer.btn_detect_unsynced"),
            width=100,
            height=28,
            command=self._detect_unsynced_apps_transfer,
        )
        self.btn_detect_unsynced_transfer.pack(side="right", padx=4)

        self.transfer_unsynced_vars: Dict[str, ctk.BooleanVar] = {}
        self._transfer_detected_apps: List[DetectedApp] = []
        self.transfer_unsynced_frame = ctk.CTkScrollableFrame(
            unsync_transfer, height=120, fg_color="transparent",
        )
        self.transfer_unsynced_frame.pack(fill="x", padx=8, pady=(0, 8))

        ctk.CTkLabel(
            self.transfer_unsynced_frame,
            text=t("transfer.unsynced_placeholder"),
            text_color="#8d99ae",
            font=ctk.CTkFont(size=11),
        ).pack(pady=6)

        # Source device file tree
        src_tree_frame = ctk.CTkFrame(transfer_scroll)
        src_tree_frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            src_tree_frame,
            text=t("transfer.src_tree_title"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 2))

        ctk.CTkLabel(
            src_tree_frame,
            text=t("transfer.src_tree_desc"),
            font=ctk.CTkFont(size=11),
            text_color="#8d99ae",
        ).pack(anchor="w", padx=12, pady=(0, 4))

        self.transfer_src_tree = DeviceTreeBrowser(
            src_tree_frame,
            adb_core=self.adb,
            serial=None,
            root_path="/sdcard",
            height=160,
        )
        self.transfer_src_tree.pack(fill="x", padx=8, pady=(0, 8))

        # Destination device file tree
        dst_tree_frame = ctk.CTkFrame(transfer_scroll)
        dst_tree_frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            dst_tree_frame,
            text=t("transfer.dst_tree_title"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 2))

        ctk.CTkLabel(
            dst_tree_frame,
            text=t("transfer.dst_tree_desc"),
            font=ctk.CTkFont(size=11),
            text_color="#8d99ae",
        ).pack(anchor="w", padx=12, pady=(0, 4))

        self.transfer_dst_tree = DeviceTreeBrowser(
            dst_tree_frame,
            adb_core=self.adb,
            serial=None,
            root_path="/sdcard",
            height=160,
        )
        self.transfer_dst_tree.pack(fill="x", padx=8, pady=(0, 8))

        # Buttons
        btn_frame = ctk.CTkFrame(transfer_scroll, fg_color="transparent")
        btn_frame.pack(fill="x", padx=4, pady=4)

        self.btn_start_transfer = ctk.CTkButton(
            btn_frame,
            text=t("transfer.btn_start"),
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            height=42,
            command=self._start_transfer,
        )
        self.btn_start_transfer.pack(side="left", padx=8)

        self.btn_clone_device = ctk.CTkButton(
            btn_frame,
            text=t("transfer.btn_clone"),
            height=42,
            command=self._clone_device,
        )
        self.btn_clone_device.pack(side="left", padx=8)

        self.btn_cancel_transfer = ctk.CTkButton(
            btn_frame, text=t("transfer.btn_cancel"),
            fg_color=COLORS["error"], height=42, state="disabled",
            command=self._cancel_transfer,
        )
        self.btn_cancel_transfer.pack(side="left", padx=8)

        # Progress
        progress_frame = ctk.CTkFrame(transfer_scroll)
        progress_frame.pack(fill="x", padx=4, pady=4)

        self.transfer_progress_label = ctk.CTkLabel(
            progress_frame, text=t("transfer.progress_waiting"), anchor="w",
        )
        self.transfer_progress_label.pack(fill="x", padx=12, pady=(8, 2))

        self.transfer_progress_bar = ctk.CTkProgressBar(progress_frame)
        self.transfer_progress_bar.pack(fill="x", padx=12, pady=(0, 4))
        self.transfer_progress_bar.set(0)

        self.transfer_progress_detail = ctk.CTkLabel(
            progress_frame, text="", anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
        )
        self.transfer_progress_detail.pack(fill="x", padx=12, pady=(0, 8))

    # ==================================================================
    # DRIVERS TAB
    # ==================================================================
    def _build_drivers_tab(self):
        tab = self._tab_drivers

        info_frame = ctk.CTkFrame(tab)
        info_frame.pack(fill="x", padx=8, pady=8)

        ctk.CTkLabel(
            info_frame,
            text=t("drivers.title"),
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        self.driver_status_text = ctk.CTkTextbox(info_frame, height=150)
        self.driver_status_text.pack(fill="x", padx=12, pady=(0, 8))
        self.driver_status_text.insert("end", t("drivers.placeholder"))
        self.driver_status_text.configure(state="disabled")

        # --- Row 1: Check + Google + Universal + Auto ---
        btn_frame = ctk.CTkFrame(tab, fg_color="transparent")
        btn_frame.pack(fill="x", padx=8, pady=4)

        ctk.CTkButton(
            btn_frame, text=t("drivers.btn_check"), height=40,
            command=self._check_drivers,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text=t("drivers.btn_google"),
            fg_color=COLORS["success"], hover_color="#05c090", height=40,
            command=self._install_google_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text=t("drivers.btn_universal"),
            fg_color=COLORS["warning"], text_color="black",
            hover_color="#e6bc5a", height=40,
            command=self._install_universal_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text=t("drivers.btn_auto"),
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            height=40,
            command=self._auto_install_drivers,
        ).pack(side="left", padx=8)

        # --- Row 2: Chipset-specific drivers ---
        ctk.CTkLabel(
            tab, text=t("drivers.chipset_title"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(10, 2))

        chipset_frame = ctk.CTkFrame(tab, fg_color="transparent")
        chipset_frame.pack(fill="x", padx=8, pady=4)

        ctk.CTkButton(
            chipset_frame, text=t("drivers.btn_samsung"),
            fg_color="#1428A0", hover_color="#0D1B6E", height=40,
            command=self._install_samsung_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            chipset_frame, text=t("drivers.btn_qualcomm"),
            fg_color="#3253DC", hover_color="#263EA0", height=40,
            command=self._install_qualcomm_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            chipset_frame, text=t("drivers.btn_mediatek"),
            fg_color="#E3350D", hover_color="#B02A0A", height=40,
            command=self._install_mediatek_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            chipset_frame, text=t("drivers.btn_intel"),
            fg_color="#0068B5", hover_color="#004A80", height=40,
            command=self._install_intel_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            chipset_frame, text=t("drivers.btn_all_chipsets"),
            fg_color="#6B21A8", hover_color="#4C1D95", height=40,
            command=self._install_all_chipset_drivers,
        ).pack(side="left", padx=8)

        # --- Row 3: iOS / Apple drivers ---
        ctk.CTkLabel(
            tab, text=t("drivers.ios_title"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(10, 2))

        ios_frame = ctk.CTkFrame(tab, fg_color="transparent")
        ios_frame.pack(fill="x", padx=8, pady=4)

        ctk.CTkButton(
            ios_frame, text=t("drivers.btn_apple"),
            fg_color="#A2AAAD", hover_color="#8E9497", text_color="#000000",
            height=40,
            command=self._install_apple_drivers,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            ios_frame, text=t("drivers.btn_check_apple"),
            fg_color="#555555", hover_color="#444444",
            height=40,
            command=self._check_apple_drivers,
        ).pack(side="left", padx=8)

        # Driver install progress
        self.driver_progress_label = ctk.CTkLabel(
            tab, text="", anchor="w",
        )
        self.driver_progress_label.pack(fill="x", padx=20, pady=(8, 2))

        self.driver_progress_bar = ctk.CTkProgressBar(tab)
        self.driver_progress_bar.pack(fill="x", padx=20, pady=(0, 8))
        self.driver_progress_bar.set(0)

    # ==================================================================
    # SETTINGS TAB
    # ==================================================================
    def _build_settings_tab(self):
        tab = self._tab_settings

        settings_scroll = ctk.CTkScrollableFrame(tab)
        settings_scroll.pack(fill="both", expand=True, padx=4, pady=4)

        frame = ctk.CTkFrame(settings_scroll)
        frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            frame,
            text=t("settings.title"),
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        # ────── Language section ──────
        lang_header = ctk.CTkFrame(frame, fg_color="transparent")
        lang_header.pack(fill="x", padx=12, pady=(4, 4))
        ctk.CTkLabel(
            lang_header,
            text=f"🌐 {t('common.language')}",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w")

        lang_frame = ctk.CTkFrame(frame, fg_color="transparent")
        lang_frame.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(lang_frame, text=t("settings.language_label")).pack(side="left")

        langs = available_languages()
        lang_names = [f"{v} ({k})" for k, v in langs.items()]
        lang_codes = list(langs.keys())
        current = get_language()
        current_idx = lang_codes.index(current) if current in lang_codes else 0

        self.settings_lang_var = ctk.StringVar(value=lang_names[current_idx])
        self._lang_code_map = dict(zip(lang_names, lang_codes))

        ctk.CTkOptionMenu(
            lang_frame, values=lang_names,
            variable=self.settings_lang_var, width=220,
            command=self._on_language_changed,
        ).pack(side="left", padx=8)

        # ADB path
        adb_frame = ctk.CTkFrame(frame, fg_color="transparent")
        adb_frame.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(adb_frame, text=t("settings.label_adb_path")).pack(side="left")
        self.entry_adb_path = ctk.CTkEntry(adb_frame, width=400)
        self.entry_adb_path.pack(side="left", padx=8)
        if self.adb.adb_path:
            self.entry_adb_path.insert(0, self.adb.adb_path)
        ctk.CTkButton(
            adb_frame, text=t("settings.btn_browse_adb"), width=80,
            command=self._browse_adb,
        ).pack(side="left")

        # Backup directory
        bkp_frame = ctk.CTkFrame(frame, fg_color="transparent")
        bkp_frame.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(bkp_frame, text=t("settings.label_backup_dir")).pack(side="left")
        self.entry_backup_dir = ctk.CTkEntry(bkp_frame, width=400)
        self.entry_backup_dir.pack(side="left", padx=8)
        self.entry_backup_dir.insert(0, str(self.backup_mgr.backup_dir))
        ctk.CTkButton(
            bkp_frame, text=t("settings.btn_browse_adb"), width=80,
            command=self._browse_backup_dir,
        ).pack(side="left")

        # Auto-install drivers
        self.auto_driver_var = ctk.BooleanVar(
            value=self.config.get("drivers.auto_install", True)
        )
        ctk.CTkCheckBox(
            frame, text=t("settings.auto_install_drivers"),
            variable=self.auto_driver_var,
        ).pack(anchor="w", padx=12, pady=8)

        # ────── Acceleration section ──────
        accel_header = ctk.CTkFrame(frame, fg_color="transparent")
        accel_header.pack(fill="x", padx=12, pady=(16, 4))
        ctk.CTkLabel(
            accel_header,
            text=f"⚡ {t('settings.section_acceleration')}",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w")

        # GPU enabled checkbox
        self.settings_gpu_var = ctk.BooleanVar(
            value=self.config.get("acceleration.gpu_enabled", True),
        )
        ctk.CTkCheckBox(
            frame, text=t("settings.gpu_acceleration"),
            variable=self.settings_gpu_var,
        ).pack(anchor="w", padx=12, pady=4)

        # Multi-GPU
        self.settings_multigpu_var = ctk.BooleanVar(
            value=self.config.get("acceleration.multi_gpu", True),
        )
        ctk.CTkCheckBox(
            frame, text=t("settings.multi_gpu"),
            variable=self.settings_multigpu_var,
        ).pack(anchor="w", padx=28, pady=2)

        # NPU enabled checkbox
        self.settings_npu_var = ctk.BooleanVar(
            value=self.config.get("acceleration.npu_enabled", True),
        )
        ctk.CTkCheckBox(
            frame, text=t("settings.npu_acceleration"),
            variable=self.settings_npu_var,
        ).pack(anchor="w", padx=12, pady=4)

        # ────── Performance Presets ──────
        preset_header = ctk.CTkFrame(frame, fg_color="transparent")
        preset_header.pack(fill="x", padx=12, pady=(12, 4))
        ctk.CTkLabel(
            preset_header,
            text=f"🚀 {t('settings.section_performance')}",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w")

        preset_frame = ctk.CTkFrame(frame, fg_color="transparent")
        preset_frame.pack(fill="x", padx=12, pady=4)

        ctk.CTkButton(
            preset_frame,
            text=f"⚡ {t('settings.preset_max_performance')}",
            width=160,
            command=self._apply_preset_max_performance,
            fg_color="#c0392b",
            hover_color="#e74c3c",
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            preset_frame,
            text=f"⚖️ {t('settings.preset_balanced')}",
            width=140,
            command=self._apply_preset_balanced,
            fg_color="#2980b9",
            hover_color="#3498db",
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            preset_frame,
            text=f"🔋 {t('settings.preset_power_saver')}",
            width=140,
            command=self._apply_preset_power_saver,
            fg_color="#27ae60",
            hover_color="#2ecc71",
        ).pack(side="left", padx=4)

        # Current profile info
        self.lbl_perf_profile = ctk.CTkLabel(
            frame, text="",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
        )
        self.lbl_perf_profile.pack(anchor="w", padx=12, pady=(2, 4))
        self._refresh_perf_profile_label()

        # Checksum verification
        self.settings_verify_var = ctk.BooleanVar(
            value=self.config.get("acceleration.verify_checksums", True),
        )
        ctk.CTkCheckBox(
            frame, text=t("settings.verify_transfers"),
            variable=self.settings_verify_var,
        ).pack(anchor="w", padx=12, pady=4)

        # Checksum algo
        algo_frame = ctk.CTkFrame(frame, fg_color="transparent")
        algo_frame.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(algo_frame, text=t("settings.checksum_algo")).pack(side="left")
        self.settings_algo_var = ctk.StringVar(
            value=self.config.get("acceleration.checksum_algo", "md5"),
        )
        ctk.CTkOptionMenu(
            algo_frame, values=["md5", "sha1", "sha256"],
            variable=self.settings_algo_var, width=100,
        ).pack(side="left", padx=8)

        # Workers — auto-detect toggle + manual override
        auto_thr_frame = ctk.CTkFrame(frame, fg_color="transparent")
        auto_thr_frame.pack(fill="x", padx=12, pady=4)
        self.settings_auto_threads_var = ctk.BooleanVar(
            value=self.config.get("acceleration.auto_threads", True),
        )
        ctk.CTkCheckBox(
            auto_thr_frame,
            text=t("settings.auto_threads"),
            variable=self.settings_auto_threads_var,
            command=self._toggle_auto_threads,
        ).pack(side="left")

        # Dynamic info label
        dyn_pull, dyn_push = TransferAccelerator.compute_dynamic_workers()
        cores = os.cpu_count() or "?"
        self.lbl_auto_threads = ctk.CTkLabel(
            auto_thr_frame,
            text=f"  ({cores} cores → pull {dyn_pull} / push {dyn_push})",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["success"],
        )
        self.lbl_auto_threads.pack(side="left", padx=8)

        workers_frame = ctk.CTkFrame(frame, fg_color="transparent")
        workers_frame.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(workers_frame, text=t("settings.max_pull_workers")).pack(side="left")
        self.settings_pull_w = ctk.CTkEntry(workers_frame, width=50)
        self.settings_pull_w.pack(side="left", padx=4)
        self.settings_pull_w.insert(
            0, str(self.config.get("acceleration.max_pull_workers", 0) or dyn_pull),
        )
        ctk.CTkLabel(workers_frame, text="/").pack(side="left")
        self.settings_push_w = ctk.CTkEntry(workers_frame, width=50)
        self.settings_push_w.pack(side="left", padx=4)
        self.settings_push_w.insert(
            0, str(self.config.get("acceleration.max_push_workers", 0) or dyn_push),
        )
        # Apply initial auto-threads state
        self._toggle_auto_threads()

        # ────── Virtualization section ──────
        virt_header = ctk.CTkFrame(frame, fg_color="transparent")
        virt_header.pack(fill="x", padx=12, pady=(16, 4))
        ctk.CTkLabel(
            virt_header,
            text=f"🖥️ {t('settings.virtualization')}",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w")

        self.settings_virt_var = ctk.BooleanVar(
            value=self.config.get("virtualization.enabled", True),
        )
        ctk.CTkCheckBox(
            frame, text=t("settings.virtualization"),
            variable=self.settings_virt_var,
        ).pack(anchor="w", padx=12, pady=4)

        # GPU info label (populated async)
        self.lbl_settings_gpu_info = ctk.CTkLabel(
            frame, text=t("common.loading"),
            font=ctk.CTkFont(size=11), text_color=COLORS["text_dim"],
            justify="left", anchor="w",
        )
        self.lbl_settings_gpu_info.pack(anchor="w", padx=12, pady=(8, 4))

        threading.Thread(target=self._load_settings_gpu_info, daemon=True).start()

        # Download ADB
        ctk.CTkButton(
            frame, text=t("settings.download_adb"),
            command=self._download_platform_tools,
        ).pack(anchor="w", padx=12, pady=8)

        # ────── ADB no PATH ──────
        path_header = ctk.CTkFrame(frame, fg_color="transparent")
        path_header.pack(fill="x", padx=12, pady=(16, 4))
        ctk.CTkLabel(
            path_header,
            text=t("settings.adb_path_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w")

        self.lbl_path_status = ctk.CTkLabel(
            frame, text=t("common.loading"),
            font=ctk.CTkFont(size=11), text_color=COLORS["text_dim"],
            anchor="w",
        )
        self.lbl_path_status.pack(anchor="w", padx=12, pady=(2, 4))

        path_btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        path_btn_frame.pack(fill="x", padx=12, pady=4)

        self.btn_add_path = ctk.CTkButton(
            path_btn_frame, text=t("settings.btn_add_path"),
            width=240, command=self._add_adb_to_path,
        )
        self.btn_add_path.pack(side="left", padx=(0, 8))

        self.btn_remove_path = ctk.CTkButton(
            path_btn_frame, text=t("settings.btn_remove_path"),
            width=240, fg_color=COLORS["error"], hover_color="#d03050",
            command=self._remove_adb_from_path,
        )
        self.btn_remove_path.pack(side="left")

        # Populate PATH status async
        threading.Thread(target=self._refresh_path_status, daemon=True).start()

        # Save
        ctk.CTkButton(
            frame, text=t("settings.btn_save"),
            fg_color=COLORS["success"], hover_color="#05c090",
            command=self._save_settings,
        ).pack(anchor="w", padx=12, pady=12)

    # ==================================================================
    # Thread-safe helpers
    # ==================================================================
    def _safe_after(self, ms: int, func, *args):
        """Schedule func on the main thread, guarded against shutdown."""
        if self._closing:
            return
        try:
            self.after(ms, func, *args)
        except Exception:
            pass

    # ==================================================================
    # Device confirmation overlay
    # ==================================================================
    def _show_device_confirmation(self, title: str, message: str):
        """Show a prominent overlay telling the user to confirm on device.

        Called from worker threads — schedules UI work on the main thread.
        """
        def _build():
            if self._closing:
                return
            # Dismiss any existing overlay first
            self._dismiss_device_confirmation_ui()

            dlg = ctk.CTkToplevel(self)
            dlg.title(title)
            dlg.geometry("520x340")
            dlg.resizable(False, False)
            dlg.transient(self)
            dlg.attributes("-topmost", True)
            dlg.configure(fg_color=COLORS["bg"])
            dlg.protocol("WM_DELETE_WINDOW", lambda: None)  # Prevent closing

            # Pulsing phone icon
            self._confirm_icon_lbl = ctk.CTkLabel(
                dlg, text="📱", font=ctk.CTkFont(size=64),
            )
            self._confirm_icon_lbl.pack(pady=(24, 8))

            # Title
            ctk.CTkLabel(
                dlg, text=title,
                font=ctk.CTkFont(size=20, weight="bold"),
                text_color=COLORS["warning"],
            ).pack(pady=(0, 8))

            # Message
            ctk.CTkLabel(
                dlg, text=message,
                font=ctk.CTkFont(size=13),
                wraplength=460,
                justify="center",
            ).pack(padx=20, pady=(0, 12))

            # Animated waiting indicator
            self._confirm_dots_lbl = ctk.CTkLabel(
                dlg, text=t("devices.waiting_confirmation"),
                font=ctk.CTkFont(size=12),
                text_color=COLORS["text_dim"],
            )
            self._confirm_dots_lbl.pack(pady=(4, 8))

            # Pulsing progress bar (indeterminate feel)
            self._confirm_pulse_bar = ctk.CTkProgressBar(
                dlg, width=400, height=6,
                progress_color=COLORS["warning"],
            )
            self._confirm_pulse_bar.pack(pady=(0, 16))
            self._confirm_pulse_bar.set(0)

            self._confirm_dlg = dlg
            self._confirm_pulse_val = 0.0
            self._confirm_pulse_dir = 1
            self._animate_confirmation_pulse()

            # Also beep to attract attention
            try:
                self.bell()
            except Exception:
                pass

        self._safe_after(0, _build)

    def _animate_confirmation_pulse(self):
        """Animate the progress bar back and forth while waiting."""
        if self._closing:
            return
        if not hasattr(self, "_confirm_dlg") or self._confirm_dlg is None:
            return
        try:
            self._confirm_pulse_val += 0.02 * self._confirm_pulse_dir
            if self._confirm_pulse_val >= 1.0:
                self._confirm_pulse_dir = -1
            elif self._confirm_pulse_val <= 0.0:
                self._confirm_pulse_dir = 1
            self._confirm_pulse_bar.set(self._confirm_pulse_val)
            self._confirm_dlg.after(50, self._animate_confirmation_pulse)
        except Exception:
            pass

    def _dismiss_device_confirmation(self):
        """Hide the device confirmation overlay. Called from worker threads."""
        self._safe_after(0, self._dismiss_device_confirmation_ui)

    def _dismiss_device_confirmation_ui(self):
        """Destroy the overlay on the main thread."""
        if hasattr(self, "_confirm_dlg") and self._confirm_dlg is not None:
            try:
                self._confirm_dlg.destroy()
            except Exception:
                pass
            self._confirm_dlg = None

    # ==================================================================
    # Device management
    # ==================================================================
    def _start_device_monitor(self):
        """Start polling for device changes."""
        if self._closing:
            return
        self.adb.register_device_callback(self._on_device_event)
        self.adb.start_device_monitor()
        # Initial refresh
        self._safe_after(500, self._refresh_devices)

    def _on_device_event(self, event: str, device: DeviceInfo):
        """Called from monitor thread on device events."""
        if self._closing:
            return
        self._safe_after(100, self._refresh_devices)
        if event == "connected":
            self._safe_after(200, lambda: self._set_status(
                t("devices.device_connected", name=device.friendly_name())
            ))
            # Auto-install drivers if configured (skip if already running)
            if (
                self.config.get("drivers.auto_install", True)
                and os.name == "nt"
                and not self._driver_install_running
            ):
                self._safe_after(2000, self._auto_install_drivers)

    def _refresh_devices(self):
        """Refresh the device list in the UI."""
        try:
            devices_list = self.adb.list_devices()
            self.devices = {d.serial: d for d in devices_list}
        except Exception as exc:
            log.warning("Failed to list devices: %s", exc)
            self.devices = {}

        # Also discover iOS devices via DeviceManager
        try:
            all_unified = self.device_mgr.list_all_devices()
            self.unified_devices = {d.serial: d for d in all_unified}
        except Exception as exc:
            log.debug("Unified device scan: %s", exc)
            self.unified_devices = {}

        # Count total (Android + iOS)
        android_count = len(self.devices)
        ios_count = sum(
            1 for d in self.unified_devices.values()
            if d.platform == DevicePlatform.IOS
        )
        total_count = android_count + ios_count

        # Update top bar
        if total_count == 0:
            self.lbl_connection.configure(
                text=t("devices.connection_none"),
                text_color=COLORS["text_dim"],
            )
        elif total_count == 1:
            if self.devices:
                dev = list(self.devices.values())[0]
                label = f"✅ 🤖 {dev.friendly_name()} ({dev.state})"
            else:
                dev_u = [d for d in self.unified_devices.values() if d.platform == DevicePlatform.IOS][0]
                label = f"✅ 🍎 {dev_u.friendly_name()}"
            self.lbl_connection.configure(text=label, text_color=COLORS["success"])
        else:
            parts = []
            if android_count:
                parts.append(f"🤖{android_count}")
            if ios_count:
                parts.append(f"🍎{ios_count}")
            self.lbl_connection.configure(
                text=t("devices.connection_multiple", count=total_count, parts=' + '.join(parts)),
                text_color=COLORS["success"],
            )

        # Update device list tab (quick — basic info from adb devices -l)
        for w in self.device_list_frame.winfo_children():
            w.destroy()

        if not self.devices and not ios_count:
            self.lbl_no_devices = ctk.CTkLabel(
                self.device_list_frame,
                text=t("devices.no_device_instructions_ios"),
                font=ctk.CTkFont(size=13),
                text_color=COLORS["text_dim"],
                justify="center",
            )
            self.lbl_no_devices.pack(expand=True, pady=40)
        else:
            # Android devices
            for serial, dev in self.devices.items():
                self._create_device_card(serial, dev)
            # iOS devices
            for serial, udev in self.unified_devices.items():
                if udev.platform == DevicePlatform.IOS:
                    self._create_ios_device_card(serial, udev)

        # Update transfer device menus (preserves user selection)
        self._update_transfer_menus()

        # Update backup/restore tree browsers with first available device serial
        first_serial = list(self.devices.keys())[0] if self.devices else None
        self.backup_tree.set_serial(first_serial)
        self.restore_tree.set_serial(first_serial)

        # Fetch full details (manufacturer, storage) in background threads
        if self.devices:
            self._enrich_devices_async(list(self.devices.keys()))

    def _enrich_devices_async(self, serials: List[str]):
        """Fetch get_device_details for each serial in parallel, then update UI."""
        def _fetch_all():
            results: Dict[str, DeviceInfo] = {}
            with ThreadPoolExecutor(max_workers=min(4, len(serials))) as pool:
                futures = {
                    pool.submit(self.adb.get_device_details, s): s
                    for s in serials
                }
                for fut in as_completed(futures):
                    serial = futures[fut]
                    try:
                        results[serial] = fut.result()
                    except Exception as exc:
                        log.warning("Failed to enrich %s: %s", serial, exc)
            # Update on main thread
            self._safe_after(0, lambda: self._apply_enriched_devices(results))

        threading.Thread(target=_fetch_all, daemon=True).start()

    def _apply_enriched_devices(self, enriched: Dict[str, DeviceInfo]):
        """Merge enriched details into self.devices and refresh cards."""
        if self._closing:
            return
        for serial, dev in enriched.items():
            if serial in self.devices:
                self.devices[serial] = dev

        # Rebuild device cards with full info
        for w in self.device_list_frame.winfo_children():
            w.destroy()
        for serial, dev in self.devices.items():
            self._create_device_card(serial, dev)
        for serial, udev in self.unified_devices.items():
            if udev.platform == DevicePlatform.IOS:
                self._create_ios_device_card(serial, udev)

        # Refresh transfer menus with enriched names
        self._update_transfer_menus()

    def _create_device_card(self, serial: str, dev: DeviceInfo):
        """Create a card widget for a device."""
        card = ctk.CTkFrame(self.device_list_frame, corner_radius=8)
        card.pack(fill="x", padx=4, pady=4)

        name = dev.friendly_name()
        state_color = COLORS["success"] if dev.state == "device" else COLORS["warning"]

        # ---- Row 1: Name + state + serial ----
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(8, 2))

        ctk.CTkLabel(
            top, text=f"📱 {name}",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")

        ctk.CTkLabel(
            top, text=f"  [{dev.state}]",
            text_color=state_color,
            font=ctk.CTkFont(size=12),
        ).pack(side="left")

        ctk.CTkLabel(
            top, text=serial,
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=11),
        ).pack(side="right")

        # ---- Row 2: Detailed info (brand, model, storage) ----
        info_row = ctk.CTkFrame(card, fg_color="transparent")
        info_row.pack(fill="x", padx=8, pady=(0, 4))

        details_parts: List[str] = []
        if dev.manufacturer:
            details_parts.append(f"{t('devices.brand')}: {dev.manufacturer}")
        if dev.model:
            details_parts.append(f"{t('devices.model')}: {dev.model}")
        if dev.android_version:
            details_parts.append(f"Android {dev.android_version}")

        storage_text = dev.storage_summary()
        if storage_text:
            details_parts.append(f"💾 {storage_text}")

        if details_parts:
            ctk.CTkLabel(
                info_row,
                text="  •  ".join(details_parts),
                font=ctk.CTkFont(size=11),
                text_color=COLORS["text_dim"],
            ).pack(side="left")

        # ---- Row 3: Buttons ----
        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=8, pady=(0, 8))

        ctk.CTkButton(
            btn_row, text=t("devices.btn_details"), width=100,
            command=lambda s=serial: self._show_device_details(s),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            btn_row, text=t("devices.btn_backup"), width=100,
            fg_color=COLORS["success"], hover_color="#05c090",
            command=lambda s=serial: self._quick_backup(s),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            btn_row, text=t("devices.btn_reboot"), width=100,
            fg_color=COLORS["warning"], text_color="black",
            command=lambda s=serial: self._reboot_device(s),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            btn_row, text=t("devices.btn_clear_cache"), width=120,
            fg_color="#7b2cbf", hover_color="#9d4edd",
            command=lambda s=serial: self._open_cache_manager(s),
        ).pack(side="left", padx=4)

    def _create_ios_device_card(self, serial: str, dev: UnifiedDeviceInfo):
        """Create a card widget for an iOS device."""
        card = ctk.CTkFrame(self.device_list_frame, corner_radius=8)
        card.pack(fill="x", padx=4, pady=4)

        name = dev.friendly_name()
        state_color = COLORS["success"]

        # ---- Row 1: Name + serial ----
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(8, 2))

        ctk.CTkLabel(
            top, text=f"🍎 {name}",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")

        ctk.CTkLabel(
            top, text=f"  [iOS {dev.os_version}]",
            text_color=state_color,
            font=ctk.CTkFont(size=12),
        ).pack(side="left")

        ctk.CTkLabel(
            top, text=serial[:16] + "…" if len(serial) > 16 else serial,
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=11),
        ).pack(side="right")

        # ---- Row 2: Info ----
        info_row = ctk.CTkFrame(card, fg_color="transparent")
        info_row.pack(fill="x", padx=8, pady=(0, 4))

        details_parts: List[str] = ["Apple"]
        if dev.model:
            details_parts.append(dev.model)
        if dev.device_class:
            details_parts.append(dev.device_class)
        stor = dev.storage_summary()
        if stor:
            details_parts.append(f"💾 {stor}")

        ctk.CTkLabel(
            info_row,
            text="  •  ".join(details_parts),
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
        ).pack(side="left")

        # ---- Row 3: Info note ----
        ctk.CTkLabel(
            card,
            text=t("devices.ios_cross_platform"),
            font=ctk.CTkFont(size=10),
            text_color=COLORS["text_dim"],
        ).pack(padx=8, pady=(0, 8), anchor="w")

    def _show_device_details(self, serial: str):
        """Show detailed device info."""
        self._set_status(t("devices.loading_details", serial=serial))

        def _fetch():
            dev = self.adb.get_device_details(serial)
            self.after(0, lambda: self._display_device_details(dev))

        threading.Thread(target=_fetch, daemon=True).start()

    def _display_device_details(self, dev: DeviceInfo):
        self.device_details_text.configure(state="normal")
        self.device_details_text.delete("1.0", "end")
        text = (
            f"{t('devices.detail_manufacturer')}:     {dev.manufacturer}\n"
            f"{t('devices.detail_model')}:         {dev.model}\n"
            f"{t('devices.detail_product')}:        {dev.product}\n"
            f"Android:        {dev.android_version} (SDK {dev.sdk_version})\n"
            f"Serial:         {dev.serial}\n"
            f"{t('devices.detail_battery')}:        {dev.battery_level}%\n"
            f"{t('devices.detail_storage')}:  {format_bytes(dev.storage_free)} {t('common.free')} "
            f"/ {format_bytes(dev.storage_total)} {t('common.total')}"
        )
        self.device_details_text.insert("end", text)
        self.device_details_text.configure(state="disabled")
        self._set_status(t("devices.details_loaded"))

    def _reboot_device(self, serial: str):
        if messagebox.askyesno(t("devices.reboot_title"), t("devices.reboot_confirm", serial=serial)):
            self.adb.reboot("", serial)
            self._set_status(t("devices.rebooting", serial=serial))

    def _quick_backup(self, serial: str):
        self.selected_device = serial
        self.tabview.set(t("tabs.backup"))

    # ==================================================================
    # Cache management
    # ==================================================================
    def _open_cache_manager(self, serial: str):
        """Open a dialog to manage app cache for the device."""
        dialog = ctk.CTkToplevel(self)
        dialog.title(t("cache.title"))
        dialog.geometry("650x520")
        dialog.transient(self)
        dialog.grab_set()
        dialog.attributes("-topmost", True)

        dev = self.devices.get(serial)
        dev_name = dev.friendly_name() if dev else serial

        # Header
        hdr = ctk.CTkFrame(dialog, fg_color="transparent")
        hdr.pack(fill="x", padx=16, pady=(16, 8))

        ctk.CTkLabel(
            hdr, text=t("cache.header", name=dev_name),
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w")

        self._cache_status_label = ctk.CTkLabel(
            hdr, text=t("cache.scanning"),
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=12),
        )
        self._cache_status_label.pack(anchor="w", pady=(4, 0))

        # Buttons row
        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(4, 4))

        btn_clear_all = ctk.CTkButton(
            btn_frame, text=t("cache.btn_clear_all"),
            fg_color=COLORS["error"], hover_color="#d63a5e",
            height=36, width=220,
            command=lambda: self._clear_all_cache(serial, dialog),
        )
        btn_clear_all.pack(side="left", padx=4)

        btn_clear_sel = ctk.CTkButton(
            btn_frame, text=t("cache.btn_clear_selected"),
            fg_color="#7b2cbf", hover_color="#9d4edd",
            height=36, width=200,
            command=lambda: self._clear_selected_cache(serial, dialog),
        )
        btn_clear_sel.pack(side="left", padx=4)

        btn_refresh = ctk.CTkButton(
            btn_frame, text="🔄", width=36, height=36,
            command=lambda: self._scan_cache(serial, scroll_frame, dialog),
        )
        btn_refresh.pack(side="right", padx=4)

        # Select All / None
        sel_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        sel_frame.pack(fill="x", padx=16, pady=(0, 4))

        ctk.CTkButton(
            sel_frame, text=t("common.select_all"), width=70, height=28,
            fg_color="#06d6a0", hover_color="#05c090",
            command=lambda: self._cache_select_toggle(True),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            sel_frame, text=t("common.select_none"), width=80, height=28,
            fg_color="#ef476f", hover_color="#d63a5e",
            command=lambda: self._cache_select_toggle(False),
        ).pack(side="left", padx=4)

        # Scrollable app list
        scroll_frame = ctk.CTkScrollableFrame(dialog, height=320)
        scroll_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        self._cache_check_vars: Dict[str, ctk.BooleanVar] = {}
        self._cache_scroll_frame = scroll_frame

        # Start scanning in background
        self._scan_cache(serial, scroll_frame, dialog)

    def _scan_cache(self, serial: str, scroll_frame, dialog):
        """Scan app cache sizes in background."""
        self._cache_status_label.configure(text=t("cache.scanning"))

        for w in scroll_frame.winfo_children():
            w.destroy()
        self._cache_check_vars.clear()

        ctk.CTkLabel(
            scroll_frame, text=t("cache.loading"),
            text_color=COLORS["text_dim"],
        ).pack(pady=20)

        def _run():
            try:
                packages = self.adb.list_packages(serial, third_party=True)
                # Get storage info for each package using dumpsys package
                app_info_list = []
                for pkg in packages:
                    out = self.adb.run_shell(
                        f'dumpsys package {pkg} 2>/dev/null | grep -iE "codePath|dataDir|versionName"',
                        serial, timeout=5,
                    )
                    app_info_list.append({"package": pkg, "info": out})

                # Get cache size estimates using du on accessible cache dirs
                cache_data = []
                for item in app_info_list:
                    pkg = item["package"]
                    # Try to get cache size from accessible locations
                    size_out = self.adb.run_shell(
                        f'du -sk /data/data/{pkg}/cache /data/data/{pkg}/code_cache '
                        f'/data/user/0/{pkg}/cache /data/user/0/{pkg}/code_cache '
                        f'/sdcard/Android/data/{pkg}/cache 2>/dev/null | '
                        f'awk \'{{s+=$1}} END {{print s}}\'',
                        serial, timeout=5,
                    )
                    try:
                        cache_kb = int(size_out.strip()) if size_out.strip() else 0
                    except ValueError:
                        cache_kb = 0

                    cache_data.append({
                        "package": pkg,
                        "cache_bytes": cache_kb * 1024,
                    })

                # Sort by cache size descending
                cache_data.sort(key=lambda x: x["cache_bytes"], reverse=True)
                self.after(0, lambda: self._render_cache_list(cache_data, scroll_frame))
            except Exception as exc:
                log.warning("Cache scan error: %s", exc)
                self.after(0, lambda: self._cache_status_label.configure(
                    text=t("cache.scan_error", error=exc)
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _render_cache_list(self, cache_data: List[Dict], scroll_frame):
        """Render the cache app list in the dialog."""
        for w in scroll_frame.winfo_children():
            w.destroy()
        self._cache_check_vars.clear()

        total_cache = sum(d["cache_bytes"] for d in cache_data)
        apps_with_cache = sum(1 for d in cache_data if d["cache_bytes"] > 0)

        self._cache_status_label.configure(
            text=t("cache.status", total=len(cache_data), with_cache=apps_with_cache, size=format_bytes(total_cache))
        )

        if not cache_data:
            ctk.CTkLabel(
                scroll_frame, text=t("cache.no_apps"),
                text_color=COLORS["text_dim"],
            ).pack(pady=20)
            return

        for item in cache_data:
            pkg = item["package"]
            cache_bytes = item["cache_bytes"]

            row = ctk.CTkFrame(scroll_frame, fg_color="transparent", height=32)
            row.pack(fill="x", padx=4, pady=1)
            row.pack_propagate(False)

            var = ctk.BooleanVar(value=cache_bytes > 0)
            self._cache_check_vars[pkg] = var

            ctk.CTkCheckBox(
                row, text="", variable=var, width=24, height=24,
                checkbox_width=20, checkbox_height=20,
            ).pack(side="left", padx=(4, 2))

            # Package name
            ctk.CTkLabel(
                row, text=pkg,
                font=ctk.CTkFont(size=12),
                anchor="w",
            ).pack(side="left", fill="x", expand=True, padx=4)

            # Cache size badge
            if cache_bytes > 0:
                size_text = format_bytes(cache_bytes)
                badge_color = "#ef476f" if cache_bytes > 10_000_000 else (
                    "#ffd166" if cache_bytes > 1_000_000 else "#8d99ae"
                )
            else:
                size_text = "—"
                badge_color = "#4a4a5a"

            ctk.CTkLabel(
                row, text=size_text,
                font=ctk.CTkFont(size=11),
                text_color=badge_color,
                width=80, anchor="e",
            ).pack(side="right", padx=8)

    def _cache_select_toggle(self, select: bool):
        """Select or deselect all cache checkboxes."""
        for var in self._cache_check_vars.values():
            var.set(select)

    def _clear_all_cache(self, serial: str, dialog):
        """Clear all app caches using pm trim-caches."""
        if not messagebox.askyesno(
            t("cache.title"),
            t("cache.confirm_all"),
            parent=dialog,
        ):
            return

        self._cache_status_label.configure(text=t("cache.clearing_all"))

        def _run():
            try:
                success = self.adb.clear_all_cache(serial)
                if success:
                    msg = t("cache.all_cleared")
                else:
                    msg = t("cache.may_need_root")
                self.after(0, lambda: self._cache_status_label.configure(text=msg))
                # Refresh the list
                self.after(500, lambda: self._scan_cache(
                    serial, self._cache_scroll_frame, dialog,
                ))
            except Exception as exc:
                self.after(0, lambda: self._cache_status_label.configure(
                    text=f"{t('common.error')}: {exc}"
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _clear_selected_cache(self, serial: str, dialog):
        """Clear cache for selected apps."""
        selected = [pkg for pkg, var in self._cache_check_vars.items() if var.get()]
        if not selected:
            messagebox.showinfo(t("cache.title"), t("cache.no_selection"), parent=dialog)
            return

        if not messagebox.askyesno(
            t("cache.title"),
            t("cache.confirm_selected", count=len(selected)),
            parent=dialog,
        ):
            return

        self._cache_status_label.configure(
            text=t("cache.clearing_selected", count=len(selected))
        )

        def _run():
            try:
                cleared = 0
                for i, pkg in enumerate(selected):
                    self.adb.clear_app_cache(pkg, serial)
                    cleared += 1
                    if i % 5 == 0:
                        self.after(0, lambda c=cleared, total=len(selected):
                            self._cache_status_label.configure(
                                text=t("cache.clearing_progress", done=c, total=total)
                            )
                        )

                self.after(0, lambda: self._cache_status_label.configure(
                    text=t("cache.selected_cleared", count=cleared)
                ))
                # Refresh the list
                self.after(500, lambda: self._scan_cache(
                    serial, self._cache_scroll_frame, dialog,
                ))
            except Exception as exc:
                self.after(0, lambda: self._cache_status_label.configure(
                    text=f"{t('common.error')}: {exc}"
                ))

        threading.Thread(target=_run, daemon=True).start()

    # ==================================================================
    # UI locking — prevent interaction during long-running operations
    # ==================================================================
    def _lock_ui(self):
        """Disable all action buttons and tab switching during an operation."""
        self._ui_locked = True
        # Disable all start / action buttons
        for btn in self._lockable_buttons():
            try:
                btn.configure(state="disabled")
            except Exception:
                pass
        # Disable the tab bar segmented button (prevents tab switching)
        try:
            self.tabview._segmented_button.configure(state="disabled")
        except Exception:
            pass

    def _unlock_ui(self):
        """Re-enable all action buttons and tab switching."""
        self._ui_locked = False
        for btn in self._lockable_buttons():
            try:
                btn.configure(state="normal")
            except Exception:
                pass
        # Re-enable tab bar
        try:
            self.tabview._segmented_button.configure(state="normal")
        except Exception:
            pass
        # Cancel buttons back to disabled (no operation running)
        for cbtn in (
            self.btn_cancel_backup,
            self.btn_cancel_restore,
            self.btn_cancel_transfer,
        ):
            try:
                cbtn.configure(state="disabled")
            except Exception:
                pass

    def _lockable_buttons(self):
        """Return the list of buttons that should be disabled during operations."""
        btns = [
            self.btn_start_backup,
            self.btn_start_restore,
            self.btn_start_transfer,
            self.btn_clone_device,
            self.btn_refresh,
        ]
        if hasattr(self, "btn_scan_cleanup"):
            btns.append(self.btn_scan_cleanup)
        if hasattr(self, "btn_execute_cleanup"):
            btns.append(self.btn_execute_cleanup)
        # Toolbox buttons
        for attr in (
            "btn_tb_device_info", "btn_tb_battery", "btn_tb_storage",
            "btn_tb_network", "btn_tb_list_apps", "btn_tb_clear_all_cache",
            "btn_tb_force_stop_all", "btn_tb_screenshot", "btn_tb_screenrecord",
            "btn_tb_logcat",
        ):
            if hasattr(self, attr):
                btns.append(getattr(self, attr))
        return btns

    # ==================================================================
    # Backup operations
    # ==================================================================
    def _start_backup(self):
        serial = self._get_selected_device()
        if not serial:
            return

        # Update tree serial before backup
        self.backup_tree.set_serial(serial)

        self._lock_ui()
        self.btn_cancel_backup.configure(state="normal")

        backup_type = self.backup_type_var.get()

        # Gather messaging app selections
        selected_msg_keys = [
            k for k, v in self.messaging_app_vars.items() if v.get()
        ]

        # Gather custom tree paths
        custom_paths = self.backup_tree.get_selected_paths()

        # Gather unsynced apps selections
        selected_unsynced_pkgs = [
            pkg for pkg, v in self.unsynced_app_vars.items() if v.get()
        ]

        def _run():
            try:
                self.backup_mgr.set_progress_callback(self._on_backup_progress)

                if backup_type == "full":
                    self.backup_mgr.backup_full(serial)
                elif backup_type == "custom":
                    # Custom mode: use tree-selected paths + messaging apps
                    if custom_paths:
                        self.backup_mgr.backup_custom_paths(serial, custom_paths)
                    if selected_msg_keys:
                        self.backup_mgr.backup_messaging_apps(
                            serial, app_keys=selected_msg_keys
                        )
                    if selected_unsynced_pkgs:
                        self.backup_mgr.backup_unsynced_apps(
                            serial, packages=selected_unsynced_pkgs,
                        )
                    if not custom_paths and not selected_msg_keys and not selected_unsynced_pkgs:
                        self.after(0, lambda: messagebox.showwarning(
                            "Backup",
                            t("backup.select_items_msg"),
                        ))
                        return
                else:
                    # Selective mode
                    cats = [k for k, v in self.backup_cat_vars.items() if v.get()]
                    file_cats = [c for c in cats if c in MEDIA_PATHS]
                    special = [c for c in cats if c not in MEDIA_PATHS and c != "apps"]

                    if file_cats:
                        self.backup_mgr.backup_files(serial, file_cats)
                    if "apps" in cats:
                        self.backup_mgr.backup_apps(serial)
                    if "contacts" in special:
                        self.backup_mgr.backup_contacts(serial)
                    if "sms" in special:
                        self.backup_mgr.backup_sms(serial)

                    # Messaging apps (if any selected)
                    if selected_msg_keys:
                        self.backup_mgr.backup_messaging_apps(
                            serial, app_keys=selected_msg_keys
                        )

                    # Unsynced apps
                    if selected_unsynced_pkgs:
                        self.backup_mgr.backup_unsynced_apps(
                            serial, packages=selected_unsynced_pkgs,
                        )

                    # Custom tree paths (even in selective mode)
                    if custom_paths:
                        self.backup_mgr.backup_custom_paths(serial, custom_paths)

                self.after(0, lambda: messagebox.showinfo("Backup", t("backup.completed")))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror(t("common.error"), str(exc)))
            finally:
                self.after(0, self._backup_finished)

        threading.Thread(target=_run, daemon=True).start()

    def _on_backup_progress(self, p: BackupProgress):
        def _update():
            if self._closing:
                return
            try:
                phase_text = p.phase
                if p.sub_phase:
                    phase_text += f" ({p.sub_phase})"
                self.backup_progress_label.configure(text=f"{phase_text}: {p.current_item}")
                self.backup_progress_bar.set(p.percent / 100)
                detail_parts = []
                if p.items_total:
                    detail_parts.append(f"{p.items_done}/{p.items_total} {t('common.items')}")
                if p.bytes_total:
                    detail_parts.append(f"{format_bytes(p.bytes_done)}/{format_bytes(p.bytes_total)}")
                if p.elapsed_seconds > 0:
                    detail_parts.append(f"{t('common.time')}: {format_duration(p.elapsed_seconds)}")
                if p.eta_seconds and p.eta_seconds > 0:
                    detail_parts.append(f"ETA: {format_duration(p.eta_seconds)}")
                if p.errors:
                    detail_parts.append(f"{t('common.errors')}: {len(p.errors)}")
                self.backup_progress_detail.configure(text="  |  ".join(detail_parts))
            except Exception:
                pass
        self._safe_after(0, _update)

    def _cancel_backup(self):
        self.backup_mgr.cancel()
        self.transfer_mgr.cancel()
        self._set_status(t("backup.cancelled"))

    def _backup_finished(self):
        self._unlock_ui()
        self._refresh_backup_list()

    def _open_backup_folder(self):
        open_folder(str(self.backup_mgr.backup_dir))

    def _refresh_backup_list(self):
        """Refresh the backup list in both backup and restore tabs."""
        backups = self.backup_mgr.list_backups()

        # Clear backup list frame
        for w in self.backup_list_frame.winfo_children():
            w.destroy()

        if not backups:
            ctk.CTkLabel(
                self.backup_list_frame,
                text=t("backup.no_backup"),
                text_color=COLORS["text_dim"],
            ).pack(pady=20)
        else:
            for m in backups:
                self._create_backup_card(m)

        # Update restore dropdown
        values = [
            f"{m.backup_id} ({m.backup_type} - {format_bytes(m.size_bytes)})"
            for m in backups
        ]
        if values:
            self.restore_backup_menu.configure(values=values)
            self.restore_backup_menu.set(values[0])
        else:
            self.restore_backup_menu.configure(values=[t("restore.no_backup")])

    def _create_backup_card(self, m):
        card = ctk.CTkFrame(self.backup_list_frame, corner_radius=8)
        card.pack(fill="x", padx=4, pady=3)

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=6)

        type_icons = {
            "full": "🗄️", "files": "📁", "apps": "📦",
            "contacts": "👤", "sms": "💬", "messaging": "💬",
            "custom": "🌳",
        }
        icon = type_icons.get(m.backup_type, "💾")

        ctk.CTkLabel(
            row, text=f"{icon} {m.backup_id}",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left")

        info = f"{format_bytes(m.size_bytes)} | {m.timestamp[:10]}"
        if m.file_count:
            info += f" | {m.file_count} {t('common.files')}"
        if m.app_count:
            info += f" | {m.app_count} apps"

        ctk.CTkLabel(
            row, text=info,
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=12)

        ctk.CTkButton(
            row, text="🗑️", width=32,
            fg_color=COLORS["error"],
            command=lambda bid=m.backup_id: self._delete_backup(bid),
        ).pack(side="right", padx=2)

    def _delete_backup(self, backup_id: str):
        if messagebox.askyesno(t("backup.delete_title"), t("backup.delete_confirm", id=backup_id)):
            self.backup_mgr.delete_backup(backup_id)
            self._refresh_backup_list()

    # ==================================================================
    # Restore operations
    # ==================================================================
    def _start_restore(self):
        serial = self._get_selected_device()
        if not serial:
            return

        selected = self.restore_backup_menu.get()
        if "Nenhum" in selected or t("restore.no_backup") in selected:
            messagebox.showwarning(t("restore.title"), t("restore.no_selection"))
            return

        backup_id = selected.split(" (")[0]

        if not messagebox.askyesno(
            t("restore.confirm_title"),
            t("restore.confirm_msg", id=backup_id, serial=serial),
        ):
            return

        self._lock_ui()
        self.btn_cancel_restore.configure(state="normal")

        def _run():
            try:
                self.restore_mgr.set_progress_callback(self._on_restore_progress)
                self.restore_mgr.restore_smart(serial, backup_id)
                self.after(0, lambda: messagebox.showinfo(
                    t("restore.title"), t("restore.completed")
                ))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror(t("common.error"), str(exc)))
            finally:
                self.after(0, self._restore_finished)

        threading.Thread(target=_run, daemon=True).start()

    def _on_restore_progress(self, p: BackupProgress):
        def _update():
            if self._closing:
                return
            try:
                phase_text = p.phase
                if p.sub_phase:
                    phase_text += f" ({p.sub_phase})"
                self.restore_progress_label.configure(text=f"{phase_text}: {p.current_item}")
                self.restore_progress_bar.set(p.percent / 100)
                detail_parts = []
                if p.items_total:
                    detail_parts.append(f"{p.items_done}/{p.items_total} {t('common.items')}")
                if p.bytes_total:
                    detail_parts.append(f"{format_bytes(p.bytes_done)}/{format_bytes(p.bytes_total)}")
                if p.elapsed_seconds > 0:
                    detail_parts.append(f"{t('common.time')}: {format_duration(p.elapsed_seconds)}")
                if p.eta_seconds and p.eta_seconds > 0:
                    detail_parts.append(f"ETA: {format_duration(p.eta_seconds)}")
                if p.errors:
                    detail_parts.append(f"{t('common.errors')}: {len(p.errors)}")
                self.restore_progress_detail.configure(text="  |  ".join(detail_parts))
            except Exception:
                pass
        self._safe_after(0, _update)

    def _cancel_restore(self):
        self.restore_mgr.cancel()
        self.transfer_mgr.cancel()
        self._set_status(t("restore.cancelled"))

    def _restore_finished(self):
        self._unlock_ui()

    # ==================================================================
    # Transfer operations
    # ==================================================================
    def _on_transfer_source_changed(self, value: str):
        """Called when the source device dropdown changes."""
        serial = self._get_serial_from_menu(value)
        if serial:
            self.transfer_src_tree.set_serial(serial)

    def _on_transfer_target_changed(self, value: str):
        """Called when the target device dropdown changes."""
        serial = self._get_serial_from_menu(value)
        if serial:
            self.transfer_dst_tree.set_serial(serial)

    def _update_transfer_menus(self):
        # Build combined list: Android + iOS devices
        names: List[str] = []
        for s in self.devices:
            dev = self.devices[s]
            names.append(f"🤖 {dev.short_label()} ({s})")
        for s, udev in self.unified_devices.items():
            if udev.platform == DevicePlatform.IOS:
                names.append(f"🍎 {udev.short_label()} ({s})")

        placeholder = t("transfer.select_device")
        if not names:
            names = [t("transfer.no_device")]

        # Preserve current user selection if the device is still available
        prev_src = self.transfer_source_menu.get()
        prev_tgt = self.transfer_target_menu.get()

        self.transfer_source_menu.configure(values=names)
        self.transfer_target_menu.configure(values=names)

        if names == [t("transfer.no_device")]:
            self.transfer_source_menu.set(t("transfer.no_device"))
            self.transfer_target_menu.set(t("transfer.no_device"))
            return

        # Keep previous selection ONLY if it's still valid — never auto-pick
        if prev_src in names:
            self.transfer_source_menu.set(prev_src)
        else:
            self.transfer_source_menu.set(placeholder)

        if prev_tgt in names:
            self.transfer_target_menu.set(prev_tgt)
        else:
            self.transfer_target_menu.set(placeholder)

    def _get_serial_from_menu(self, menu_value: str) -> Optional[str]:
        """Extract serial from '(serial)' in menu value."""
        if "(" in menu_value and ")" in menu_value:
            return menu_value.split("(")[-1].rstrip(")")
        return None

    def _start_transfer(self):
        src = self._get_serial_from_menu(self.transfer_source_menu.get())
        tgt = self._get_serial_from_menu(self.transfer_target_menu.get())

        if not src or not tgt:
            messagebox.showwarning(t("transfer.title"), t("transfer.select_both"))
            return
        if src == tgt:
            messagebox.showwarning(t("transfer.title"), t("transfer.different_devices"))
            return

        # Detect cross-platform scenario
        is_cross = self.device_mgr.is_cross_platform(src, tgt)
        if is_cross:
            self._start_cross_transfer(src, tgt)
            return

        # Update trees with correct serials
        self.transfer_src_tree.set_serial(src)
        self.transfer_dst_tree.set_serial(tgt)

        # Gather messaging app keys
        do_messaging = self.transfer_cat_vars.get(
            "messaging_apps", ctk.BooleanVar(value=False)
        ).get()
        msg_keys = [k for k, v in self.transfer_msg_vars.items() if v.get()] if do_messaging else []

        # Gather unsynced app packages
        unsynced_pkgs = [pkg for pkg, v in self.transfer_unsynced_vars.items() if v.get()]

        # Gather custom paths from source tree
        custom_paths = self.transfer_src_tree.get_selected_paths()

        config = TransferConfig(
            apps=self.transfer_cat_vars.get("apps", ctk.BooleanVar(value=False)).get(),
            photos=self.transfer_cat_vars.get("photos", ctk.BooleanVar(value=False)).get(),
            videos=self.transfer_cat_vars.get("videos", ctk.BooleanVar(value=False)).get(),
            music=self.transfer_cat_vars.get("music", ctk.BooleanVar(value=False)).get(),
            documents=self.transfer_cat_vars.get("documents", ctk.BooleanVar(value=False)).get(),
            contacts=self.transfer_cat_vars.get("contacts", ctk.BooleanVar(value=False)).get(),
            sms=self.transfer_cat_vars.get("sms", ctk.BooleanVar(value=False)).get(),
            messaging_apps=do_messaging,
            messaging_app_keys=msg_keys,
            unsynced_packages=unsynced_pkgs,
            custom_paths=custom_paths,
            ignore_cache=self.var_ignore_cache.get(),
            ignore_thumbnails=self.var_ignore_thumbnails.get(),
        )

        self._lock_ui()
        self.btn_cancel_transfer.configure(state="normal")

        def _run():
            try:
                self.transfer_mgr.set_progress_callback(self._on_transfer_progress)
                success = self.transfer_mgr.transfer(src, tgt, config)
                msg = t("transfer.completed") if success else t("transfer.completed_errors")
                self.after(0, lambda: messagebox.showinfo(t("transfer.title"), msg))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror(t("common.error"), str(exc)))
            finally:
                self.after(0, self._transfer_finished)

        threading.Thread(target=_run, daemon=True).start()

    def _start_cross_transfer(self, src_serial: str, tgt_serial: str):
        """Handle cross-platform (Android ↔ iOS) transfer."""
        src_info = self.device_mgr.get_device_info(src_serial)
        tgt_info = self.device_mgr.get_device_info(tgt_serial)
        src_name = src_info.friendly_name() if src_info else src_serial
        tgt_name = tgt_info.friendly_name() if tgt_info else tgt_serial
        src_plat = src_info.platform_label() if src_info else "?"
        tgt_plat = tgt_info.platform_label() if tgt_info else "?"

        # Cross-platform dialog
        dlg = ctk.CTkToplevel(self)
        dlg.title("🔀 Transferência Cross-Platform")
        dlg.geometry("520x420")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self)
        dlg.attributes("-topmost", True)

        ctk.CTkLabel(
            dlg, text=t("cross_transfer.title"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(padx=16, pady=(16, 4))

        ctk.CTkLabel(
            dlg,
            text=f"{src_name} ({src_plat})  ➡️  {tgt_name} ({tgt_plat})",
            font=ctk.CTkFont(size=13),
            text_color=COLORS["warning"],
        ).pack(padx=16, pady=(0, 12))

        # Checkboxes for what to transfer
        cat_frame = ctk.CTkFrame(dlg)
        cat_frame.pack(fill="x", padx=16, pady=8)

        ctk.CTkLabel(
            cat_frame, text=t("cross_transfer.select_what"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        cross_cats = {
            "photos": (t("cross_transfer.cat_photos"), True),
            "videos": (t("cross_transfer.cat_videos"), True),
            "music": (t("cross_transfer.cat_music"), True),
            "documents": (t("cross_transfer.cat_documents"), True),
            "contacts": (t("cross_transfer.cat_contacts"), True),
            "sms": ("💬 SMS", True),
            "calendar": (t("cross_transfer.cat_calendar"), True),
            "whatsapp": (t("cross_transfer.cat_whatsapp"), True),
        }
        cross_vars: Dict[str, ctk.BooleanVar] = {}
        for key, (label, default) in cross_cats.items():
            var = ctk.BooleanVar(value=default)
            cross_vars[key] = var
            ctk.CTkCheckBox(
                cat_frame, text=label, variable=var,
            ).pack(anchor="w", padx=20, pady=2)

        # HEIC conversion option  
        heic_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            cat_frame, text=t("cross_transfer.convert_heic"),
            variable=heic_var,
        ).pack(anchor="w", padx=20, pady=2)

        # Warnings
        warn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        warn_frame.pack(fill="x", padx=16, pady=(8, 4))
        ctk.CTkLabel(
            warn_frame,
            text=t("cross_transfer.warnings"),
            font=ctk.CTkFont(size=11),
            text_color=COLORS["warning"],
            justify="left",
        ).pack(anchor="w")

        # Buttons
        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(8, 16))

        def _on_confirm():
            config = CrossTransferConfig(
                photos=cross_vars["photos"].get(),
                videos=cross_vars["videos"].get(),
                music=cross_vars["music"].get(),
                documents=cross_vars["documents"].get(),
                contacts=cross_vars["contacts"].get(),
                sms=cross_vars["sms"].get(),
                calendar=cross_vars["calendar"].get(),
                convert_heic=heic_var.get(),
            )
            want_whatsapp = cross_vars["whatsapp"].get()
            dlg.destroy()
            self._lock_ui()
            self.btn_cancel_transfer.configure(state="normal")

            def _run():
                try:
                    self.cross_transfer_mgr.set_progress_callback(
                        self._on_cross_transfer_progress
                    )
                    success = self.cross_transfer_mgr.transfer(
                        src_serial, tgt_serial, config
                    )

                    # WhatsApp media transfer (runs after main transfer)
                    if want_whatsapp and success:
                        try:
                            from .whatsapp_transfer import (
                                WhatsAppTransferManager,
                                WhatsAppTransferConfig,
                            )
                            wa_mgr = WhatsAppTransferManager(self.device_mgr)
                            wa_mgr.set_progress_callback(
                                lambda p: self._on_cross_transfer_progress(
                                    CrossTransferProgress(
                                        phase="whatsapp",
                                        sub_phase=p.sub_phase,
                                        current_item=p.current_item,
                                        percent=p.percent,
                                        errors=p.errors,
                                        warnings=p.warnings,
                                    )
                                )
                            )
                            wa_ok = wa_mgr.transfer(src_serial, tgt_serial)
                            if not wa_ok:
                                success = False
                        except ImportError:
                            log.warning("whatsapp_transfer module not available")
                        except Exception as wa_exc:
                            log.warning("WhatsApp transfer error: %s", wa_exc)

                    msg = (
                        t("cross_transfer.success")
                        if success else
                        t("cross_transfer.success_with_errors")
                    )
                    self.after(0, lambda: messagebox.showinfo(t("transfer.title"), msg))
                except Exception as exc:
                    self.after(0, lambda: messagebox.showerror(t("common.error"), str(exc)))
                finally:
                    self.after(0, self._transfer_finished)

            threading.Thread(target=_run, daemon=True).start()

        ctk.CTkButton(
            btn_frame,
            text=t("cross_transfer.btn_start"),
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            height=42, width=220,
            command=_on_confirm,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame,
            text=t("common.cancel"),
            fg_color=COLORS["text_dim"],
            height=42, width=120,
            command=dlg.destroy,
        ).pack(side="left", padx=8)

    def _on_cross_transfer_progress(self, p: CrossTransferProgress):
        """Update UI from cross-platform transfer progress."""
        def _update():
            if self._closing:
                return
            try:
                phase_labels = {
                    "initializing": t("progress.initializing"),
                    "contacts": t("progress.contacts"),
                    "sms": "SMS",
                    "calendar": t("progress.calendar"),
                    "photos": t("progress.photos"),
                    "videos": t("progress.videos"),
                    "music": t("progress.music"),
                    "documents": t("progress.documents"),
                    "whatsapp": "WhatsApp",
                    "complete": t("progress.complete"),
                    "complete_with_errors": t("progress.complete_errors"),
                    "error": t("common.error"),
                }
                label = phase_labels.get(p.phase, p.phase)
                self.transfer_progress_label.configure(
                    text=f"🔀 {label}: {p.sub_phase} - {p.current_item}"
                )
                self.transfer_progress_bar.set(p.percent / 100)
                detail = ""
                if p.elapsed_seconds > 0:
                    detail = f"{t('common.time')}: {format_duration(p.elapsed_seconds)}"
                if p.errors:
                    detail += f"  |  {t('common.errors')}: {len(p.errors)}"
                if p.warnings:
                    detail += f"  |  {t('common.warnings')}: {len(p.warnings)}"
                self.transfer_progress_detail.configure(text=detail)
            except Exception:
                pass
        self._safe_after(0, _update)

    # ------------------------------------------------------------------
    # Unsynced app detection for transfer tab
    # ------------------------------------------------------------------
    def _detect_unsynced_apps_transfer(self):
        """Detect unsynced apps on source device for transfer."""
        src_serial = self._get_serial_from_menu(self.transfer_source_menu.get())
        if not src_serial:
            messagebox.showwarning(t("transfer.detect"), t("transfer.select_source"))
            return

        self.btn_detect_unsynced_transfer.configure(state="disabled", text="⏳ ...")
        self._set_status(t("transfer.scanning_source"))

        def _run():
            try:
                detector = UnsyncedAppDetector(self.adb)
                detected = detector.detect(src_serial, include_unknown=True)
                self.after(0, lambda: self._on_unsynced_transfer_detected(detected))
            except Exception as exc:
                log.warning("Transfer unsynced detection error: %s", exc)
                self.after(0, lambda: self._set_status(f"{t('common.error')}: {exc}"))
                self.after(0, lambda: self.btn_detect_unsynced_transfer.configure(
                    state="normal", text=t("transfer.btn_detect"),
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _detect_messaging_apps_transfer(self):
        """Detect messaging apps on source device for transfer (uses source dropdown)."""
        src_serial = self._get_serial_from_menu(self.transfer_source_menu.get())
        if not src_serial:
            messagebox.showwarning(t("transfer.detect"), t("transfer.select_source"))
            return

        self._set_status(t("transfer.detecting_messaging"))

        def _run():
            try:
                detector = MessagingAppDetector(self.adb)
                installed = detector.detect_installed_apps(src_serial)
                self.after(0, lambda: self._on_messaging_transfer_detected(installed))
            except Exception as exc:
                log.warning("Transfer messaging detection error: %s", exc)
                self.after(0, lambda: self._set_status(f"{t('common.error')}: {exc}"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_messaging_transfer_detected(self, installed: Dict):
        """Update transfer tab messaging checkboxes based on detection."""
        count = len(installed)
        self._set_status(t("transfer.messaging_detected", count=count))
        for key in self.transfer_msg_vars:
            self.transfer_msg_vars[key].set(key in installed)

    def _on_unsynced_transfer_detected(self, detected: List[DetectedApp]):
        """Render detected unsynced apps in the transfer tab."""
        self._transfer_detected_apps = detected
        self.btn_detect_unsynced_transfer.configure(state="normal", text=t("transfer.btn_detect"))

        for w in self.transfer_unsynced_frame.winfo_children():
            w.destroy()
        self.transfer_unsynced_vars.clear()

        if not detected:
            ctk.CTkLabel(
                self.transfer_unsynced_frame,
                text=t("transfer.no_unsynced"),
                text_color="#06d6a0",
            ).pack(pady=6)
            return

        # Compact rendering for transfer tab
        risk_colors = {
            "critical": "#ef476f", "high": "#ffd166",
            "medium": "#06d6a0", "low": "#8d99ae", "unknown": "#a8a8a8",
        }
        prev_cat = None
        for app in detected:
            if app.category != prev_cat:
                prev_cat = app.category
                ctk.CTkLabel(
                    self.transfer_unsynced_frame,
                    text=f"{app.icon} {app.category_name}",
                    font=ctk.CTkFont(size=11, weight="bold"),
                    text_color="#e94560",
                ).pack(anchor="w", padx=2, pady=(4, 1))

            default_on = app.risk in ("critical", "high")
            var = ctk.BooleanVar(value=default_on)
            self.transfer_unsynced_vars[app.package] = var

            row = ctk.CTkFrame(self.transfer_unsynced_frame, fg_color="transparent", height=24)
            row.pack(fill="x", padx=2, pady=0)
            row.pack_propagate(False)

            ctk.CTkCheckBox(
                row, text=app.app_name, variable=var,
                font=ctk.CTkFont(size=11),
                checkbox_width=16, checkbox_height=16,
            ).pack(side="left", padx=2)

            rc = risk_colors.get(app.risk, "#a8a8a8")
            ctk.CTkLabel(
                row, text=f" {'⚠️' if app.risk in ('critical','high') else '•'} ",
                text_color=rc, font=ctk.CTkFont(size=10),
            ).pack(side="right", padx=4)

        self._set_status(t("transfer.apps_detected", count=len(detected)))

    def _clone_device(self):
        """Full clone via a single confirmation dialog.

        Opens a CTkToplevel where the user picks source and destination
        devices, reviews storage info and filter toggles, then confirms.
        Supports Android-only clone and cross-platform (Android ↔ iOS).
        """
        # Build combined label→serial mapping (Android + iOS)
        dev_labels: Dict[str, str] = {}
        for s in list(self.devices.keys()):
            dev = self.devices[s]
            dev_labels[f"🤖 {dev.short_label()}  ({s})"] = s
        for s, udev in self.unified_devices.items():
            if udev.platform == DevicePlatform.IOS:
                dev_labels[f"🍎 {udev.short_label()}  ({s})"] = s

        all_serials = list(dev_labels.values())
        if len(all_serials) < 2:
            messagebox.showwarning(
                t("clone.title"),
                t("clone.need_two_devices"),
            )
            return

        label_list = list(dev_labels.keys())

        # Pre-select from dropdowns if already set
        pre_src = self._get_serial_from_menu(self.transfer_source_menu.get())
        pre_tgt = self._get_serial_from_menu(self.transfer_target_menu.get())
        pre_src_lbl = next(
            (l for l, s in dev_labels.items() if s == pre_src), label_list[0]
        )
        pre_tgt_lbl = next(
            (l for l, s in dev_labels.items() if s == pre_tgt), (
                label_list[1] if len(label_list) > 1 else label_list[0]
            ),
        )

        # --- Dialog window ---
        dlg = ctk.CTkToplevel(self)
        dlg.title(t("clone.dialog_title"))
        dlg.geometry("580x520")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self)
        dlg.attributes("-topmost", True)

        # Title
        ctk.CTkLabel(
            dlg, text=t("clone.header"),
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(padx=16, pady=(16, 4))

        ctk.CTkLabel(
            dlg,
            text=t("clone.subtitle"),
            font=ctk.CTkFont(size=12),
            text_color=COLORS["text_dim"],
        ).pack(padx=16, pady=(0, 12))

        # ---- Source selector ----
        src_frame = ctk.CTkFrame(dlg)
        src_frame.pack(fill="x", padx=16, pady=4)

        ctk.CTkLabel(
            src_frame, text=t("clone.source_label"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 2))

        src_var = ctk.StringVar(value=pre_src_lbl)
        src_menu = ctk.CTkOptionMenu(src_frame, variable=src_var, values=label_list, width=500)
        src_menu.pack(padx=12, pady=(0, 4))

        src_info_lbl = ctk.CTkLabel(
            src_frame, text="", font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"], anchor="w",
        )
        src_info_lbl.pack(fill="x", padx=12, pady=(0, 8))

        # ---- Arrow ----
        ctk.CTkLabel(
            dlg, text=t("clone.arrow_label"),
            font=ctk.CTkFont(size=13), text_color=COLORS["warning"],
        ).pack(pady=4)

        # ---- Target selector ----
        tgt_frame = ctk.CTkFrame(dlg)
        tgt_frame.pack(fill="x", padx=16, pady=4)

        ctk.CTkLabel(
            tgt_frame, text=t("clone.target_label"),
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 2))

        tgt_var = ctk.StringVar(value=pre_tgt_lbl)
        tgt_menu = ctk.CTkOptionMenu(tgt_frame, variable=tgt_var, values=label_list, width=500)
        tgt_menu.pack(padx=12, pady=(0, 4))

        tgt_info_lbl = ctk.CTkLabel(
            tgt_frame, text="", font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"], anchor="w",
        )
        tgt_info_lbl.pack(fill="x", padx=12, pady=(0, 8))

        # ---- Update info labels when selection changes ----
        def _refresh_info(*_args):
            for var, lbl in ((src_var, src_info_lbl), (tgt_var, tgt_info_lbl)):
                serial = dev_labels.get(var.get())
                if serial:
                    # Try Android device first, then unified (iOS)
                    dev = self.devices.get(serial)
                    if dev:
                        stor = dev.storage_summary()
                        plat = "Android"
                    else:
                        udev = self.unified_devices.get(serial)
                        stor = udev.storage_summary() if udev else ""
                        plat = udev.platform_label() if udev else "?"
                    lbl.configure(
                        text=f"{plat}  |  Serial: {serial[:20]}…  |  {stor or 'N/A'}",
                    )

            # Detect cross-platform and update scope label
            s_src = dev_labels.get(src_var.get())
            s_tgt = dev_labels.get(tgt_var.get())
            if s_src and s_tgt and self.device_mgr.is_cross_platform(s_src, s_tgt):
                scope_lbl.configure(
                    text=t("clone.scope_cross"),
                    text_color=COLORS["warning"],
                )
            else:
                scope_lbl.configure(
                    text=t("clone.scope_android"),
                    text_color=COLORS["text_dim"],
                )

        src_var.trace_add("write", _refresh_info)
        tgt_var.trace_add("write", _refresh_info)

        # ---- What will be cloned ----
        scope_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        scope_frame.pack(fill="x", padx=16, pady=(8, 2))

        scope_lbl = ctk.CTkLabel(
            scope_frame,
            text=t("clone.scope_android"),
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
            wraplength=540,
        )
        scope_lbl.pack(anchor="w")

        _refresh_info()  # Initial population

        # ---- Filter toggles ----
        filter_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        filter_frame.pack(fill="x", padx=16, pady=(4, 8))

        dlg_ignore_cache = ctk.BooleanVar(value=self.var_ignore_cache.get())
        ctk.CTkCheckBox(
            filter_frame, text=t("transfer.filter_cache"),
            variable=dlg_ignore_cache,
        ).pack(side="left", padx=8)

        dlg_ignore_thumbs = ctk.BooleanVar(value=self.var_ignore_thumbnails.get())
        ctk.CTkCheckBox(
            filter_frame, text=t("transfer.filter_thumbs"),
            variable=dlg_ignore_thumbs,
        ).pack(side="left", padx=8)

        # ---- Buttons ----
        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(4, 16))

        def _on_confirm():
            src_serial = dev_labels.get(src_var.get())
            tgt_serial = dev_labels.get(tgt_var.get())

            if not src_serial or not tgt_serial:
                messagebox.showwarning(t("clone.title"), t("clone.select_both"), parent=dlg)
                return
            if src_serial == tgt_serial:
                messagebox.showwarning(t("clone.title"), t("clone.different_devices"), parent=dlg)
                return

            # Resolve names from either Android or Unified dict
            src_dev = self.devices.get(src_serial)
            tgt_dev = self.devices.get(tgt_serial)
            src_udev = self.unified_devices.get(src_serial)
            tgt_udev = self.unified_devices.get(tgt_serial)
            src_name = (src_dev.friendly_name() if src_dev
                        else src_udev.friendly_name() if src_udev else src_serial)
            tgt_name = (tgt_dev.friendly_name() if tgt_dev
                        else tgt_udev.friendly_name() if tgt_udev else tgt_serial)

            is_cross = self.device_mgr.is_cross_platform(src_serial, tgt_serial)

            # Final safety confirmation
            if is_cross:
                confirm_msg = t("clone.confirm_cross", src=src_name, tgt=tgt_name)
            else:
                confirm_msg = t("clone.confirm_android", src=src_name, tgt=tgt_name)

            if not messagebox.askyesno(t("clone.final_confirm"), confirm_msg, parent=dlg):
                return

            # Capture settings and close dialog
            _ic = dlg_ignore_cache.get()
            _it = dlg_ignore_thumbs.get()
            # Sync filter toggles back to main UI
            self.var_ignore_cache.set(_ic)
            self.var_ignore_thumbnails.set(_it)
            dlg.destroy()

            # Start the clone
            self._lock_ui()
            self.btn_cancel_transfer.configure(state="normal")

            if is_cross:
                # Cross-platform clone
                self._set_status(t("clone.cross_status", src=src_name, tgt=tgt_name))

                def _run():
                    try:
                        self.cross_transfer_mgr.set_progress_callback(
                            self._on_cross_transfer_progress
                        )
                        success = self.cross_transfer_mgr.transfer(
                            src_serial, tgt_serial
                        )
                        msg = (
                            t("cross_transfer.success")
                            if success else
                            t("cross_transfer.success_with_errors")
                        )
                        self.after(0, lambda: messagebox.showinfo(t("clone.title"), msg))
                    except Exception as exc:
                        self.after(0, lambda: messagebox.showerror(t("common.error"), str(exc)))
                    finally:
                        self.after(0, self._transfer_finished)

                threading.Thread(target=_run, daemon=True).start()
            else:
                # Android-to-Android clone
                self._set_status(t("clone.indexing", name=src_name))

                def _run():
                    try:
                        self.transfer_mgr.set_progress_callback(self._on_transfer_progress)
                        success = self.transfer_mgr.clone_full_storage(
                            src_serial, tgt_serial,
                            storage_path="/storage/emulated/0",
                            ignore_cache=_ic,
                            ignore_thumbnails=_it,
                        )
                        msg = (
                            t("clone.success")
                            if success else
                            t("clone.success_with_errors")
                        )
                        self.after(0, lambda: messagebox.showinfo(t("clone.title"), msg))
                    except Exception as exc:
                        self.after(0, lambda: messagebox.showerror(t("common.error"), str(exc)))
                    finally:
                        self.after(0, self._transfer_finished)

                threading.Thread(target=_run, daemon=True).start()

        ctk.CTkButton(
            btn_frame,
            text=t("clone.btn_confirm"),
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            height=42, width=220,
            command=_on_confirm,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame,
            text=t("common.cancel"),
            fg_color=COLORS["text_dim"],
            height=42, width=120,
            command=dlg.destroy,
        ).pack(side="left", padx=8)

    def _on_transfer_progress(self, p: TransferProgress):
        def _update():
            if self._closing:
                return
            try:
                phase_labels = {
                    "initializing": t("progress.initializing"),
                    "indexing": t("progress.indexing"),
                    "streaming": t("progress.streaming"),
                    "backing_up": t("progress.backing_up"),
                    "restoring": t("progress.restoring"),
                    "verifying": t("progress.verifying"),
                    "complete": t("progress.complete"),
                    "complete_with_errors": t("progress.complete_errors"),
                    "error": t("common.error"),
                }
                label = phase_labels.get(p.phase, p.phase)
                self.transfer_progress_label.configure(
                    text=f"{label}: {p.sub_phase} - {p.current_item}"
                )
                self.transfer_progress_bar.set(p.percent / 100)
                detail = ""
                if p.elapsed_seconds > 0:
                    detail = f"{t('common.time')}: {format_duration(p.elapsed_seconds)}"
                if p.errors:
                    detail += f"  |  {t('common.errors')}: {len(p.errors)}"
                self.transfer_progress_detail.configure(text=detail)
            except Exception:
                pass
        self._safe_after(0, _update)

    def _cancel_transfer(self):
        self.transfer_mgr.cancel()
        self.backup_mgr.cancel()
        self.restore_mgr.cancel()
        if hasattr(self, "cross_transfer_mgr") and self.cross_transfer_mgr:
            self.cross_transfer_mgr.cancel()
        self._set_status(t("transfer.cancelled"))

    def _transfer_finished(self):
        self._unlock_ui()

    # ==================================================================
    # Driver operations
    # ==================================================================
    def _check_drivers(self):
        self._set_status(t("drivers.checking"))

        def _run():
            status = self.driver_mgr.check_driver_status()
            self.after(0, lambda: self._display_driver_status(status))

        threading.Thread(target=_run, daemon=True).start()

    def _display_driver_status(self, status: DriverStatus):
        self.driver_status_text.configure(state="normal")
        self.driver_status_text.delete("1.0", "end")

        if not status.is_windows:
            self.driver_status_text.insert("end",
                t("drivers.not_needed")
            )
        else:
            lines = []
            if status.drivers_installed:
                lines.append(t("drivers.installed"))
            else:
                lines.append(t("drivers.not_installed"))

            lines.append(t("drivers.android_count", count=status.android_devices_detected))

            if status.devices_needing_driver:
                lines.append(t("drivers.need_driver", count=len(status.devices_needing_driver)))
                for d in status.devices_needing_driver:
                    lines.append(f"   - {d.get('caption', 'Unknown')} ({d.get('device_id', '')})")
            else:
                lines.append(t("drivers.all_ok"))

            if status.error:
                lines.append(t("drivers.detection_error", error=status.error))

            self.driver_status_text.insert("end", "\n".join(lines))

        self.driver_status_text.configure(state="disabled")
        self._set_status(t("drivers.check_done"))

    def _install_google_driver(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                t("common.permission"),
                t("common.admin_required"),
            )
            return
        self._run_driver_install(self.driver_mgr.install_google_usb_driver)

    def _install_universal_driver(self):
        self._run_driver_install(self.driver_mgr.install_universal_adb_driver)

    def _auto_install_drivers(self):
        if os.name != "nt":
            self._set_status(t("drivers.not_needed_platform"))
            return
        if self._driver_install_running:
            return
        self._run_driver_install(self.driver_mgr.auto_install_drivers)

    def _install_samsung_driver(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                t("common.permission"),
                t("common.admin_required"),
            )
            return
        self._run_driver_install(self.driver_mgr.install_samsung_driver)

    def _install_qualcomm_driver(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                t("common.permission"),
                t("common.admin_required"),
            )
            return
        self._run_driver_install(self.driver_mgr.install_qualcomm_driver)

    def _install_mediatek_driver(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                t("common.permission"),
                t("common.admin_required"),
            )
            return
        self._run_driver_install(self.driver_mgr.install_mediatek_driver)

    def _install_intel_driver(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                t("common.permission"),
                t("common.admin_required"),
            )
            return
        self._run_driver_install(self.driver_mgr.install_intel_driver)

    def _install_all_chipset_drivers(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                t("common.permission"),
                t("common.admin_required"),
            )
            return
        self._run_driver_install(self.driver_mgr.install_all_chipset_drivers)

    # -- Apple / iOS driver callbacks ----------------------------------------
    def _check_apple_drivers(self):
        """Check Apple/iOS driver status and display results."""
        self._set_status(t("drivers.checking"))

        def _run():
            from .driver_manager import check_apple_driver_status
            status = check_apple_driver_status()
            self.after(0, lambda: self._display_apple_driver_status(status))

        threading.Thread(target=_run, daemon=True).start()

    def _display_apple_driver_status(self, status: dict):
        self.driver_status_text.configure(state="normal")
        self.driver_status_text.delete("1.0", "end")

        lines = []
        if status.get("itunes_installed"):
            lines.append(t("drivers.apple.itunes_installed"))
        else:
            lines.append(t("drivers.apple.itunes_not_installed"))

        if status.get("amds_running"):
            lines.append(t("drivers.apple.service_running"))
        else:
            lines.append(t("drivers.apple.service_stopped"))

        if status.get("driver_inf_found"):
            lines.append(t("drivers.apple.driver_found"))
        else:
            lines.append(t("drivers.apple.driver_not_found"))

        ios_count = status.get("ios_devices_detected", 0)
        if ios_count:
            lines.append(t("drivers.apple.ios_count", count=ios_count))

        if status.get("error"):
            lines.append(f"\n⚠️ {status['error']}")

        if not status.get("itunes_installed"):
            lines.append(f"\n{t('drivers.apple.needs_itunes')}")

        self.driver_status_text.insert("end", "\n".join(lines))
        self.driver_status_text.configure(state="disabled")
        self._set_status(t("drivers.apple.check_done"))

    def _install_apple_drivers(self):
        """Install Apple/iOS drivers (iTunes + AMDS)."""
        self._set_status(t("drivers.apple.installing"))

        def _progress(msg: str, pct: int):
            if self._closing:
                return
            self._safe_after(0, lambda: self.driver_progress_label.configure(text=msg))
            self._safe_after(0, lambda: self.driver_progress_bar.set(pct / 100))

        def _run():
            from .driver_manager import install_apple_drivers
            try:
                success = install_apple_drivers(progress_cb=_progress)
                if success:
                    self._safe_after(0, lambda: self._set_status(
                        t("drivers.apple.install_done")
                    ))
                else:
                    self._safe_after(0, lambda: self._set_status(
                        t("drivers.apple.install_failed")
                    ))
            except Exception as exc:
                log.exception("Apple driver install error: %s", exc)
                self._safe_after(0, lambda: self._set_status(
                    t("drivers.apple.install_failed")
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _run_driver_install(self, install_func):
        if self._driver_install_running:
            return
        self._driver_install_running = True

        def _progress(msg: str, pct: int):
            if self._closing:
                return
            self._safe_after(0, lambda: self.driver_progress_label.configure(text=msg))
            self._safe_after(0, lambda: self.driver_progress_bar.set(pct / 100))

        def _run():
            try:
                success = install_func(progress_cb=_progress)
                if self._closing:
                    return
                if success:
                    self._safe_after(0, lambda: self._set_status(
                        t("drivers.install_success")
                    ))
                    # Stop monitor, restart ADB, then resume monitor
                    self.adb.stop_device_monitor()
                    if self.adb.adb_path:
                        self.driver_mgr.restart_adb_after_driver(self.adb.adb_path)
                    self.adb.start_device_monitor()
                    self._safe_after(2000, self._refresh_devices)
                else:
                    self._safe_after(0, lambda: self._set_status(
                        t("drivers.install_failed")
                    ))
            except Exception as exc:
                log.exception("Driver install thread error: %s", exc)
            finally:
                self._driver_install_running = False

        threading.Thread(target=_run, daemon=True).start()

    # ==================================================================
    # Settings operations
    # ==================================================================
    def _on_language_changed(self, selection: str):
        """Handle language dropdown change."""
        code = self._lang_code_map.get(selection, "en")
        set_language(code)
        self.config.set("app.language", code)
        self._set_status(t("settings.language_changed", lang=selection))

    def _browse_adb(self):
        path = filedialog.askopenfilename(
            title=t("settings.label_adb_path"),
            filetypes=[("Executable", "*.exe"), ("All", "*.*")],
        )
        if path:
            self.entry_adb_path.delete(0, "end")
            self.entry_adb_path.insert(0, path)

    def _browse_backup_dir(self):
        path = filedialog.askdirectory(title=t("settings.label_backup_dir"))
        if path:
            self.entry_backup_dir.delete(0, "end")
            self.entry_backup_dir.insert(0, path)

    def _download_platform_tools(self):
        self._set_status(t("settings.downloading_adb"))

        def _run():
            try:
                self.adb.download_platform_tools()
                self.after(0, lambda: messagebox.showinfo(
                    "Platform Tools", "Platform Tools baixado com sucesso!"
                ))
                self.after(0, lambda: self.entry_adb_path.delete(0, "end"))
                self.after(0, lambda: self.entry_adb_path.insert(0, self.adb.adb_path or ""))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Erro", str(exc)))

        threading.Thread(target=_run, daemon=True).start()

    def _refresh_path_status(self):
        """Check if ADB is in system PATH and update the label."""
        try:
            adb_dir = get_adb_dir(self.adb.base_dir)
            in_path = is_adb_in_path()
            admin = is_admin()

            if in_path:
                txt = t("settings.path_in_path")
                color = COLORS["success"]
            elif adb_dir:
                txt = t("settings.path_not_in_path", dir=adb_dir)
                color = COLORS["warning"]
            else:
                txt = t("settings.path_not_found")
                color = COLORS["error"]

            if not admin:
                txt += "\n" + t("settings.path_no_admin")

            self._safe_after(0, lambda: self.lbl_path_status.configure(
                text=txt, text_color=color,
            ))
        except Exception:
            pass

    def _add_adb_to_path(self):
        """Add ADB platform-tools to system PATH."""
        adb_dir = get_adb_dir(self.adb.base_dir)
        if not adb_dir:
            messagebox.showwarning(
                "ADB PATH",
                t("settings.path_download_first"),
            )
            return

        self._set_status(t("settings.adding_to_path"))
        ok, msg = add_adb_to_path(adb_dir)
        if ok:
            messagebox.showinfo("ADB PATH", msg)
        else:
            messagebox.showerror("ADB PATH", msg)
        self._set_status(t("common.done"))
        threading.Thread(target=self._refresh_path_status, daemon=True).start()

    def _remove_adb_from_path(self):
        """Remove ADB platform-tools from system PATH."""
        adb_dir = get_adb_dir(self.adb.base_dir)
        if not adb_dir:
            messagebox.showinfo("ADB PATH", t("settings.path_not_found"))
            return

        if not messagebox.askyesno(
            t("settings.remove_path_title"),
            t("settings.remove_path_msg"),
        ):
            return

        self._set_status(t("settings.removing_from_path"))
        ok, msg = remove_adb_from_path(adb_dir)
        if ok:
            messagebox.showinfo("ADB PATH", msg)
        else:
            messagebox.showerror("ADB PATH", msg)
        self._set_status(t("common.done"))
        threading.Thread(target=self._refresh_path_status, daemon=True).start()

    def _refresh_perf_profile_label(self):
        """Update the performance profile info label."""
        try:
            accel = self.transfer_mgr.accelerator
            info = accel.priority_info()
            prio = info.get("task_priority", "?")
            energy = info.get("energy_profile", "?")
            txt = t("settings.current_profile", priority=prio, energy=energy)
            self._safe_after(0, lambda: self.lbl_perf_profile.configure(text=txt))
        except Exception:
            pass

    def _apply_preset_max_performance(self):
        """Apply max-performance preset."""
        TransferAccelerator.preset_max_performance()
        self._refresh_perf_profile_label()
        self._set_status(t("settings.preset_applied", name=t("settings.preset_max_performance")))

    def _apply_preset_balanced(self):
        """Apply balanced preset."""
        TransferAccelerator.preset_balanced()
        self._refresh_perf_profile_label()
        self._set_status(t("settings.preset_applied", name=t("settings.preset_balanced")))

    def _apply_preset_power_saver(self):
        """Apply power-saver preset."""
        TransferAccelerator.preset_power_saver()
        self._refresh_perf_profile_label()
        self._set_status(t("settings.preset_applied", name=t("settings.preset_power_saver")))

    def _toggle_auto_threads(self):
        """Enable/disable the manual thread entry fields based on auto toggle."""
        auto = self.settings_auto_threads_var.get()
        state = "disabled" if auto else "normal"
        self.settings_pull_w.configure(state=state)
        self.settings_push_w.configure(state=state)
        if auto:
            dyn_pull, dyn_push = TransferAccelerator.compute_dynamic_workers()
            self.settings_pull_w.configure(state="normal")
            self.settings_pull_w.delete(0, "end")
            self.settings_pull_w.insert(0, str(dyn_pull))
            self.settings_pull_w.configure(state="disabled")
            self.settings_push_w.configure(state="normal")
            self.settings_push_w.delete(0, "end")
            self.settings_push_w.insert(0, str(dyn_push))
            self.settings_push_w.configure(state="disabled")
            self.lbl_auto_threads.configure(text_color=COLORS["success"])
        else:
            self.lbl_auto_threads.configure(text_color=COLORS["text_dim"])

    def _save_settings(self):
        adb_path = self.entry_adb_path.get().strip()
        if adb_path:
            self.adb.adb_path = adb_path

        backup_dir = self.entry_backup_dir.get().strip()
        if backup_dir:
            self.backup_mgr.backup_dir = Path(backup_dir)
            self.restore_mgr.backup_dir = Path(backup_dir)

        self.config.set("drivers.auto_install", self.auto_driver_var.get())

        # Acceleration settings
        gpu_on = self.settings_gpu_var.get()
        auto_thr = self.settings_auto_threads_var.get()
        npu_on = self.settings_npu_var.get()
        self.config.set("acceleration.gpu_enabled", gpu_on)
        self.config.set("acceleration.multi_gpu", self.settings_multigpu_var.get())
        self.config.set("acceleration.npu_enabled", npu_on)
        self.config.set("acceleration.verify_checksums", self.settings_verify_var.get())
        self.config.set("acceleration.checksum_algo", self.settings_algo_var.get())
        self.config.set("acceleration.auto_threads", auto_thr)

        if auto_thr:
            dyn_pull, dyn_push = TransferAccelerator.compute_dynamic_workers()
            self.config.set("acceleration.max_pull_workers", dyn_pull)
            self.config.set("acceleration.max_push_workers", dyn_push)
        else:
            try:
                pw = int(self.settings_pull_w.get())
                self.config.set("acceleration.max_pull_workers", max(1, min(pw, 16)))
            except ValueError:
                pass
            try:
                pushw = int(self.settings_push_w.get())
                self.config.set("acceleration.max_push_workers", max(1, min(pushw, 16)))
            except ValueError:
                pass

        # Virtualization settings
        self.config.set("virtualization.enabled", self.settings_virt_var.get())

        self.config.save()

        # Apply to accelerator at runtime
        accel = self.transfer_mgr.accelerator
        accel.set_gpu_enabled(gpu_on)
        accel.set_multi_gpu(self.settings_multigpu_var.get())
        accel.set_npu_enabled(npu_on)
        accel.set_virt_enabled(self.settings_virt_var.get())
        accel.verify_checksums = self.settings_verify_var.get()
        accel.checksum_algo = self.settings_algo_var.get()
        accel.auto_threads = auto_thr
        if auto_thr:
            accel.max_pull_workers, accel.max_push_workers = (
                TransferAccelerator.compute_dynamic_workers()
            )
        else:
            try:
                accel.max_pull_workers = int(self.settings_pull_w.get())
            except ValueError:
                pass
            try:
                accel.max_push_workers = int(self.settings_push_w.get())
            except ValueError:
                pass

        # Sync footer toggles
        self._gpu_toggle_var.set(gpu_on)
        self._npu_toggle_var.set(npu_on)
        self._virt_toggle_var.set(self.settings_virt_var.get())
        threading.Thread(target=self._init_accel_footer, daemon=True).start()
        self._refresh_perf_profile_label()

        self._set_status(t("settings.saved"))
        messagebox.showinfo(t("settings.title"), t("settings.saved"))

    def _load_settings_gpu_info(self):
        """Background: populate GPU/virt info in settings tab."""
        try:
            accel = self.transfer_mgr.accelerator
            lines = [accel.summary()]
            text = "\n".join(lines)
            self._safe_after(0, lambda: self.lbl_settings_gpu_info.configure(text=text))
        except Exception as exc:
            self._safe_after(
                0, lambda: self.lbl_settings_gpu_info.configure(
                    text=f"{t('common.error')}: {exc}",
                ),
            )

    # ==================================================================
    # Helpers
    # ==================================================================
    def _get_selected_device(self) -> Optional[str]:
        """Get a device serial, asking user if multiple are connected."""
        if not self.devices:
            messagebox.showwarning(
                t("devices.no_device_title"),
                t("devices.no_device_msg"),
            )
            return None

        if self.selected_device and self.selected_device in self.devices:
            return self.selected_device

        if len(self.devices) == 1:
            return list(self.devices.keys())[0]

        # Multiple devices — use the first one (could improve with a dialog)
        return list(self.devices.keys())[0]

    def _set_status(self, text: str):
        if self._closing:
            return
        try:
            self.lbl_status.configure(text=text)
        except Exception:
            pass

    def on_closing(self):
        if self._closing:
            return
        self._closing = True
        log.info("Shutting down...")
        self._dismiss_device_confirmation_ui()
        try:
            self.adb.stop_device_monitor()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
def run_gui(config: Config, adb: ADBCore):
    """Create and run the GUI application."""
    app = ADBToolkitApp(config, adb)
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    try:
        app.mainloop()
    except KeyboardInterrupt:
        app.on_closing()
    except Exception as exc:
        log.exception("GUI crashed: %s", exc)
        app.on_closing()
