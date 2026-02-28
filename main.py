#!/usr/bin/env python3
"""
ADB Toolkit - Backup, Recovery & Transfer
Main entry point.
"""

import sys
import argparse
import logging
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.adb_core import ADBCore
from src.config import Config
from src.log_setup import setup_logging


def main():
    parser = argparse.ArgumentParser(
        description="ADB Toolkit — Backup, Recovery & Transfer de dispositivos Android",
    )
    parser.add_argument(
        "--cli", action="store_true",
        help="Modo linha de comando (sem GUI)",
    )
    parser.add_argument(
        "--backup", metavar="SERIAL",
        help="Iniciar backup do dispositivo especificado",
    )
    parser.add_argument(
        "--restore", nargs=2, metavar=("SERIAL", "BACKUP_ID"),
        help="Restaurar backup para dispositivo",
    )
    parser.add_argument(
        "--transfer", nargs=2, metavar=("SOURCE", "TARGET"),
        help="Transferir dados entre dispositivos",
    )
    parser.add_argument(
        "--list-devices", action="store_true",
        help="Listar dispositivos conectados",
    )
    parser.add_argument(
        "--list-backups", action="store_true",
        help="Listar backups disponíveis",
    )
    parser.add_argument(
        "--install-drivers", action="store_true",
        help="Instalar drivers USB automaticamente",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Logs detalhados",
    )
    args = parser.parse_args()

    # Setup
    level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level=level)
    log = logging.getLogger("adb_toolkit")

    config = Config()
    adb = ADBCore(ROOT)

    # Ensure ADB is available
    try:
        adb.ensure_adb()
    except Exception as exc:
        log.error("Falha ao localizar/baixar ADB: %s", exc)
        print(f"ERRO: Não foi possível encontrar ou baixar o ADB: {exc}")
        print("Baixe manualmente de: https://developer.android.com/studio/releases/platform-tools")
        sys.exit(1)

    # CLI mode
    if args.list_devices:
        devices = adb.list_devices()
        if not devices:
            print("Nenhum dispositivo conectado.")
        else:
            print(f"\n{'Serial':<25} {'Estado':<15} {'Modelo':<20}")
            print("-" * 60)
            for d in devices:
                info = adb.get_device_details(d.serial)
                print(f"{d.serial:<25} {d.state:<15} {info.friendly_name():<20}")
        return

    if args.list_backups:
        from src.backup_manager import BackupManager
        from src.utils import format_bytes
        mgr = BackupManager(adb)
        backups = mgr.list_backups()
        if not backups:
            print("Nenhum backup encontrado.")
        else:
            print(f"\n{'ID':<45} {'Tipo':<10} {'Tamanho':<12} {'Data':<12}")
            print("-" * 80)
            for b in backups:
                print(f"{b.backup_id:<45} {b.backup_type:<10} "
                      f"{format_bytes(b.size_bytes):<12} {b.timestamp[:10]:<12}")
        return

    if args.install_drivers:
        from src.driver_manager import DriverManager
        dm = DriverManager(ROOT)
        print("Verificando e instalando drivers...")

        def _progress(msg, pct):
            print(f"  [{pct:3d}%] {msg}")

        if dm.auto_install_drivers(progress_cb=_progress):
            print("✅ Drivers instalados com sucesso!")
        else:
            print("❌ Falha na instalação de drivers.")
        return

    if args.backup:
        from src.backup_manager import BackupManager
        from src.utils import format_bytes
        mgr = BackupManager(adb)

        def _progress(p):
            if p.items_total:
                print(f"\r  [{p.percent:5.1f}%] {p.phase}: {p.current_item} "
                      f"({p.items_done}/{p.items_total})", end="", flush=True)

        mgr.set_progress_callback(_progress)
        print(f"Iniciando backup de {args.backup}...")
        manifests = mgr.backup_comprehensive(args.backup)
        print()
        for m in manifests:
            print(f"  ✅ {m.backup_type}: {m.backup_id} ({format_bytes(m.size_bytes)})")
        return

    if args.restore:
        serial, backup_id = args.restore
        from src.restore_manager import RestoreManager
        mgr = RestoreManager(adb)

        def _progress(p):
            print(f"\r  [{p.percent:5.1f}%] {p.phase}: {p.current_item}", end="", flush=True)

        mgr.set_progress_callback(_progress)
        print(f"Restaurando {backup_id} para {serial}...")
        success = mgr.restore_smart(serial, backup_id)
        print()
        print("✅ Restauração concluída!" if success else "❌ Restauração falhou.")
        return

    if args.transfer:
        source, target = args.transfer
        from src.transfer_manager import TransferManager, TransferConfig
        mgr = TransferManager(adb)

        def _progress(p):
            print(f"\r  [{p.percent:5.1f}%] {p.phase}/{p.sub_phase}: {p.current_item}",
                  end="", flush=True)

        mgr.set_progress_callback(_progress)
        print(f"Transferindo dados: {source} → {target}...")
        config_t = TransferConfig()
        success = mgr.transfer(source, target, config_t)
        print()
        print("✅ Transferência concluída!" if success else "❌ Transferência falhou.")
        return

    # GUI mode (default)
    if args.cli:
        parser.print_help()
        return

    try:
        from src.gui import run_gui
        run_gui(config, adb)
    except ImportError as exc:
        log.error("Falha ao iniciar GUI: %s", exc)
        print("ERRO: customtkinter é necessário para a interface gráfica.")
        print("Instale com: pip install customtkinter")
        sys.exit(1)


if __name__ == "__main__":
    main()
