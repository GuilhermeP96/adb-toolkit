"""
gui.py - Main graphical user interface for ADB Toolkit.

Built with customtkinter for a modern dark-mode UI.
Tabs: Devices | Backup | Restore | Transfer | Drivers | Settings
"""

import logging
import os
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
from .driver_manager import DriverManager, DriverStatus
from .device_explorer import (
    DeviceTreeBrowser, MessagingAppDetector, AndroidPathResolver, MESSAGING_APPS,
    UnsyncedAppDetector, DetectedApp, UNSYNCED_APP_CATEGORIES,
)
from .config import Config
from .utils import format_bytes, format_duration, open_folder
from .accelerator import TransferAccelerator, detect_all_gpus, detect_virtualization

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
        self.driver_mgr = DriverManager(adb.base_dir)

        self.devices: Dict[str, DeviceInfo] = {}
        self.selected_device: Optional[str] = None
        self._closing = False
        self._ready = False  # True after UI fully built
        self._driver_install_running = False

        # Window setup
        self.title("ADB Toolkit — Backup · Recovery · Transfer")
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

        self._tab_devices = self.tabview.add("📱 Dispositivos")
        self._tab_backup = self.tabview.add("💾 Backup")
        self._tab_restore = self.tabview.add("♻️ Restaurar")
        self._tab_transfer = self.tabview.add("🔄 Transferir")
        self._tab_drivers = self.tabview.add("🔧 Drivers")
        self._tab_settings = self.tabview.add("⚙️ Configurações")

        self._build_devices_tab()
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
            text="🔌 ADB Toolkit",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left", padx=16)

        self.lbl_connection = ctk.CTkLabel(
            frame,
            text="Nenhum dispositivo conectado",
            text_color=COLORS["text_dim"],
        )
        self.lbl_connection.pack(side="left", padx=20)

        ctk.CTkButton(
            frame, text="Atualizar", width=100,
            command=self._refresh_devices,
        ).pack(side="right", padx=8, pady=8)

    # ------------------------------------------------------------------
    # Status bar (footer) with acceleration toggles
    # ------------------------------------------------------------------
    def _build_statusbar(self):
        frame = ctk.CTkFrame(self, height=32, corner_radius=0)
        frame.pack(fill="x", side="bottom")
        frame.pack_propagate(False)

        # Left — status text
        self.lbl_status = ctk.CTkLabel(
            frame, text="Pronto", anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self.lbl_status.pack(side="left", padx=12)

        # Right — version
        self.lbl_version = ctk.CTkLabel(
            frame, text="v1.0.0", anchor="e",
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

        # Virtualization label + toggle
        self.lbl_virt_status = ctk.CTkLabel(
            frame, text="🖥️Virt: …", anchor="e",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
        )
        self.lbl_virt_status.pack(side="right", padx=(4, 2))

        self._virt_toggle_var = ctk.BooleanVar(
            value=self.config.get("virtualization.enabled", False),
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
        """Background detection of GPU + virtualization for footer labels."""
        try:
            accel = self.transfer_mgr.accelerator
            gpu_enabled = self._gpu_toggle_var.get()
            accel.set_gpu_enabled(gpu_enabled)

            gpus = accel.usable_gpus
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
            virt_enabled = self._virt_toggle_var.get()
            accel.set_virt_enabled(virt_enabled)
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
        except Exception as exc:
            log.debug("Footer accel init error: %s", exc)

    def _on_gpu_toggle(self):
        """GPU toggle callback."""
        on = self._gpu_toggle_var.get()
        self.config.set("acceleration.gpu_enabled", on)
        self.transfer_mgr.accelerator.set_gpu_enabled(on)
        # Refresh footer label
        threading.Thread(target=self._init_accel_footer, daemon=True).start()
        state = "ativada" if on else "desativada"
        self._set_status(f"Aceleração por GPU {state}")

    def _on_virt_toggle(self):
        """Virtualization toggle callback."""
        on = self._virt_toggle_var.get()
        self.config.set("virtualization.enabled", on)
        self.transfer_mgr.accelerator.set_virt_enabled(on)
        threading.Thread(target=self._init_accel_footer, daemon=True).start()
        state = "ativada" if on else "desativada"
        self._set_status(f"Virtualização {state}")

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
            text="Dispositivos Conectados",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 4))

        self.device_list_frame = ctk.CTkScrollableFrame(list_frame, height=200)
        self.device_list_frame.pack(fill="both", expand=True, padx=8, pady=8)

        self.lbl_no_devices = ctk.CTkLabel(
            self.device_list_frame,
            text="Nenhum dispositivo detectado.\n\n"
                 "1. Conecte seu dispositivo Android via USB\n"
                 "2. Ative a 'Depuração USB' nas Opções do Desenvolvedor\n"
                 "3. Aceite o prompt de depuração no dispositivo",
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
            text="Detalhes do Dispositivo",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        self.device_details_text = ctk.CTkTextbox(details_frame, height=120)
        self.device_details_text.pack(fill="x", padx=8, pady=(0, 8))
        self.device_details_text.insert("end", "Selecione um dispositivo acima.")
        self.device_details_text.configure(state="disabled")

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
            text="Opções de Backup",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        # Backup type selection
        type_frame = ctk.CTkFrame(opts_frame, fg_color="transparent")
        type_frame.pack(fill="x", padx=12, pady=4)

        self.backup_type_var = ctk.StringVar(value="selective")
        ctk.CTkRadioButton(
            type_frame, text="Backup Seletivo", variable=self.backup_type_var,
            value="selective", command=self._on_backup_type_change,
        ).pack(side="left", padx=(0, 20))
        ctk.CTkRadioButton(
            type_frame, text="Backup Completo (ADB)", variable=self.backup_type_var,
            value="full", command=self._on_backup_type_change,
        ).pack(side="left", padx=(0, 20))
        ctk.CTkRadioButton(
            type_frame, text="Caminhos Personalizados", variable=self.backup_type_var,
            value="custom", command=self._on_backup_type_change,
        ).pack(side="left")

        # ------ Standard category checkboxes ------
        self.backup_cats_frame = ctk.CTkFrame(opts_frame, fg_color="transparent")
        self.backup_cats_frame.pack(fill="x", padx=12, pady=8)

        self.backup_cat_vars: Dict[str, ctk.BooleanVar] = {}
        categories = [
            ("apps", "📦 Aplicativos (APKs)"),
            ("photos", "📷 Fotos"),
            ("videos", "🎬 Vídeos"),
            ("music", "🎵 Músicas"),
            ("documents", "📄 Documentos"),
            ("contacts", "👤 Contatos"),
            ("sms", "💬 SMS"),
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
            text="📱 Apps de Mensagem",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")

        ctk.CTkButton(
            msg_header, text="🔍 Detectar Apps", width=120, height=28,
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
            text="📦 Outros Apps com Dados Locais",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")

        self.btn_detect_unsynced = ctk.CTkButton(
            unsync_header,
            text="🔎 Detectar Apps sem Backup Online",
            width=220,
            height=28,
            command=self._detect_unsynced_apps,
        )
        self.btn_detect_unsynced.pack(side="right", padx=4)

        ctk.CTkLabel(
            unsync_frame,
            text="Apps instalados cujos dados podem não estar sincronizados na nuvem "
                 "(autenticadores, jogos, notas, gravações, etc.)",
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
            text="Clique em \"Detectar Apps sem Backup Online\" para escanear o dispositivo",
            text_color="#8d99ae",
            font=ctk.CTkFont(size=11),
        )
        self._unsynced_placeholder.pack(pady=10)

        # ------ File Tree Browser (for custom mode) ------
        tree_label_frame = ctk.CTkFrame(backup_scroll)
        tree_label_frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            tree_label_frame,
            text="🌳 Navegador de Arquivos do Dispositivo",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            tree_label_frame,
            text="Navegue pela árvore e marque as pastas/arquivos que deseja incluir no backup",
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
            text="▶ Iniciar Backup",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["success"],
            hover_color="#05c090",
            height=42,
            command=self._start_backup,
        )
        self.btn_start_backup.pack(side="left", padx=8)

        self.btn_cancel_backup = ctk.CTkButton(
            btn_frame, text="✖ Cancelar", fg_color=COLORS["error"],
            hover_color="#d63a5e", height=42, state="disabled",
            command=self._cancel_backup,
        )
        self.btn_cancel_backup.pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text="📂 Abrir Pasta de Backups",
            height=42, command=self._open_backup_folder,
        ).pack(side="right", padx=8)

        # ------ Progress section ------
        progress_frame = ctk.CTkFrame(backup_scroll)
        progress_frame.pack(fill="x", padx=4, pady=4)

        self.backup_progress_label = ctk.CTkLabel(
            progress_frame, text="Aguardando...", anchor="w",
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
            text="Backups Salvos",
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

        self._set_status("Detectando apps de mensagem...")

        def _run():
            try:
                detector = MessagingAppDetector(self.adb)
                installed = detector.detect_installed_apps(serial)
                self.after(0, lambda: self._on_messaging_detected(installed))
            except Exception as exc:
                log.warning("Messaging detection error: %s", exc)
                self.after(0, lambda: self._set_status(f"Erro: {exc}"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_messaging_detected(self, installed: Dict):
        """Update messaging checkboxes based on detection results."""
        count = len(installed)
        self._set_status(f"{count} app(s) de mensagem detectado(s)")

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

        self.btn_detect_unsynced.configure(state="disabled", text="⏳ Escaneando...")
        self._set_status("Escaneando apps com dados locais...")

        def _run():
            try:
                detector = UnsyncedAppDetector(self.adb)
                detected = detector.detect(serial, include_unknown=True, min_data_size_kb=256)
                self.after(0, lambda: self._on_unsynced_detected(detected))
            except Exception as exc:
                log.warning("Unsynced app detection error: %s", exc)
                self.after(0, lambda: self._set_status(f"Erro na detecção: {exc}"))
                self.after(0, lambda: self.btn_detect_unsynced.configure(
                    state="normal", text="🔎 Detectar Apps sem Backup Online",
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _on_unsynced_detected(self, detected: List[DetectedApp]):
        """Render detected unsynced apps as checkboxes grouped by category."""
        self._detected_apps = detected
        self.btn_detect_unsynced.configure(
            state="normal", text="🔎 Detectar Apps sem Backup Online",
        )

        # Clear previous content
        for w in self.backup_unsynced_frame.winfo_children():
            w.destroy()
        self.unsynced_app_vars.clear()

        if not detected:
            ctk.CTkLabel(
                self.backup_unsynced_frame,
                text="✅ Nenhum app adicional com dados locais significativos encontrado.",
                text_color="#06d6a0",
            ).pack(pady=10)
            self._set_status("Scan concluído — nenhum app adicional detectado")
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
            "critical": "CRÍTICO",
            "high": "ALTO",
            "medium": "MÉDIO",
            "low": "BAIXO",
            "unknown": "?",
        }

        # Header with select all / none
        hdr = ctk.CTkFrame(self.backup_unsynced_frame, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 4))

        total_label = ctk.CTkLabel(
            hdr,
            text=f"{len(detected)} app(s) detectado(s)",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        total_label.pack(side="left", padx=4)

        ctk.CTkButton(
            hdr, text="✅ Todos", width=60, height=24,
            fg_color="#06d6a0", hover_color="#05c090",
            command=lambda: self._toggle_all_unsynced(True),
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            hdr, text="❌ Nenhum", width=68, height=24,
            fg_color="#ef476f", hover_color="#d63a5e",
            command=lambda: self._toggle_all_unsynced(False),
        ).pack(side="right", padx=2)
        ctk.CTkButton(
            hdr, text="⚠️ Só Críticos", width=90, height=24,
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
        status = f"{len(detected)} app(s) detectado(s)"
        if n_crit:
            status += f" — ⚠️ {n_crit} CRÍTICO(S)"
        if n_high:
            status += f", {n_high} ALTO(S)"
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
            text="Selecionar Backup para Restaurar",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        sel_row = ctk.CTkFrame(sel_frame, fg_color="transparent")
        sel_row.pack(fill="x", padx=12, pady=(0, 8))

        self.restore_backup_menu = ctk.CTkOptionMenu(
            sel_row, values=["Nenhum backup disponível"],
            width=400,
        )
        self.restore_backup_menu.pack(side="left")

        ctk.CTkButton(
            sel_row, text="🔄 Atualizar Lista", width=120,
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
            opts_inner, text="📦 Restaurar Aplicativos", variable=self.restore_apps_var,
        ).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ctk.CTkCheckBox(
            opts_inner, text="📁 Restaurar Arquivos", variable=self.restore_files_var,
        ).grid(row=0, column=1, sticky="w", padx=8, pady=4)
        ctk.CTkCheckBox(
            opts_inner, text="💬 Restaurar Apps de Mensagem",
            variable=self.restore_messaging_var,
        ).grid(row=0, column=2, sticky="w", padx=8, pady=4)
        ctk.CTkCheckBox(
            opts_inner, text="💾 Restaurar Dados de Apps (confirmação no device)",
            variable=self.restore_data_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=4)

        # File tree browser for destination device preview
        tree_frame = ctk.CTkFrame(restore_scroll)
        tree_frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            tree_frame,
            text="🌳 Visualizar Arquivos do Dispositivo Destino",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            tree_frame,
            text="Navegue para verificar o conteúdo atual antes de restaurar",
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
            text="▶ Iniciar Restauração",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["warning"],
            text_color="black",
            hover_color="#e6bc5a",
            height=42,
            command=self._start_restore,
        )
        self.btn_start_restore.pack(side="left", padx=8)

        self.btn_cancel_restore = ctk.CTkButton(
            btn_frame, text="✖ Cancelar", fg_color=COLORS["error"],
            height=42, state="disabled",
            command=self._cancel_restore,
        )
        self.btn_cancel_restore.pack(side="left", padx=8)

        # Progress
        progress_frame = ctk.CTkFrame(restore_scroll)
        progress_frame.pack(fill="x", padx=4, pady=4)

        self.restore_progress_label = ctk.CTkLabel(
            progress_frame, text="Aguardando...", anchor="w",
        )
        self.restore_progress_label.pack(fill="x", padx=12, pady=(8, 2))

        self.restore_progress_bar = ctk.CTkProgressBar(progress_frame)
        self.restore_progress_bar.pack(fill="x", padx=12, pady=(0, 8))
        self.restore_progress_bar.set(0)

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
            text="Transferência entre Dispositivos",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        row_frame = ctk.CTkFrame(dev_frame, fg_color="transparent")
        row_frame.pack(fill="x", padx=12, pady=4)

        # Source
        ctk.CTkLabel(row_frame, text="📱 Origem:").grid(row=0, column=0, sticky="w", padx=4)
        self.transfer_source_menu = ctk.CTkOptionMenu(
            row_frame, values=["Nenhum dispositivo"], width=300,
            command=self._on_transfer_source_changed,
        )
        self.transfer_source_menu.grid(row=0, column=1, padx=8, pady=4)

        # Arrow
        ctk.CTkLabel(
            row_frame, text="  ➡️  ",
            font=ctk.CTkFont(size=20),
        ).grid(row=0, column=2, padx=4)

        # Target
        ctk.CTkLabel(row_frame, text="📱 Destino:").grid(row=0, column=3, sticky="w", padx=4)
        self.transfer_target_menu = ctk.CTkOptionMenu(
            row_frame, values=["Nenhum dispositivo"], width=300,
            command=self._on_transfer_target_changed,
        )
        self.transfer_target_menu.grid(row=0, column=4, padx=8, pady=4)

        # Transfer categories
        cats_frame = ctk.CTkFrame(transfer_scroll)
        cats_frame.pack(fill="x", padx=4, pady=(0, 4))

        ctk.CTkLabel(
            cats_frame,
            text="O que transferir:",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        self.transfer_cat_vars: Dict[str, ctk.BooleanVar] = {}
        t_cats = [
            ("apps", "📦 Aplicativos"),
            ("photos", "📷 Fotos"),
            ("videos", "🎬 Vídeos"),
            ("music", "🎵 Músicas"),
            ("documents", "📄 Documentos"),
            ("contacts", "👤 Contatos"),
            ("sms", "💬 SMS"),
            ("messaging_apps", "💬 Apps de Mensagem"),
        ]

        cats_inner = ctk.CTkFrame(cats_frame, fg_color="transparent")
        cats_inner.pack(fill="x", padx=12, pady=(0, 4))

        for i, (key, label) in enumerate(t_cats):
            var = ctk.BooleanVar(value=True if key != "messaging_apps" else False)
            self.transfer_cat_vars[key] = var
            ctk.CTkCheckBox(
                cats_inner, text=label, variable=var,
            ).grid(row=i // 4, column=i % 4, sticky="w", padx=8, pady=4)

        # Messaging apps selection for transfer
        msg_transfer_frame = ctk.CTkFrame(transfer_scroll)
        msg_transfer_frame.pack(fill="x", padx=4, pady=4)

        msg_hdr = ctk.CTkFrame(msg_transfer_frame, fg_color="transparent")
        msg_hdr.pack(fill="x", padx=12, pady=(8, 4))

        ctk.CTkLabel(
            msg_hdr,
            text="📱 Apps de Mensagem a Transferir",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        ctk.CTkButton(
            msg_hdr, text="🔍 Detectar", width=90, height=28,
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
            text="📦 Outros Apps (dados locais)",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left")

        self.btn_detect_unsynced_transfer = ctk.CTkButton(
            unsync_t_hdr,
            text="🔎 Detectar",
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
            text="Detecte primeiro para ver apps disponíveis",
            text_color="#8d99ae",
            font=ctk.CTkFont(size=11),
        ).pack(pady=6)

        # Source device file tree
        src_tree_frame = ctk.CTkFrame(transfer_scroll)
        src_tree_frame.pack(fill="x", padx=4, pady=4)

        ctk.CTkLabel(
            src_tree_frame,
            text="🌳 Arquivos do Dispositivo Origem",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 2))

        ctk.CTkLabel(
            src_tree_frame,
            text="Selecione pastas/arquivos extras a transferir (opcional)",
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
            text="🌳 Arquivos do Dispositivo Destino",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 2))

        ctk.CTkLabel(
            dst_tree_frame,
            text="Visualize o conteúdo do dispositivo destino",
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
            text="🔄 Iniciar Transferência",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            height=42,
            command=self._start_transfer,
        )
        self.btn_start_transfer.pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame,
            text="📋 Clonar Dispositivo (Tudo)",
            height=42,
            command=self._clone_device,
        ).pack(side="left", padx=8)

        self.btn_cancel_transfer = ctk.CTkButton(
            btn_frame, text="✖ Cancelar",
            fg_color=COLORS["error"], height=42, state="disabled",
            command=self._cancel_transfer,
        )
        self.btn_cancel_transfer.pack(side="left", padx=8)

        # Progress
        progress_frame = ctk.CTkFrame(transfer_scroll)
        progress_frame.pack(fill="x", padx=4, pady=4)

        self.transfer_progress_label = ctk.CTkLabel(
            progress_frame, text="Aguardando...", anchor="w",
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
            text="Gerenciamento de Drivers USB",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        self.driver_status_text = ctk.CTkTextbox(info_frame, height=150)
        self.driver_status_text.pack(fill="x", padx=12, pady=(0, 8))
        self.driver_status_text.insert("end", "Clique em 'Verificar Drivers' para analisar.")
        self.driver_status_text.configure(state="disabled")

        # --- Row 1: Check + Google + Universal + Auto ---
        btn_frame = ctk.CTkFrame(tab, fg_color="transparent")
        btn_frame.pack(fill="x", padx=8, pady=4)

        ctk.CTkButton(
            btn_frame, text="🔍 Verificar Drivers", height=40,
            command=self._check_drivers,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text="📥 Instalar Google USB Driver",
            fg_color=COLORS["success"], hover_color="#05c090", height=40,
            command=self._install_google_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text="📥 Instalar Driver Universal",
            fg_color=COLORS["warning"], text_color="black",
            hover_color="#e6bc5a", height=40,
            command=self._install_universal_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_frame, text="🔄 Auto-detectar e Instalar",
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            height=40,
            command=self._auto_install_drivers,
        ).pack(side="left", padx=8)

        # --- Row 2: Chipset-specific drivers ---
        ctk.CTkLabel(
            tab, text="Drivers por Chipset:",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(10, 2))

        chipset_frame = ctk.CTkFrame(tab, fg_color="transparent")
        chipset_frame.pack(fill="x", padx=8, pady=4)

        ctk.CTkButton(
            chipset_frame, text="📱 Samsung (Exynos/ODIN)",
            fg_color="#1428A0", hover_color="#0D1B6E", height=40,
            command=self._install_samsung_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            chipset_frame, text="🔧 Qualcomm (Snapdragon)",
            fg_color="#3253DC", hover_color="#263EA0", height=40,
            command=self._install_qualcomm_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            chipset_frame, text="⚙️ MediaTek",
            fg_color="#E3350D", hover_color="#B02A0A", height=40,
            command=self._install_mediatek_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            chipset_frame, text="💻 Intel",
            fg_color="#0068B5", hover_color="#004A80", height=40,
            command=self._install_intel_driver,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            chipset_frame, text="📦 Instalar Todos os Chipsets",
            fg_color="#6B21A8", hover_color="#4C1D95", height=40,
            command=self._install_all_chipset_drivers,
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

        frame = ctk.CTkFrame(tab)
        frame.pack(fill="both", expand=True, padx=8, pady=8)

        ctk.CTkLabel(
            frame,
            text="Configurações",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        # ADB path
        adb_frame = ctk.CTkFrame(frame, fg_color="transparent")
        adb_frame.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(adb_frame, text="Caminho ADB:").pack(side="left")
        self.entry_adb_path = ctk.CTkEntry(adb_frame, width=400)
        self.entry_adb_path.pack(side="left", padx=8)
        if self.adb.adb_path:
            self.entry_adb_path.insert(0, self.adb.adb_path)
        ctk.CTkButton(
            adb_frame, text="Procurar", width=80,
            command=self._browse_adb,
        ).pack(side="left")

        # Backup directory
        bkp_frame = ctk.CTkFrame(frame, fg_color="transparent")
        bkp_frame.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(bkp_frame, text="Pasta de Backups:").pack(side="left")
        self.entry_backup_dir = ctk.CTkEntry(bkp_frame, width=400)
        self.entry_backup_dir.pack(side="left", padx=8)
        self.entry_backup_dir.insert(0, str(self.backup_mgr.backup_dir))
        ctk.CTkButton(
            bkp_frame, text="Procurar", width=80,
            command=self._browse_backup_dir,
        ).pack(side="left")

        # Auto-install drivers
        self.auto_driver_var = ctk.BooleanVar(
            value=self.config.get("drivers.auto_install", True)
        )
        ctk.CTkCheckBox(
            frame, text="Instalar drivers automaticamente ao detectar dispositivo",
            variable=self.auto_driver_var,
        ).pack(anchor="w", padx=12, pady=8)

        # ────── Acceleration section ──────
        accel_header = ctk.CTkFrame(frame, fg_color="transparent")
        accel_header.pack(fill="x", padx=12, pady=(16, 4))
        ctk.CTkLabel(
            accel_header,
            text="⚡ Aceleração por Hardware",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w")

        # GPU enabled checkbox
        self.settings_gpu_var = ctk.BooleanVar(
            value=self.config.get("acceleration.gpu_enabled", True),
        )
        ctk.CTkCheckBox(
            frame, text="Habilitar aceleração GPU para verificação de integridade",
            variable=self.settings_gpu_var,
        ).pack(anchor="w", padx=12, pady=4)

        # Multi-GPU
        self.settings_multigpu_var = ctk.BooleanVar(
            value=self.config.get("acceleration.multi_gpu", False),
        )
        ctk.CTkCheckBox(
            frame, text="Distribuir carga entre múltiplas GPUs (se disponíveis)",
            variable=self.settings_multigpu_var,
        ).pack(anchor="w", padx=28, pady=2)

        # Checksum verification
        self.settings_verify_var = ctk.BooleanVar(
            value=self.config.get("acceleration.verify_checksums", True),
        )
        ctk.CTkCheckBox(
            frame, text="Verificar checksums após transferência/clone",
            variable=self.settings_verify_var,
        ).pack(anchor="w", padx=12, pady=4)

        # Checksum algo
        algo_frame = ctk.CTkFrame(frame, fg_color="transparent")
        algo_frame.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(algo_frame, text="Algoritmo de checksum:").pack(side="left")
        self.settings_algo_var = ctk.StringVar(
            value=self.config.get("acceleration.checksum_algo", "md5"),
        )
        ctk.CTkOptionMenu(
            algo_frame, values=["md5", "sha1", "sha256"],
            variable=self.settings_algo_var, width=100,
        ).pack(side="left", padx=8)

        # Workers
        workers_frame = ctk.CTkFrame(frame, fg_color="transparent")
        workers_frame.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(workers_frame, text="Threads pull/push:").pack(side="left")
        self.settings_pull_w = ctk.CTkEntry(workers_frame, width=50)
        self.settings_pull_w.pack(side="left", padx=4)
        self.settings_pull_w.insert(
            0, str(self.config.get("acceleration.max_pull_workers", 4)),
        )
        ctk.CTkLabel(workers_frame, text="/").pack(side="left")
        self.settings_push_w = ctk.CTkEntry(workers_frame, width=50)
        self.settings_push_w.pack(side="left", padx=4)
        self.settings_push_w.insert(
            0, str(self.config.get("acceleration.max_push_workers", 4)),
        )

        # ────── Virtualization section ──────
        virt_header = ctk.CTkFrame(frame, fg_color="transparent")
        virt_header.pack(fill="x", padx=12, pady=(16, 4))
        ctk.CTkLabel(
            virt_header,
            text="🖥️ Virtualização",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w")

        self.settings_virt_var = ctk.BooleanVar(
            value=self.config.get("virtualization.enabled", False),
        )
        ctk.CTkCheckBox(
            frame, text="Habilitar virtualização (Hyper-V / VT-x / WSL2)",
            variable=self.settings_virt_var,
        ).pack(anchor="w", padx=12, pady=4)

        # GPU info label (populated async)
        self.lbl_settings_gpu_info = ctk.CTkLabel(
            frame, text="Detectando GPUs…",
            font=ctk.CTkFont(size=11), text_color=COLORS["text_dim"],
            justify="left", anchor="w",
        )
        self.lbl_settings_gpu_info.pack(anchor="w", padx=12, pady=(8, 4))

        threading.Thread(target=self._load_settings_gpu_info, daemon=True).start()

        # Download ADB
        ctk.CTkButton(
            frame, text="📥 Baixar/Atualizar Platform Tools",
            command=self._download_platform_tools,
        ).pack(anchor="w", padx=12, pady=8)

        # Save
        ctk.CTkButton(
            frame, text="💾 Salvar Configurações",
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
                f"Dispositivo conectado: {device.friendly_name()}"
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

        # Update top bar
        count = len(self.devices)
        if count == 0:
            self.lbl_connection.configure(
                text="Nenhum dispositivo conectado",
                text_color=COLORS["text_dim"],
            )
        elif count == 1:
            dev = list(self.devices.values())[0]
            self.lbl_connection.configure(
                text=f"✅ {dev.friendly_name()} ({dev.state})",
                text_color=COLORS["success"],
            )
        else:
            self.lbl_connection.configure(
                text=f"✅ {count} dispositivos conectados",
                text_color=COLORS["success"],
            )

        # Update device list tab (quick — basic info from adb devices -l)
        for w in self.device_list_frame.winfo_children():
            w.destroy()

        if not self.devices:
            self.lbl_no_devices = ctk.CTkLabel(
                self.device_list_frame,
                text="Nenhum dispositivo detectado.\n\n"
                     "1. Conecte seu dispositivo Android via USB\n"
                     "2. Ative a 'Depuração USB' nas Opções do Desenvolvedor\n"
                     "3. Aceite o prompt de depuração no dispositivo",
                font=ctk.CTkFont(size=13),
                text_color=COLORS["text_dim"],
                justify="center",
            )
            self.lbl_no_devices.pack(expand=True, pady=40)
        else:
            for serial, dev in self.devices.items():
                self._create_device_card(serial, dev)

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
            details_parts.append(f"Marca: {dev.manufacturer}")
        if dev.model:
            details_parts.append(f"Modelo: {dev.model}")
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
            btn_row, text="ℹ️ Detalhes", width=100,
            command=lambda s=serial: self._show_device_details(s),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            btn_row, text="💾 Backup", width=100,
            fg_color=COLORS["success"], hover_color="#05c090",
            command=lambda s=serial: self._quick_backup(s),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            btn_row, text="🔄 Reiniciar", width=100,
            fg_color=COLORS["warning"], text_color="black",
            command=lambda s=serial: self._reboot_device(s),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            btn_row, text="🧹 Limpar Cache", width=120,
            fg_color="#7b2cbf", hover_color="#9d4edd",
            command=lambda s=serial: self._open_cache_manager(s),
        ).pack(side="left", padx=4)

    def _show_device_details(self, serial: str):
        """Show detailed device info."""
        self._set_status(f"Obtendo detalhes de {serial}...")

        def _fetch():
            dev = self.adb.get_device_details(serial)
            self.after(0, lambda: self._display_device_details(dev))

        threading.Thread(target=_fetch, daemon=True).start()

    def _display_device_details(self, dev: DeviceInfo):
        self.device_details_text.configure(state="normal")
        self.device_details_text.delete("1.0", "end")
        text = (
            f"Fabricante:     {dev.manufacturer}\n"
            f"Modelo:         {dev.model}\n"
            f"Produto:        {dev.product}\n"
            f"Android:        {dev.android_version} (SDK {dev.sdk_version})\n"
            f"Serial:         {dev.serial}\n"
            f"Bateria:        {dev.battery_level}%\n"
            f"Armazenamento:  {format_bytes(dev.storage_free)} livre "
            f"/ {format_bytes(dev.storage_total)} total"
        )
        self.device_details_text.insert("end", text)
        self.device_details_text.configure(state="disabled")
        self._set_status("Detalhes carregados")

    def _reboot_device(self, serial: str):
        if messagebox.askyesno("Reiniciar", f"Reiniciar dispositivo {serial}?"):
            self.adb.reboot("", serial)
            self._set_status(f"Reiniciando {serial}...")

    def _quick_backup(self, serial: str):
        self.selected_device = serial
        self.tabview.set("💾 Backup")

    # ==================================================================
    # Cache management
    # ==================================================================
    def _open_cache_manager(self, serial: str):
        """Open a dialog to manage app cache for the device."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("🧹 Limpar Cache")
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
            hdr, text=f"🧹 Gerenciador de Cache — {dev_name}",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w")

        self._cache_status_label = ctk.CTkLabel(
            hdr, text="Escaneando cache dos aplicativos...",
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=12),
        )
        self._cache_status_label.pack(anchor="w", pady=(4, 0))

        # Buttons row
        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(4, 4))

        btn_clear_all = ctk.CTkButton(
            btn_frame, text="🗑️ Limpar TODOS os Caches",
            fg_color=COLORS["error"], hover_color="#d63a5e",
            height=36, width=220,
            command=lambda: self._clear_all_cache(serial, dialog),
        )
        btn_clear_all.pack(side="left", padx=4)

        btn_clear_sel = ctk.CTkButton(
            btn_frame, text="🧹 Limpar Selecionados",
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
            sel_frame, text="✅ Todos", width=70, height=28,
            fg_color="#06d6a0", hover_color="#05c090",
            command=lambda: self._cache_select_toggle(True),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            sel_frame, text="❌ Nenhum", width=80, height=28,
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
        self._cache_status_label.configure(text="⏳ Escaneando cache dos aplicativos...")

        for w in scroll_frame.winfo_children():
            w.destroy()
        self._cache_check_vars.clear()

        ctk.CTkLabel(
            scroll_frame, text="⏳ Carregando...",
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
                    text=f"Erro ao escanear: {exc}"
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
            text=f"{len(cache_data)} apps · {apps_with_cache} com cache · "
                 f"Total estimado: {format_bytes(total_cache)}"
        )

        if not cache_data:
            ctk.CTkLabel(
                scroll_frame, text="Nenhum aplicativo encontrado.",
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
            "Limpar Cache",
            "Limpar o cache de TODOS os aplicativos?\n\n"
            "Isso não apaga dados pessoais, apenas cache temporário.",
            parent=dialog,
        ):
            return

        self._cache_status_label.configure(text="⏳ Limpando todos os caches...")

        def _run():
            try:
                success = self.adb.clear_all_cache(serial)
                if success:
                    msg = "✅ Cache de todos os aplicativos limpo!"
                else:
                    msg = "⚠️ Comando executado, mas pode exigir root."
                self.after(0, lambda: self._cache_status_label.configure(text=msg))
                # Refresh the list
                self.after(500, lambda: self._scan_cache(
                    serial, self._cache_scroll_frame, dialog,
                ))
            except Exception as exc:
                self.after(0, lambda: self._cache_status_label.configure(
                    text=f"Erro: {exc}"
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _clear_selected_cache(self, serial: str, dialog):
        """Clear cache for selected apps."""
        selected = [pkg for pkg, var in self._cache_check_vars.items() if var.get()]
        if not selected:
            messagebox.showinfo("Limpar Cache", "Nenhum app selecionado.", parent=dialog)
            return

        if not messagebox.askyesno(
            "Limpar Cache",
            f"Limpar cache de {len(selected)} aplicativo(s)?\n\n"
            "Isso não apaga dados pessoais, apenas cache temporário.",
            parent=dialog,
        ):
            return

        self._cache_status_label.configure(
            text=f"⏳ Limpando cache de {len(selected)} app(s)..."
        )

        def _run():
            try:
                cleared = 0
                for i, pkg in enumerate(selected):
                    self.adb.clear_app_cache(pkg, serial)
                    cleared += 1
                    if i % 5 == 0:
                        self.after(0, lambda c=cleared, t=len(selected):
                            self._cache_status_label.configure(
                                text=f"⏳ Limpando... {c}/{t}"
                            )
                        )

                self.after(0, lambda: self._cache_status_label.configure(
                    text=f"✅ Cache limpo em {cleared} aplicativo(s)!"
                ))
                # Refresh the list
                self.after(500, lambda: self._scan_cache(
                    serial, self._cache_scroll_frame, dialog,
                ))
            except Exception as exc:
                self.after(0, lambda: self._cache_status_label.configure(
                    text=f"Erro: {exc}"
                ))

        threading.Thread(target=_run, daemon=True).start()

    # ==================================================================
    # Backup operations
    # ==================================================================
    def _start_backup(self):
        serial = self._get_selected_device()
        if not serial:
            return

        # Update tree serial before backup
        self.backup_tree.set_serial(serial)

        self.btn_start_backup.configure(state="disabled")
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
                            "Selecione caminhos na árvore, apps de mensagem ou outros apps.",
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

                self.after(0, lambda: messagebox.showinfo("Backup", "Backup concluído!"))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Erro", str(exc)))
            finally:
                self.after(0, self._backup_finished)

        threading.Thread(target=_run, daemon=True).start()

    def _on_backup_progress(self, p: BackupProgress):
        def _update():
            if self._closing:
                return
            try:
                self.backup_progress_label.configure(text=f"{p.phase}: {p.current_item}")
                self.backup_progress_bar.set(p.percent / 100)
                detail = ""
                if p.items_total:
                    detail = f"{p.items_done}/{p.items_total} itens"
                if p.bytes_total:
                    detail += f"  |  {format_bytes(p.bytes_done)}/{format_bytes(p.bytes_total)}"
                self.backup_progress_detail.configure(text=detail)
            except Exception:
                pass
        self._safe_after(0, _update)

    def _cancel_backup(self):
        self.backup_mgr.cancel()
        self._set_status("Backup cancelado")

    def _backup_finished(self):
        self.btn_start_backup.configure(state="normal")
        self.btn_cancel_backup.configure(state="disabled")
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
                text="Nenhum backup encontrado",
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
            self.restore_backup_menu.configure(values=["Nenhum backup disponível"])

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
            info += f" | {m.file_count} arquivos"
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
        if messagebox.askyesno("Excluir Backup", f"Excluir '{backup_id}'?"):
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
        if "Nenhum" in selected:
            messagebox.showwarning("Restauração", "Nenhum backup selecionado")
            return

        backup_id = selected.split(" (")[0]

        if not messagebox.askyesno(
            "Confirmar Restauração",
            f"Restaurar backup '{backup_id}' para {serial}?\n\n"
            "Isso pode sobrescrever dados existentes no dispositivo.",
        ):
            return

        self.btn_start_restore.configure(state="disabled")
        self.btn_cancel_restore.configure(state="normal")

        def _run():
            try:
                self.restore_mgr.set_progress_callback(self._on_restore_progress)
                self.restore_mgr.restore_smart(serial, backup_id)
                self.after(0, lambda: messagebox.showinfo(
                    "Restauração", "Restauração concluída!"
                ))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Erro", str(exc)))
            finally:
                self.after(0, self._restore_finished)

        threading.Thread(target=_run, daemon=True).start()

    def _on_restore_progress(self, p: BackupProgress):
        def _update():
            if self._closing:
                return
            try:
                self.restore_progress_label.configure(text=f"{p.phase}: {p.current_item}")
                self.restore_progress_bar.set(p.percent / 100)
            except Exception:
                pass
        self._safe_after(0, _update)

    def _cancel_restore(self):
        self.restore_mgr.cancel()

    def _restore_finished(self):
        self.btn_start_restore.configure(state="normal")
        self.btn_cancel_restore.configure(state="disabled")

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
        serials = list(self.devices.keys())
        placeholder = "⬇ Selecione o dispositivo"
        names = [
            f"{self.devices[s].short_label()} ({s})" for s in serials
        ] if serials else ["Nenhum dispositivo"]

        # Preserve current user selection if the device is still available
        prev_src = self.transfer_source_menu.get()
        prev_tgt = self.transfer_target_menu.get()

        self.transfer_source_menu.configure(values=names)
        self.transfer_target_menu.configure(values=names)

        if not serials:
            self.transfer_source_menu.set("Nenhum dispositivo")
            self.transfer_target_menu.set("Nenhum dispositivo")
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
            messagebox.showwarning("Transferência", "Selecione os dispositivos de origem e destino")
            return
        if src == tgt:
            messagebox.showwarning("Transferência", "Origem e destino devem ser diferentes")
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
        )

        self.btn_start_transfer.configure(state="disabled")
        self.btn_cancel_transfer.configure(state="normal")

        def _run():
            try:
                self.transfer_mgr.set_progress_callback(self._on_transfer_progress)
                success = self.transfer_mgr.transfer(src, tgt, config)
                msg = "Transferência concluída!" if success else "Transferência concluída com erros."
                self.after(0, lambda: messagebox.showinfo("Transferência", msg))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Erro", str(exc)))
            finally:
                self.after(0, self._transfer_finished)

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # Unsynced app detection for transfer tab
    # ------------------------------------------------------------------
    def _detect_unsynced_apps_transfer(self):
        """Detect unsynced apps on source device for transfer."""
        src_serial = self._get_serial_from_menu(self.transfer_source_menu.get())
        if not src_serial:
            messagebox.showwarning("Detectar", "Selecione o dispositivo de origem primeiro")
            return

        self.btn_detect_unsynced_transfer.configure(state="disabled", text="⏳ ...")
        self._set_status("Escaneando apps no dispositivo de origem...")

        def _run():
            try:
                detector = UnsyncedAppDetector(self.adb)
                detected = detector.detect(src_serial, include_unknown=True)
                self.after(0, lambda: self._on_unsynced_transfer_detected(detected))
            except Exception as exc:
                log.warning("Transfer unsynced detection error: %s", exc)
                self.after(0, lambda: self._set_status(f"Erro: {exc}"))
                self.after(0, lambda: self.btn_detect_unsynced_transfer.configure(
                    state="normal", text="🔎 Detectar",
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _detect_messaging_apps_transfer(self):
        """Detect messaging apps on source device for transfer (uses source dropdown)."""
        src_serial = self._get_serial_from_menu(self.transfer_source_menu.get())
        if not src_serial:
            messagebox.showwarning("Detectar", "Selecione o dispositivo de origem primeiro")
            return

        self._set_status("Detectando apps de mensagem no dispositivo de origem...")

        def _run():
            try:
                detector = MessagingAppDetector(self.adb)
                installed = detector.detect_installed_apps(src_serial)
                self.after(0, lambda: self._on_messaging_transfer_detected(installed))
            except Exception as exc:
                log.warning("Transfer messaging detection error: %s", exc)
                self.after(0, lambda: self._set_status(f"Erro: {exc}"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_messaging_transfer_detected(self, installed: Dict):
        """Update transfer tab messaging checkboxes based on detection."""
        count = len(installed)
        self._set_status(f"{count} app(s) de mensagem detectado(s) no dispositivo de origem")
        for key in self.transfer_msg_vars:
            self.transfer_msg_vars[key].set(key in installed)

    def _on_unsynced_transfer_detected(self, detected: List[DetectedApp]):
        """Render detected unsynced apps in the transfer tab."""
        self._transfer_detected_apps = detected
        self.btn_detect_unsynced_transfer.configure(state="normal", text="🔎 Detectar")

        for w in self.transfer_unsynced_frame.winfo_children():
            w.destroy()
        self.transfer_unsynced_vars.clear()

        if not detected:
            ctk.CTkLabel(
                self.transfer_unsynced_frame,
                text="✅ Nenhum app adicional detectado",
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

        self._set_status(f"{len(detected)} app(s) com dados locais no dispositivo de origem")

    def _clone_device(self):
        """Full clone: copies entire /storage/emulated/0 + apps + contacts + SMS.

        Requires explicit step-by-step confirmation:
        1. User selects source device
        2. User selects destination device
        3. Detailed confirmation dialog with device names and what will be copied
        4. Indexing of source storage
        5. Transfer execution
        """
        src = self._get_serial_from_menu(self.transfer_source_menu.get())
        tgt = self._get_serial_from_menu(self.transfer_target_menu.get())

        # --- Step 1: Validate source ---
        if not src:
            messagebox.showwarning(
                "Clonar Dispositivo",
                "Selecione o dispositivo de ORIGEM no menu acima primeiro.\n\n"
                "Escolha qual aparelho você quer copiar os dados.",
            )
            return

        # --- Step 2: Validate destination ---
        if not tgt:
            messagebox.showwarning(
                "Clonar Dispositivo",
                "Selecione o dispositivo de DESTINO no menu acima.\n\n"
                "Escolha para qual aparelho os dados serão copiados.",
            )
            return

        if src == tgt:
            messagebox.showwarning(
                "Clonar Dispositivo",
                "Origem e destino devem ser aparelhos DIFERENTES.",
            )
            return

        # Get device details for display
        src_dev = self.devices.get(src)
        tgt_dev = self.devices.get(tgt)
        src_name = src_dev.friendly_name() if src_dev else src
        tgt_name = tgt_dev.friendly_name() if tgt_dev else tgt
        src_stor = src_dev.storage_summary() if src_dev else ""
        tgt_stor = tgt_dev.storage_summary() if tgt_dev else ""

        # --- Step 3: Explicit confirmation ---
        confirm = messagebox.askyesno(
            "⚠️ Confirmar Clone Completo",
            f"ATENÇÃO — Verifique os dispositivos com cuidado:\n\n"
            f"📱 ORIGEM (copiar DE):  {src_name}\n"
            f"    Serial: {src}\n"
            f"    Armazenamento: {src_stor or 'N/A'}\n\n"
            f"📱 DESTINO (copiar PARA):  {tgt_name}\n"
            f"    Serial: {tgt}\n"
            f"    Armazenamento: {tgt_stor or 'N/A'}\n\n"
            f"Será copiado do ORIGEM → DESTINO:\n"
            f"  • Toda a memória interna (/storage/emulated/0)\n"
            f"  • Todos os aplicativos instalados\n"
            f"  • Contatos e SMS\n"
            f"  • Apps de mensagem detectados\n\n"
            f"Os dados existentes no DESTINO serão sobrescritos.\n\n"
            f"Tem certeza que quer continuar?",
        )
        if not confirm:
            return

        # Second confirmation (safety net)
        confirm2 = messagebox.askyesno(
            "Confirmação Final",
            f"ÚLTIMA CONFIRMAÇÃO:\n\n"
            f"  {src_name}  ➡️  {tgt_name}\n\n"
            f"Todos os dados da memória interna do {src_name} serão\n"
            f"copiados para o {tgt_name}.\n\n"
            f"Confirma?",
        )
        if not confirm2:
            return

        self.btn_start_transfer.configure(state="disabled")
        self.btn_cancel_transfer.configure(state="normal")
        self._set_status(f"Indexando memória interna de {src_name}...")

        def _run():
            try:
                self.transfer_mgr.set_progress_callback(self._on_transfer_progress)
                success = self.transfer_mgr.clone_full_storage(
                    src, tgt,
                    storage_path="/storage/emulated/0",
                )
                msg = (
                    "✅ Clone completo concluído com sucesso!"
                    if success else
                    "⚠️ Clone concluído com alguns erros. Verifique o log."
                )
                self.after(0, lambda: messagebox.showinfo("Clone", msg))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Erro", str(exc)))
            finally:
                self.after(0, self._transfer_finished)

        threading.Thread(target=_run, daemon=True).start()

    def _on_transfer_progress(self, p: TransferProgress):
        def _update():
            if self._closing:
                return
            try:
                phase_labels = {
                    "initializing": "Inicializando",
                    "indexing": "Indexando arquivos",
                    "backing_up": "Fazendo backup",
                    "restoring": "Restaurando",
                    "verifying": "Verificando integridade",
                    "complete": "Concluído",
                    "complete_with_errors": "Concluído (com erros)",
                    "error": "Erro",
                }
                label = phase_labels.get(p.phase, p.phase)
                self.transfer_progress_label.configure(
                    text=f"{label}: {p.sub_phase} - {p.current_item}"
                )
                self.transfer_progress_bar.set(p.percent / 100)
                detail = ""
                if p.elapsed_seconds > 0:
                    detail = f"Tempo: {format_duration(p.elapsed_seconds)}"
                if p.errors:
                    detail += f"  |  Erros: {len(p.errors)}"
                self.transfer_progress_detail.configure(text=detail)
            except Exception:
                pass
        self._safe_after(0, _update)

    def _cancel_transfer(self):
        self.transfer_mgr.cancel()

    def _transfer_finished(self):
        self.btn_start_transfer.configure(state="normal")
        self.btn_cancel_transfer.configure(state="disabled")

    # ==================================================================
    # Driver operations
    # ==================================================================
    def _check_drivers(self):
        self._set_status("Verificando drivers...")

        def _run():
            status = self.driver_mgr.check_driver_status()
            self.after(0, lambda: self._display_driver_status(status))

        threading.Thread(target=_run, daemon=True).start()

    def _display_driver_status(self, status: DriverStatus):
        self.driver_status_text.configure(state="normal")
        self.driver_status_text.delete("1.0", "end")

        if not status.is_windows:
            self.driver_status_text.insert("end",
                "✅ Drivers USB não são necessários nesta plataforma (Linux/macOS).\n"
                "O ADB funciona nativamente."
            )
        else:
            lines = []
            if status.drivers_installed:
                lines.append("✅ Drivers ADB detectados no sistema")
            else:
                lines.append("❌ Drivers ADB NÃO detectados")

            lines.append(f"\n📱 Dispositivos Android no Device Manager: {status.android_devices_detected}")

            if status.devices_needing_driver:
                lines.append(f"\n⚠️ Dispositivos precisando de driver: {len(status.devices_needing_driver)}")
                for d in status.devices_needing_driver:
                    lines.append(f"   - {d.get('caption', 'Unknown')} ({d.get('device_id', '')})")
            else:
                lines.append("\n✅ Todos os dispositivos com drivers corretos")

            if status.error:
                lines.append(f"\n⚠️ Erro na detecção: {status.error}")

            self.driver_status_text.insert("end", "\n".join(lines))

        self.driver_status_text.configure(state="disabled")
        self._set_status("Verificação de drivers concluída")

    def _install_google_driver(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                "Permissão",
                "É necessário executar como Administrador para instalar drivers.",
            )
            return
        self._run_driver_install(self.driver_mgr.install_google_usb_driver)

    def _install_universal_driver(self):
        self._run_driver_install(self.driver_mgr.install_universal_adb_driver)

    def _auto_install_drivers(self):
        if os.name != "nt":
            self._set_status("Instalação de drivers não necessária nesta plataforma")
            return
        if self._driver_install_running:
            return
        self._run_driver_install(self.driver_mgr.auto_install_drivers)

    def _install_samsung_driver(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                "Permissão",
                "É necessário executar como Administrador para instalar drivers.",
            )
            return
        self._run_driver_install(self.driver_mgr.install_samsung_driver)

    def _install_qualcomm_driver(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                "Permissão",
                "É necessário executar como Administrador para instalar drivers.",
            )
            return
        self._run_driver_install(self.driver_mgr.install_qualcomm_driver)

    def _install_mediatek_driver(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                "Permissão",
                "É necessário executar como Administrador para instalar drivers.",
            )
            return
        self._run_driver_install(self.driver_mgr.install_mediatek_driver)

    def _install_intel_driver(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                "Permissão",
                "É necessário executar como Administrador para instalar drivers.",
            )
            return
        self._run_driver_install(self.driver_mgr.install_intel_driver)

    def _install_all_chipset_drivers(self):
        if not self.driver_mgr.is_admin():
            messagebox.showwarning(
                "Permissão",
                "É necessário executar como Administrador para instalar drivers.",
            )
            return
        self._run_driver_install(self.driver_mgr.install_all_chipset_drivers)

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
                        "Driver instalado com sucesso!"
                    ))
                    # Stop monitor, restart ADB, then resume monitor
                    self.adb.stop_device_monitor()
                    if self.adb.adb_path:
                        self.driver_mgr.restart_adb_after_driver(self.adb.adb_path)
                    self.adb.start_device_monitor()
                    self._safe_after(2000, self._refresh_devices)
                else:
                    self._safe_after(0, lambda: self._set_status(
                        "Instalação do driver falhou ou foi cancelada."
                    ))
            except Exception as exc:
                log.exception("Driver install thread error: %s", exc)
            finally:
                self._driver_install_running = False

        threading.Thread(target=_run, daemon=True).start()

    # ==================================================================
    # Settings operations
    # ==================================================================
    def _browse_adb(self):
        path = filedialog.askopenfilename(
            title="Selecionar ADB",
            filetypes=[("Executável", "*.exe"), ("Todos", "*.*")],
        )
        if path:
            self.entry_adb_path.delete(0, "end")
            self.entry_adb_path.insert(0, path)

    def _browse_backup_dir(self):
        path = filedialog.askdirectory(title="Selecionar Pasta de Backups")
        if path:
            self.entry_backup_dir.delete(0, "end")
            self.entry_backup_dir.insert(0, path)

    def _download_platform_tools(self):
        self._set_status("Baixando Platform Tools...")

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
        self.config.set("acceleration.gpu_enabled", gpu_on)
        self.config.set("acceleration.multi_gpu", self.settings_multigpu_var.get())
        self.config.set("acceleration.verify_checksums", self.settings_verify_var.get())
        self.config.set("acceleration.checksum_algo", self.settings_algo_var.get())
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
        accel.set_virt_enabled(self.settings_virt_var.get())
        accel.verify_checksums = self.settings_verify_var.get()
        accel.checksum_algo = self.settings_algo_var.get()
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
        self._virt_toggle_var.set(self.settings_virt_var.get())
        threading.Thread(target=self._init_accel_footer, daemon=True).start()

        self._set_status("Configurações salvas")
        messagebox.showinfo("Configurações", "Configurações salvas com sucesso!")

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
                    text=f"Erro ao detectar GPUs: {exc}",
                ),
            )

    # ==================================================================
    # Helpers
    # ==================================================================
    def _get_selected_device(self) -> Optional[str]:
        """Get a device serial, asking user if multiple are connected."""
        if not self.devices:
            messagebox.showwarning(
                "Dispositivo",
                "Nenhum dispositivo conectado.\n"
                "Conecte um dispositivo Android com Depuração USB ativada.",
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
