#!/usr/bin/env python3
"""
WhatsApp Video Deduplication v2 — ADB Toolkit
===============================================
Deep analysis with multiple algorithms:

  Phase 1: Inventory — file size + mtime via stat, duration via MediaStore
  Phase 2: Exact duplicates — size grouping → partial hash → full SHA-256
  Phase 3: Near-duplicates — same duration (±500ms), similar size (±1%),
           then content hash skipping MP4 container header (first 4KB)
  Phase 4: SHA-256 cross-verification of all candidates

Keeper strategy: ALWAYS keep the oldest file (by filesystem mtime).
"""

import subprocess
import sys
import os
import re
import json
import time
from collections import defaultdict
from datetime import datetime

# ─── Configuration ───────────────────────────────────────────────────
ADB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform-tools", "adb.exe")
WA_VIDEO_DIR = "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Video"
PARTIAL_HASH_BYTES = 65536       # 64 KB for partial hash
CONTAINER_SKIP_BYTES = 4096      # Skip first 4KB (ftyp+moov header) for content comparison
NEAR_SIZE_TOLERANCE = 0.01       # ±1% size for near-duplicate grouping
NEAR_DURATION_MS = 500           # ±500ms duration tolerance
BATCH_SIZE = 60                  # files per ADB shell call
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toolbox_output")


# ═════════════════════════════════════════════════════════════════════
#  ADB helpers
# ═════════════════════════════════════════════════════════════════════

def adb_shell(cmd: str, timeout: int = 300) -> str:
    """Run a single ADB shell command and return stdout."""
    result = subprocess.run(
        [ADB, "shell", cmd],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace"
    )
    return result.stdout.strip()


def adb_shell_lines(cmd: str, timeout: int = 600) -> list[str]:
    """Run ADB shell command, return non-empty lines."""
    out = adb_shell(cmd, timeout)
    return [l for l in out.splitlines() if l.strip()]


# ═════════════════════════════════════════════════════════════════════
#  Data structures
# ═════════════════════════════════════════════════════════════════════

class FileInfo:
    __slots__ = ("path", "size", "mtime", "duration_ms",
                 "partial_hash", "full_hash", "content_hash")

    def __init__(self, path: str, size: int, mtime: int):
        self.path = path
        self.size = size
        self.mtime = mtime          # epoch seconds
        self.duration_ms: int = 0   # from MediaStore
        self.partial_hash: str = ""
        self.full_hash: str = ""    # SHA-256
        self.content_hash: str = "" # hash skipping container header


# ═════════════════════════════════════════════════════════════════════
#  Phase 1: Full inventory
# ═════════════════════════════════════════════════════════════════════

def phase1_inventory() -> list[FileInfo]:
    """Collect path, size, mtime for every video file + duration from MediaStore."""
    print("\n═══ FASE 1: Inventário completo ═══")

    # 1a: stat all files — SIZE|MTIME|PATH
    print("  [1a] Listando arquivos com stat (tamanho + data)...")
    cmd = (
        f'find "{WA_VIDEO_DIR}" -type f -name "*.mp4" '
        f'-exec stat -c "%s|%Y|%n" {{}} + 2>/dev/null'
    )
    lines = adb_shell_lines(cmd, timeout=120)

    files: dict[str, FileInfo] = {}
    for line in lines:
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        try:
            size = int(parts[0])
            mtime = int(parts[1])
        except ValueError:
            continue
        path = parts[2]
        files[path] = FileInfo(path, size, mtime)

    total_bytes = sum(f.size for f in files.values())
    print(f"  Total: {len(files)} arquivos ({format_bytes(total_bytes)})")

    # 1b: Duration from MediaStore
    print("  [1b] Extraindo duração via MediaStore...")
    ms_cmd = (
        "content query --uri content://media/external/video/media "
        "--projection 'duration:_data:_size'"
    )
    ms_lines = adb_shell_lines(ms_cmd, timeout=120)

    duration_count = 0
    # Parse: Row: N duration=XXXX, _data=/path, _size=YYYY
    pat = re.compile(
        r"duration=(\d+),\s*_data=([^,]+?)(?:,\s*_size=(\d+))?$"
    )
    for line in ms_lines:
        m = pat.search(line)
        if not m:
            continue
        dur = int(m.group(1))
        data_path = m.group(2).strip()
        # MediaStore uses /storage/emulated/0, stat uses /sdcard
        norm = data_path.replace("/storage/emulated/0/", "/sdcard/")
        if norm in files:
            files[norm].duration_ms = dur
            duration_count += 1

    print(f"  Durações obtidas: {duration_count}/{len(files)}")

    # Summary
    with_dur = sum(1 for f in files.values() if f.duration_ms > 0)
    oldest = min(files.values(), key=lambda f: f.mtime)
    newest = max(files.values(), key=lambda f: f.mtime)
    print(f"  Arquivo mais antigo: {datetime.fromtimestamp(oldest.mtime).strftime('%Y-%m-%d')} — {os.path.basename(oldest.path)}")
    print(f"  Arquivo mais recente: {datetime.fromtimestamp(newest.mtime).strftime('%Y-%m-%d')} — {os.path.basename(newest.path)}")

    return list(files.values())


