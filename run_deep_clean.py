#!/usr/bin/env python3
"""
run_deep_clean.py — Execute a deep cleanup on a connected Android device.

Usage:
    python run_deep_clean.py              # auto-detect device, execute
    python run_deep_clean.py --dry-run    # preview only (no deletions)
    python run_deep_clean.py -s SERIAL    # target specific device
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.adb_core import ADBCore
from src.deep_cleaner import DeepCleaner


def _progress(msg: str, pct: float):
    bar_len = 30
    filled = int(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\r  [{bar}] {pct:5.1f}%  {msg:<70}", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Deep cleanup de dispositivo Android via ADB")
    parser.add_argument("-s", "--serial", help="Serial do dispositivo (auto-detect se omitido)")
    parser.add_argument("--dry-run", action="store_true", help="Apenas listar — não apagar nada")
    parser.add_argument("-v", "--verbose", action="store_true", help="Logs detalhados")
    args = parser.parse_args()

    import logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s  %(name)s  %(message)s",
    )

    adb = ADBCore()
    adb_path = adb.find_adb()
    if not adb_path:
        print("❌  ADB não encontrado. Verifique se está em PATH ou em platform-tools/")
        sys.exit(1)

    devices = adb.list_devices()
    online = [d for d in devices if d.state == "device"]

    if not online:
        print("❌  Nenhum dispositivo Android conectado (state=device).")
        sys.exit(1)

    if args.serial:
        matches = [d for d in online if d.serial == args.serial]
        if not matches:
            print(f"❌  Dispositivo '{args.serial}' não encontrado entre: "
                  f"{[d.serial for d in online]}")
            sys.exit(1)
        dev = matches[0]
    else:
        dev = online[0]
        if len(online) > 1:
            print(f"⚠️  Múltiplos dispositivos. Usando o primeiro: {dev.serial}")
            print(f"   (use -s SERIAL para escolher)")

    detail = adb.get_device_details(dev.serial)
    print(f"📱  Dispositivo: {detail.friendly_name()}  ({dev.serial})")
    print(f"    Android {detail.android_version}  |  "
          f"Bateria {detail.battery_level}%  |  "
          f"{detail.storage_summary()}")
    print()

    if args.dry_run:
        print("🔍  Modo DRY-RUN — nada será apagado.\n")
    else:
        print("🧹  Iniciando limpeza profunda…\n")

    cleaner = DeepCleaner(adb, dev.serial)
    cleaner.set_progress_callback(_progress)

    result = cleaner.run(dry_run=args.dry_run)

    # Final report
    print("\n")
    print("=" * 72)
    print("  RELATÓRIO DA LIMPEZA")
    print("=" * 72)
    for line in result.details[-1:]:
        print(f"  ✅  {line}")
    print(f"\n  Diretórios removidos : {result.dirs_removed}")
    print(f"  Arquivos removidos   : {result.files_removed}")
    print(f"  Órfãos removidos     : {result.orphans_removed}")
    print(f"  Espaço liberado (est): ~{_fmt(result.bytes_freed)}")
    if result.errors:
        print(f"\n  ⚠️  Erros: {len(result.errors)}")
        for e in result.errors[:10]:
            print(f"      • {e}")
    print("=" * 72)

    if args.verbose:
        print("\n--- Detalhes completos ---")
        for d in result.details:
            print(f"  {d}")


def _fmt(size: int) -> str:
    if size <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


if __name__ == "__main__":
    main()
