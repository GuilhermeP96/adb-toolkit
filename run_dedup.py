#!/usr/bin/env python3
"""
run_dedup.py — Find & remove duplicate files on a connected Android device.

Usage:
    python run_dedup.py                   # scan WhatsApp + media, auto-detect device
    python run_dedup.py --dry-run         # preview only (no deletes)
    python run_dedup.py -s SERIAL         # target specific device
    python run_dedup.py --roots /sdcard/DCIM /sdcard/Download  # custom scan targets
    python run_dedup.py --whatsapp-only   # scan only WhatsApp media
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.adb_core import ADBCore
from src.dedup_cleaner import DedupCleaner, DEFAULT_SCAN_ROOTS


def _progress(msg: str, pct: float):
    bar_len = 30
    filled = int(bar_len * pct / 100)
    bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
    print(f"\r  [{bar}] {pct:5.1f}%  {msg:<72}", end="", flush=True)


_WA_ONLY_ROOTS = [
    "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media",
    "/storage/emulated/0/WhatsApp/Media",
    "/storage/emulated/0/Android/media/com.whatsapp.w4b/WhatsApp Business/Media",
    "/storage/emulated/0/WhatsApp Business/Media",
]


def main():
    parser = argparse.ArgumentParser(
        description="Detectar e remover arquivos duplicados em dispositivo Android via ADB",
    )
    parser.add_argument("-s", "--serial", help="Serial do dispositivo (auto-detect se omitido)")
    parser.add_argument("--dry-run", action="store_true", help="Apenas listar - nao apagar nada")
    parser.add_argument("--whatsapp-only", action="store_true", help="Escanear apenas midias do WhatsApp")
    parser.add_argument("--roots", nargs="+", metavar="PATH", help="Diretorios remotos para escanear")
    parser.add_argument("--min-size", type=int, default=1024, help="Tamanho minimo em bytes (default: 1024)")
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
        print("\u274c  ADB nao encontrado. Verifique se esta em PATH ou em platform-tools/")
        sys.exit(1)

    devices = adb.list_devices()
    online = [d for d in devices if d.state == "device"]
    if not online:
        print("\u274c  Nenhum dispositivo Android conectado (state=device).")
        sys.exit(1)

    if args.serial:
        matches = [d for d in online if d.serial == args.serial]
        if not matches:
            print(f"\u274c  Dispositivo '{args.serial}' nao encontrado entre: "
                  f"{[d.serial for d in online]}")
            sys.exit(1)
        dev = matches[0]
    else:
        dev = online[0]
        if len(online) > 1:
            print(f"\u26a0\ufe0f  Multiplos dispositivos. Usando o primeiro: {dev.serial}")

    detail = adb.get_device_details(dev.serial)
    print(f"\U0001f4f1  Dispositivo: {detail.friendly_name()}  ({dev.serial})")
    print(f"    Android {detail.android_version}  |  "
          f"Bateria {detail.battery_level}%  |  "
          f"{detail.storage_summary()}")
    print()

    # Choose scan roots
    if args.roots:
        roots = args.roots
    elif args.whatsapp_only:
        roots = _WA_ONLY_ROOTS
    else:
        roots = DEFAULT_SCAN_ROOTS

    if args.dry_run:
        print("\U0001f50d  Modo DRY-RUN - nada sera apagado.\n")
    else:
        print("\U0001f9f9  Iniciando deteccao de duplicatas...\n")

    print("  Diretorios a escanear:")
    for r in roots:
        print(f"    - {r}")
    print()

    cleaner = DedupCleaner(adb, dev.serial)
    cleaner.set_progress_callback(_progress)

    result = cleaner.run(
        scan_roots=roots,
        dry_run=args.dry_run,
        min_size=args.min_size,
    )

    # Report
    print("\n\n")
    print("=" * 72)
    print("  RELATORIO DE DEDUPLICACAO")
    print("=" * 72)
    print(f"  Arquivos escaneados    : {result.files_scanned}")
    print(f"  Grupos por tamanho     : {result.size_groups}")
    print(f"  Grupos hash parcial    : {result.partial_hash_groups}")
    print(f"  Grupos SHA-256 full    : {result.full_hash_groups}")
    print(f"  Grupos confirmados     : {result.confirmed_dup_groups}")
    print(f"  Duplicatas encontradas : {result.duplicates_found}")
    print(f"  Duplicatas removidas   : {result.duplicates_removed}")
    print(f"  Espaco liberado (est)  : ~{_fmt(result.bytes_freed)}")
    if result.errors:
        print(f"\n  \u26a0\ufe0f  Erros: {len(result.errors)}")
        for e in result.errors[:10]:
            print(f"      \u2022 {e}")
    print("=" * 72)

    if args.verbose:
        print("\n--- Pipeline stats ---")
        print(f"  Stage 1 (size groups)      : {result.size_groups}")
        print(f"  Stage 2 (partial hash)     : {result.partial_hash_groups}")
        print(f"  Stage 3 (full SHA-256)     : {result.full_hash_groups}")
        print(f"  Stage 4 (byte spot-check)  : {result.confirmed_dup_groups}")
        print(f"  Stage 5 (deleted)          : {result.duplicates_removed}")

        # Show kept originals
        if result.kept_originals:
            print(f"\n--- Originais mantidos ({len(result.kept_originals)}) ---")
            for orig in result.kept_originals[:50]:
                print(f"  KEPT  {orig}")
            if len(result.kept_originals) > 50:
                print(f"  ... e mais {len(result.kept_originals) - 50}")

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