# ═════════════════════════════════════════════════════════════════════
#  Phase 2: Exact duplicates (size → partial hash → full SHA-256)
# ═════════════════════════════════════════════════════════════════════

def phase2_exact_duplicates(all_files: list[FileInfo]) -> dict[str, list[FileInfo]]:
    """Find exact byte-for-byte duplicates using 3-tier hash filtering."""
    print("\n═══ FASE 2: Duplicatas exatas ═══")

    # 2a: Group by exact size
    print("  [2a] Agrupando por tamanho exato...")
    size_groups: dict[int, list[FileInfo]] = defaultdict(list)
    for f in all_files:
        size_groups[f.size].append(f)

    candidates = {s: g for s, g in size_groups.items() if len(g) > 1}
    cand_files = sum(len(g) for g in candidates.values())
    print(f"       {len(candidates)} grupos com mesmo tamanho ({cand_files} arquivos)")

    if not candidates:
        return {}

    # 2b: Partial hash (first 64KB + last 64KB)
    print("  [2b] Hash parcial (64KB início + 64KB fim)...")
    cand_list = [f for g in candidates.values() for f in g]
    _compute_partial_hashes(cand_list)

    # Re-group by size + partial_hash
    combo: dict[str, list[FileInfo]] = defaultdict(list)
    for f in cand_list:
        key = f"{f.size}|{f.partial_hash}" if f.partial_hash else f"unique|{id(f)}"
        combo[key].append(f)
    combo_dupes = {k: g for k, g in combo.items() if len(g) > 1}
    combo_files = sum(len(g) for g in combo_dupes.values())
    print(f"       {len(combo_dupes)} grupos após hash parcial ({combo_files} arquivos)")

    if not combo_dupes:
        return {}

    # 2c: Full SHA-256
    print("  [2c] SHA-256 completo (confirmação final)...")
    final_list = [f for g in combo_dupes.values() for f in g]
    _compute_full_hashes(final_list)

    # Group by full hash
    hash_groups: dict[str, list[FileInfo]] = defaultdict(list)
    for f in final_list:
        if f.full_hash:
            hash_groups[f.full_hash].append(f)

    exact_dupes = {h: g for h, g in hash_groups.items() if len(g) > 1}
    exact_files = sum(len(g) for g in exact_dupes.values())
    removable = exact_files - len(exact_dupes)
    print(f"  ✓ Duplicatas exatas confirmadas: {len(exact_dupes)} grupos ({exact_files} arquivos, {removable} removíveis)")

    return exact_dupes


# ═════════════════════════════════════════════════════════════════════
#  Phase 3: Near-duplicates (duration + content hash sans header)
# ═════════════════════════════════════════════════════════════════════

def phase3_near_duplicates(
    all_files: list[FileInfo],
    already_found: set[str]
) -> dict[str, list[FileInfo]]:
    """
    Find near-duplicates: same video content re-wrapped by WhatsApp.
    Groups by similar duration (±500ms) + similar size (±1%),
    then compares content hash (skipping 4KB MP4 header).
    """
    print("\n═══ FASE 3: Near-duplicates (mesmo conteúdo, container diferente) ═══")

    # Only check files with known duration, excluding already-found exact dupes
    candidates = [
        f for f in all_files
        if f.duration_ms > 0 and f.path not in already_found
    ]
    print(f"  Candidatos com duração conhecida (excl. duplicatas exatas): {len(candidates)}")

    if len(candidates) < 2:
        print("  Poucos candidatos para análise de near-duplicates.")
        return {}

    # 3a: Group by duration bucket (500ms granularity)
    print("  [3a] Agrupando por duração similar (±500ms)...")
    dur_groups: dict[int, list[FileInfo]] = defaultdict(list)
    for f in candidates:
        bucket = f.duration_ms // NEAR_DURATION_MS
        dur_groups[bucket].append(f)
        # Also add to adjacent bucket for edge cases
        dur_groups[bucket + 1].append(f)

    # Deduplicate: for each pair of files in same bucket, check size tolerance
    print("  [3b] Filtrando por tamanho similar (±1%)...")
    near_candidates: dict[str, list[FileInfo]] = defaultdict(list)
    seen_pairs: set[tuple[str, str]] = set()

    for bucket, group in dur_groups.items():
        if len(group) < 2:
            continue
        # Sort by size for efficient comparison
        group.sort(key=lambda f: f.size)
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                fi, fj = group[i], group[j]
                if fi.path == fj.path:
                    continue
                pair_key = tuple(sorted([fi.path, fj.path]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                # Check size tolerance
                if fi.size == fj.size:
                    continue  # Exact same size — already handled in Phase 2
                size_ratio = abs(fi.size - fj.size) / max(fi.size, fj.size)
                if size_ratio > NEAR_SIZE_TOLERANCE:
                    continue
                # Check duration tolerance
                if abs(fi.duration_ms - fj.duration_ms) > NEAR_DURATION_MS:
                    continue

                # These are near-duplicate candidates
                # Group them by a canonical key (smaller path)
                canon = min(fi.path, fj.path)
                if canon not in near_candidates or fi not in near_candidates[canon]:
                    near_candidates[canon] = []
                if fi not in near_candidates[canon]:
                    near_candidates[canon].append(fi)
                if fj not in near_candidates[canon]:
                    near_candidates[canon].append(fj)

    cand_groups = {k: g for k, g in near_candidates.items() if len(g) > 1}
    cand_files = sum(len(g) for g in cand_groups.values())
    print(f"       {len(cand_groups)} grupos candidatos ({cand_files} arquivos)")

    if not cand_groups:
        return {}

    # 3c: Content hash — skip first 4KB (container header), hash next 256KB
    print("  [3c] Hash de conteúdo (skip header 4KB, hash 256KB corpo)...")
    all_near_files = list({f.path: f for g in cand_groups.values() for f in g}.values())
    _compute_content_hashes(all_near_files)

    # Re-group by content hash
    content_groups: dict[str, list[FileInfo]] = defaultdict(list)
    for f in all_near_files:
        if f.content_hash:
            content_groups[f.content_hash].append(f)

    near_dupes = {h: g for h, g in content_groups.items() if len(g) > 1}
    near_files = sum(len(g) for g in near_dupes.values())

    if near_dupes:
        removable = near_files - len(near_dupes)
        near_bytes = sum(
            min(f.size for f in g) * (len(g) - 1) for g in near_dupes.values()
        )
        print(f"  ✓ Near-duplicates confirmados: {len(near_dupes)} grupos ({near_files} arquivos, {removable} removíveis)")
        print(f"    Economia potencial: ~{format_bytes(near_bytes)}")
    else:
        print("  Nenhum near-duplicate encontrado.")

    return near_dupes


# ═════════════════════════════════════════════════════════════════════
#  Hash computation helpers
# ═════════════════════════════════════════════════════════════════════

def _compute_partial_hashes(files: list[FileInfo]):
    """Compute partial hash (first 64KB + last 64KB) via md5sum on device."""
    total = len(files)
    done = 0
    size_map = {f.path: f.size for f in files}

    for i in range(0, total, BATCH_SIZE):
        batch = files[i:i + BATCH_SIZE]
        cmds = []
        for f in batch:
            escaped = f.path.replace("'", "'\\''")
            if f.size <= PARTIAL_HASH_BYTES * 2:
                cmds.append(f"md5sum '{escaped}' 2>/dev/null")
            else:
                blocks = PARTIAL_HASH_BYTES // 512
                skip_end = (f.size - PARTIAL_HASH_BYTES) // 512
                cmds.append(
                    f"{{ dd if='{escaped}' bs=512 count={blocks} 2>/dev/null; "
                    f"dd if='{escaped}' bs=512 skip={skip_end} 2>/dev/null; }} | md5sum 2>/dev/null"
                )

        combined = " ; echo '|||' ; ".join(cmds)
        output = adb_shell(combined, timeout=300)
        results = output.split("|||")

        for j, f in enumerate(batch):
            if j < len(results):
                line = results[j].strip()
                if line:
                    h = line.split()[0] if line.split() else ""
                    if len(h) == 32:
                        f.partial_hash = h

        done += len(batch)
        sys.stdout.write(f"\r       Progresso: {done}/{total} ({done*100//total}%)")
        sys.stdout.flush()
    print()


def _compute_full_hashes(files: list[FileInfo]):
    """Compute full SHA-256 hash on device."""
    total = len(files)
    done = 0

    for i in range(0, total, BATCH_SIZE):
        batch = files[i:i + BATCH_SIZE]
        cmds = []
        for f in batch:
            escaped = f.path.replace("'", "'\\''")
            cmds.append(f"sha256sum '{escaped}' 2>/dev/null")

        combined = " ; ".join(cmds)
        output = adb_shell(combined, timeout=600)

        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 64:
                h, fpath = parts[0], parts[1]
                for f in batch:
                    if f.path == fpath:
                        f.full_hash = h
                        break

        done += len(batch)
        sys.stdout.write(f"\r       Progresso: {done}/{total} ({done*100//total}%)")
        sys.stdout.flush()
    print()


def _compute_content_hashes(files: list[FileInfo]):
    """Hash video content skipping the MP4 container header (first 4KB)."""
    total = len(files)
    done = 0
    hash_bytes = 262144  # 256KB of content after header

    for i in range(0, total, BATCH_SIZE):
        batch = files[i:i + BATCH_SIZE]
        cmds = []
        for f in batch:
            escaped = f.path.replace("'", "'\\''")
            skip_blocks = CONTAINER_SKIP_BYTES // 512  # 8 blocks
            count_blocks = hash_bytes // 512            # 512 blocks
            cmds.append(
                f"dd if='{escaped}' bs=512 skip={skip_blocks} count={count_blocks} 2>/dev/null | sha256sum 2>/dev/null"
            )

        combined = " ; echo '|||' ; ".join(cmds)
        output = adb_shell(combined, timeout=300)
        results = output.split("|||")

        for j, f in enumerate(batch):
            if j < len(results):
                line = results[j].strip()
                if line:
                    h = line.split()[0] if line.split() else ""
                    if len(h) == 64:
                        f.content_hash = h

        done += len(batch)
        sys.stdout.write(f"\r       Progresso: {done}/{total} ({done*100//total}%)")
        sys.stdout.flush()
    print()


# ═════════════════════════════════════════════════════════════════════
#  Keeper selection — ALWAYS keep the OLDEST (lowest mtime)
# ═════════════════════════════════════════════════════════════════════

def choose_keeper(files: list[FileInfo]) -> tuple[FileInfo, list[FileInfo]]:
    """
    Keep the oldest file (smallest mtime = earliest filesystem date).
    Tiebreaker: prefer received (root dir) over Sent.
    """
    def sort_key(f: FileInfo):
        is_sent = 1 if "/Sent/" in f.path else 0
        is_private = 2 if "/Private/" in f.path else 0
        return (f.mtime, is_sent + is_private, f.path)

    sorted_files = sorted(files, key=sort_key)
    return sorted_files[0], sorted_files[1:]


# ═════════════════════════════════════════════════════════════════════
#  Report generation
# ═════════════════════════════════════════════════════════════════════

def generate_report(
    exact_dupes: dict[str, list[FileInfo]],
    near_dupes: dict[str, list[FileInfo]],
) -> str:
    """Generate comprehensive human-readable report."""
    lines = []
    lines.append("=" * 90)
    lines.append("  RELATÓRIO DE DUPLICATAS — WhatsApp Video v2 (Deep Analysis)")
    lines.append(f"  Gerado em: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("  Algoritmos: Size grouping → Partial MD5 → Full SHA-256 → Content Hash")
    lines.append("  Critério de keeper: MAIS ANTIGO (por mtime do filesystem)")
    lines.append("=" * 90)

    # ── Exact duplicates ──
    total_exact_removable = 0
    total_exact_bytes = 0
    exact_details = []

    for h, group in exact_dupes.items():
        keeper, remove = choose_keeper(group)
        removable_bytes = keeper.size * len(remove)
        total_exact_removable += len(remove)
        total_exact_bytes += removable_bytes
        exact_details.append((h, keeper, remove, removable_bytes))

    exact_details.sort(key=lambda x: -x[3])

    lines.append("")
    lines.append("┌─────────────────────────────────────────────────────────────────────────────────┐")
    lines.append("│  SEÇÃO A: DUPLICATAS EXATAS (SHA-256 idêntico — 100% seguro remover)            │")
    lines.append("└─────────────────────────────────────────────────────────────────────────────────┘")
    lines.append(f"  • Grupos: {len(exact_dupes)}")
    lines.append(f"  • Removíveis: {total_exact_removable} arquivos")
    lines.append(f"  • Economia: {format_bytes(total_exact_bytes)}")
    lines.append("")

    for idx, (h, keeper, remove, removable_bytes) in enumerate(exact_details, 1):
        mtime_str = datetime.fromtimestamp(keeper.mtime).strftime("%Y-%m-%d %H:%M")
        dur_str = format_duration(keeper.duration_ms) if keeper.duration_ms else "?"
        lines.append(f"  ─── Grupo #{idx}  |  SHA-256: {h[:16]}…  |  {format_bytes(keeper.size)}  |  Duração: {dur_str}  |  {len(remove)+1} cópias")
        lines.append(f"  ✓ MANTER:  {keeper.path}")
        lines.append(f"             mtime: {mtime_str}")
        for r in remove:
            r_mtime = datetime.fromtimestamp(r.mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  ✗ REMOVER: {r.path}")
            lines.append(f"             mtime: {r_mtime}")
        lines.append(f"  → Economia: {format_bytes(removable_bytes)}")
        lines.append("")

    # ── Near-duplicates ──
    total_near_removable = 0
    total_near_bytes = 0
    near_details = []

    for h, group in near_dupes.items():
        keeper, remove = choose_keeper(group)
        removable_bytes = sum(r.size for r in remove)
        total_near_removable += len(remove)
        total_near_bytes += removable_bytes
        near_details.append((h, keeper, remove, removable_bytes))

    near_details.sort(key=lambda x: -x[3])

    lines.append("┌─────────────────────────────────────────────────────────────────────────────────┐")
    lines.append("│  SEÇÃO B: NEAR-DUPLICATES (mesmo conteúdo, container MP4 diferente)             │")
    lines.append("│  ⚠️  Revisar manualmente antes de remover                                       │")
    lines.append("└─────────────────────────────────────────────────────────────────────────────────┘")
    lines.append(f"  • Grupos: {len(near_dupes)}")
    lines.append(f"  • Removíveis: {total_near_removable} arquivos")
    lines.append(f"  • Economia: {format_bytes(total_near_bytes)}")
    lines.append("")

    for idx, (h, keeper, remove, removable_bytes) in enumerate(near_details, 1):
        mtime_str = datetime.fromtimestamp(keeper.mtime).strftime("%Y-%m-%d %H:%M")
        dur_str = format_duration(keeper.duration_ms)
        lines.append(f"  ─── Near-Grupo #{idx}  |  Content: {h[:16]}…  |  Duração: {dur_str}")
        lines.append(f"  ✓ MANTER:  {keeper.path}")
        lines.append(f"             {format_bytes(keeper.size)}  |  mtime: {mtime_str}")
        for r in remove:
            r_mtime = datetime.fromtimestamp(r.mtime).strftime("%Y-%m-%d %H:%M")
            r_dur = format_duration(r.duration_ms)
            size_diff = abs(r.size - keeper.size) * 100 / max(keeper.size, 1)
            lines.append(f"  ✗ REMOVER: {r.path}")
            lines.append(f"             {format_bytes(r.size)} (Δ{size_diff:.1f}%)  |  Duração: {r_dur}  |  mtime: {r_mtime}")
        lines.append(f"  → Economia: {format_bytes(removable_bytes)}")
        lines.append("")

    # ── Grand total ──
    grand_removable = total_exact_removable + total_near_removable
    grand_bytes = total_exact_bytes + total_near_bytes
    lines.append("=" * 90)
    lines.append(f"  TOTAL GERAL:")
    lines.append(f"  • Exatas:         {total_exact_removable} arquivos = {format_bytes(total_exact_bytes)}")
    lines.append(f"  • Near-dupes:     {total_near_removable} arquivos = {format_bytes(total_near_bytes)}")
    lines.append(f"  • TOTAL REMOVÍVEL: {grand_removable} arquivos = {format_bytes(grand_bytes)}")
    lines.append("=" * 90)

    return "\n".join(lines)


def generate_delete_script(
    exact_dupes: dict[str, list[FileInfo]],
    near_dupes: dict[str, list[FileInfo]],
) -> str:
    """Generate ADB shell script — exact dupes auto-delete, near-dupes commented."""
    lines = []
    lines.append("#!/system/bin/sh")
    lines.append("# WhatsApp Video Duplicate Removal Script v2")
    lines.append(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("# Keeper strategy: OLDEST file (by mtime)")
    lines.append("# Algorithms: SHA-256 (exact) + Content-Hash (near)")
    lines.append("")
    lines.append("DELETED=0")
    lines.append("ERRORS=0")
    lines.append("")

    # Exact duplicates — safe to auto-delete
    lines.append("# ═══════════════════════════════════════════════════════════")
    lines.append("# SEÇÃO A: DUPLICATAS EXATAS (SHA-256 idêntico)")
    lines.append("# ═══════════════════════════════════════════════════════════")
    lines.append("")

    for h, group in exact_dupes.items():
        keeper, remove = choose_keeper(group)
        lines.append(f"# SHA-256: {h[:16]}… — keeping: {os.path.basename(keeper.path)}")
        for r in remove:
            escaped = r.path.replace("'", "'\\''")
            lines.append(f"if rm '{escaped}'; then DELETED=$((DELETED+1)); else ERRORS=$((ERRORS+1)); fi")
        lines.append("")

    # Near-duplicates — commented out for safety
    if near_dupes:
        lines.append("# ═══════════════════════════════════════════════════════════")
        lines.append("# SEÇÃO B: NEAR-DUPLICATES (descomentrar para remover)")
        lines.append("# ⚠️  REVISAR MANUALMENTE antes de descomentar!")
        lines.append("# ═══════════════════════════════════════════════════════════")
        lines.append("")

        for h, group in near_dupes.items():
            keeper, remove = choose_keeper(group)
            lines.append(f"# Content: {h[:16]}… — keeping: {os.path.basename(keeper.path)}")
            for r in remove:
                escaped = r.path.replace("'", "'\\''")
                lines.append(f"# if rm '{escaped}'; then DELETED=$((DELETED+1)); else ERRORS=$((ERRORS+1)); fi")
            lines.append("")

    lines.append(f'echo "Concluído: $DELETED removidos, $ERRORS erros"')
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════
#  Formatters
# ═════════════════════════════════════════════════════════════════════

def format_bytes(b: int) -> str:
    if b >= 1 << 30:
        return f"{b / (1 << 30):.2f} GB"
    if b >= 1 << 20:
        return f"{b / (1 << 20):.1f} MB"
    if b >= 1 << 10:
        return f"{b / (1 << 10):.1f} KB"
    return f"{b} B"


def format_duration(ms: int) -> str:
    if ms <= 0:
        return "?"
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ═════════════════════════════════════════════════════════════════════
#  Main
# ═════════════════════════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║  WhatsApp Video Deduplication v2 — Deep Analysis               ║")
    print("║  Algoritmos: Size → Partial Hash → SHA-256 → Content Hash      ║")
    print("║  Keeper: mais antigo (mtime)  |  Near-dupe: duração+conteúdo   ║")
    print("╚══════════════════════════════════════════════════════════════════╝")

    # Check device
    result = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10)
    device_lines = [l for l in result.stdout.splitlines() if "\tdevice" in l]
    if not device_lines:
        print("\nERRO: Nenhum dispositivo ADB conectado!")
        sys.exit(1)
    device_id = device_lines[0].split("\t")[0]
    print(f"\nDispositivo: {device_id}")

    t0 = time.time()

    # Phase 1: Full inventory
    all_files = phase1_inventory()
    if len(all_files) < 2:
        print("\nMuito poucos arquivos para comparar.")
        return

    # Phase 2: Exact duplicates
    exact_dupes = phase2_exact_duplicates(all_files)

    # Phase 3: Near-duplicates (exclude already-found exact paths)
    exact_paths: set[str] = set()
    for group in exact_dupes.values():
        for f in group:
            exact_paths.add(f.path)

    near_dupes = phase3_near_duplicates(all_files, exact_paths)

    elapsed = time.time() - t0

    # Summary
    exact_removable = sum(len(g) - 1 for g in exact_dupes.values())
    exact_bytes = sum(
        choose_keeper(g)[0].size * len(choose_keeper(g)[1])
        for g in exact_dupes.values()
    )
    near_removable = sum(len(g) - 1 for g in near_dupes.values())
    near_bytes = sum(
        sum(r.size for r in choose_keeper(g)[1])
        for g in near_dupes.values()
    )
    grand_removable = exact_removable + near_removable
    grand_bytes = exact_bytes + near_bytes

    print(f"\n  Tempo total: {elapsed:.0f}s")
    print(f"\n╔══════════════════════════════════════════════════════════════════╗")
    print(f"║  RESULTADO FINAL                                                ║")
    print(f"║  Duplicatas exatas (SHA-256):  {len(exact_dupes):>4} grupos → {exact_removable:>4} removíveis ({format_bytes(exact_bytes):>10})")
    print(f"║  Near-duplicates (conteúdo):   {len(near_dupes):>4} grupos → {near_removable:>4} removíveis ({format_bytes(near_bytes):>10})")
    print(f"║  TOTAL:                        {len(exact_dupes)+len(near_dupes):>4} grupos → {grand_removable:>4} removíveis ({format_bytes(grand_bytes):>10})")
    print(f"╚══════════════════════════════════════════════════════════════════╝")

    # Save outputs
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Report
    report = generate_report(exact_dupes, near_dupes)
    report_path = os.path.join(OUTPUT_DIR, "wa_video_duplicates_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n  Relatório: {report_path}")

    # JSON
    json_data = {
        "generated": datetime.now().isoformat(),
        "device": device_id,
        "analysis_seconds": round(elapsed),
        "algorithms": ["size_grouping", "partial_md5", "sha256_full", "content_hash_skip_header", "mediastore_duration"],
        "keeper_strategy": "oldest_by_mtime",
        "exact_duplicates": {
            "groups": len(exact_dupes),
            "removable_files": exact_removable,
            "removable_bytes": exact_bytes,
            "removable_human": format_bytes(exact_bytes),
            "details": []
        },
        "near_duplicates": {
            "groups": len(near_dupes),
            "removable_files": near_removable,
            "removable_bytes": near_bytes,
            "removable_human": format_bytes(near_bytes),
            "details": []
        },
        "grand_total": {
            "removable_files": grand_removable,
            "removable_bytes": grand_bytes,
            "removable_human": format_bytes(grand_bytes),
        }
    }

    for h, group in exact_dupes.items():
        keeper, remove = choose_keeper(group)
        json_data["exact_duplicates"]["details"].append({
            "sha256": h,
            "size": keeper.size,
            "size_human": format_bytes(keeper.size),
            "duration_ms": keeper.duration_ms,
            "copies": len(group),
            "keep": {"path": keeper.path, "mtime": keeper.mtime},
            "remove": [{"path": r.path, "mtime": r.mtime} for r in remove]
        })
    json_data["exact_duplicates"]["details"].sort(key=lambda g: -g["size"] * (g["copies"]-1))

    for h, group in near_dupes.items():
        keeper, remove = choose_keeper(group)
        json_data["near_duplicates"]["details"].append({
            "content_hash": h,
            "duration_ms": keeper.duration_ms,
            "copies": len(group),
            "keep": {"path": keeper.path, "size": keeper.size, "mtime": keeper.mtime},
            "remove": [{"path": r.path, "size": r.size, "mtime": r.mtime} for r in remove]
        })

    json_path = os.path.join(OUTPUT_DIR, "wa_video_duplicates.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)
    print(f"  JSON:      {json_path}")

    # Delete script
    script = generate_delete_script(exact_dupes, near_dupes)
    script_path = os.path.join(OUTPUT_DIR, "wa_video_delete_duplicates.sh")
    with open(script_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(script)
    print(f"  Script:    {script_path}")

    print(f"\n  ⚠️  Para executar a limpeza das duplicatas EXATAS:")
    print(f"      adb push {script_path} /data/local/tmp/")
    print(f"      adb shell sh /data/local/tmp/wa_video_delete_duplicates.sh")
    print(f"\n  Near-duplicates estão COMENTADOS no script — descomentar após revisão.")


if __name__ == "__main__":
    main()
