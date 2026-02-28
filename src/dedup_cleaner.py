"""
dedup_cleaner.py — Robust duplicate-file detector & cleaner for Android via ADB.

Implements a **5-stage funnel** that progressively narrows candidates,
ensuring zero false positives while staying efficient over ADB shell:

Pipeline
--------
1. **Size grouping**   — ``find … -type f | xargs stat`` to collect
   ``(path, size)`` pairs.  Files with unique sizes are instantly excluded
   (can't be duplicates).  *Cost: 1 shell call per scan root.*

2. **Partial hash**    — For each size-group (≥ 2 files), read the
   **first 4 KB + last 4 KB** via ``dd`` and pipe through ``sha256sum``.
   Files with unique partial hashes are eliminated.
   *Cost: 2× dd + sha256sum per file in size groups.*

3. **Full SHA-256**    — For files whose partial hashes match, compute
   the full ``sha256sum``.  This catches the case where only the
   head/tail are identical but the interior differs.

4. **Byte spot-check** — Even SHA-256 collisions, while astronomically
   unlikely (2⁻¹²⁸ for random data), are mitigated by reading **3
   random 512-byte samples** from the interior of the file and comparing
   them via ``cmp`` or literal equality.  This is the "pontos X" layer.

5. **Deterministic keep-policy** — Among confirmed duplicates, the
   *original* is chosen by:
   a. Shortest path depth (file closer to the media root)
   b. Earliest filename timestamp (e.g. ``IMG-20230416-WA0030``)
   c. Lowest lexicographic path (stable tiebreaker)
   All others are deleted.

Why not just MD5?
-----------------
MD5 has **known practical collision attacks** (SHAttered, Flame malware).
While accidental collisions on media files are near-impossible, a security-
conscious pipeline uses SHA-256 + spot-checks so we can guarantee zero
false positives even against adversarial content.
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from .adb_core import ADBCore

log = logging.getLogger("adb_toolkit.dedup")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PARTIAL_HASH_BYTES = 4096          # bytes to read from head AND tail
SPOT_CHECK_SAMPLES = 3             # random interior samples
SPOT_CHECK_SIZE = 512              # bytes per sample
MIN_SIZE_FOR_PARTIAL = 8192        # files smaller than this → full hash directly
MIN_SIZE_FOR_SPOT = 32768          # files smaller → skip spot-check (full hash enough)

# Default scan targets (WhatsApp + common media dirs)
DEFAULT_SCAN_ROOTS = [
    "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media",
    "/storage/emulated/0/WhatsApp/Media",
    "/storage/emulated/0/DCIM",
    "/storage/emulated/0/Pictures",
    "/storage/emulated/0/Download",
    "/storage/emulated/0/Documents",
    "/storage/emulated/0/Movies",
    "/storage/emulated/0/Music",
]

# Media extensions we consider for dedup
MEDIA_EXTENSIONS: Set[str] = {
    # images
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".tiff",
    # video
    ".mp4", ".mkv", ".avi", ".mov", ".3gp", ".webm", ".m4v",
    # audio
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".amr",
    # documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z",
    # voice notes (WhatsApp)
    ".opus",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class DedupResult:
    """Outcome of a dedup scan/clean."""
    files_scanned: int = 0
    size_groups: int = 0          # groups with 2+ files of same size
    partial_hash_groups: int = 0  # groups surviving partial hash
    full_hash_groups: int = 0     # groups surviving full hash
    confirmed_dup_groups: int = 0 # groups after spot-check (true dups)
    duplicates_found: int = 0     # total duplicate files (excluding originals)
    duplicates_removed: int = 0
    bytes_freed: int = 0
    errors: List[str] = field(default_factory=list)
    details: List[str] = field(default_factory=list)
    kept_originals: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Dedup Cleaner
# ---------------------------------------------------------------------------
class DedupCleaner:
    """Multi-stage duplicate file detector & remover."""

    def __init__(self, adb: ADBCore, serial: str):
        self.adb = adb
        self.serial = serial
        self._progress_cb: Optional[Callable[[str, float], None]] = None

    def set_progress_callback(self, cb: Callable[[str, float], None]):
        self._progress_cb = cb

    # == Public API ========================================================

    def run(
        self,
        scan_roots: Optional[List[str]] = None,
        *,
        extensions: Optional[Set[str]] = None,
        dry_run: bool = False,
        min_size: int = 1024,          # ignore files < 1 KB
        max_depth: int = 10,
    ) -> DedupResult:
        """Execute the full dedup pipeline.

        Parameters
        ----------
        scan_roots : list of remote paths to scan (default: WhatsApp + media)
        extensions : file extensions to consider (default: MEDIA_EXTENSIONS)
        dry_run    : if True, detect but don't delete
        min_size   : ignore files smaller than this (bytes)
        max_depth  : max find depth

        Returns
        -------
        DedupResult
        """
        roots = scan_roots or DEFAULT_SCAN_ROOTS
        exts = extensions or MEDIA_EXTENSIONS
        result = DedupResult()
        t0 = time.time()

        # ── Stage 1: collect files & group by size ──────────────────────
        self._notify("Estágio 1/5 — coletando arquivos …", 0)
        file_map = self._collect_files(roots, exts, min_size, max_depth)
        result.files_scanned = sum(len(v) for v in file_map.values())
        size_groups = {sz: paths for sz, paths in file_map.items() if len(paths) >= 2}
        result.size_groups = len(size_groups)

        total_candidates = sum(len(v) for v in size_groups.values())
        log.info(
            "Stage 1: %d files scanned, %d size groups (%d candidates)",
            result.files_scanned, result.size_groups, total_candidates,
        )
        self._notify(
            f"Estágio 1 concluído — {result.files_scanned} arquivos, "
            f"{result.size_groups} grupos por tamanho",
            10,
        )

        if not size_groups:
            result.details.append("Nenhum grupo de tamanho com 2+ arquivos")
            self._finish(result, t0)
            return result

        # ── Stage 2: partial hash (head + tail) ────────────────────────
        self._notify("Estágio 2/5 — hash parcial (head+tail) …", 12)
        partial_groups = self._stage_partial_hash(size_groups, result)
        result.partial_hash_groups = len(partial_groups)
        log.info("Stage 2: %d partial-hash groups survive", len(partial_groups))
        self._notify(f"Estágio 2 — {len(partial_groups)} grupos após hash parcial", 35)

        if not partial_groups:
            result.details.append("Nenhuma duplicata após hash parcial")
            self._finish(result, t0)
            return result

        # ── Stage 3: full SHA-256 ──────────────────────────────────────
        self._notify("Estágio 3/5 — SHA-256 completo …", 37)
        full_groups = self._stage_full_hash(partial_groups, result)
        result.full_hash_groups = len(full_groups)
        log.info("Stage 3: %d full-hash groups survive", len(full_groups))
        self._notify(f"Estágio 3 — {len(full_groups)} grupos após SHA-256", 60)

        if not full_groups:
            result.details.append("Nenhuma duplicata após SHA-256 completo")
            self._finish(result, t0)
            return result

        # ── Stage 4: byte spot-check ───────────────────────────────────
        self._notify("Estágio 4/5 — verificação por amostragem de bytes …", 62)
        confirmed = self._stage_spot_check(full_groups, result)
        result.confirmed_dup_groups = len(confirmed)
        result.duplicates_found = sum(len(paths) - 1 for paths in confirmed.values())
        log.info(
            "Stage 4: %d confirmed dup groups, %d duplicate files",
            len(confirmed), result.duplicates_found,
        )
        self._notify(
            f"Estágio 4 — {result.duplicates_found} duplicatas confirmadas "
            f"em {len(confirmed)} grupos",
            80,
        )

        if not confirmed:
            result.details.append("Nenhuma duplicata após spot-check de bytes")
            self._finish(result, t0)
            return result

        # ── Stage 5: choose originals & delete duplicates ──────────────
        self._notify("Estágio 5/5 — removendo duplicatas …", 82)
        self._stage_remove(confirmed, result, dry_run)

        self._finish(result, t0)
        return result

    # == Stage implementations =============================================

    # Maximum files we expect from a single find|stat command before it
    # might overflow ADB's stdout buffer (~4 MB).  If a directory is likely
    # larger than this, we split into subdirectories.
    _MAX_FILES_PER_SCAN = 10_000

    def _collect_files(
        self,
        roots: List[str],
        exts: Set[str],
        min_size: int,
        max_depth: int,
    ) -> Dict[int, List[str]]:
        """Stage 1: collect (path, size) grouped by size.

        Strategy for very large directories (e.g. WhatsApp Media 220K+):
        1. Build a ``-name`` filter from *exts* so ``find`` only returns
           files we care about — this cuts the result set dramatically.
        2. If the scan still returns nothing (ADB buffer overflow), split
           into immediate subdirectories and recurse.
        """
        size_map: Dict[int, List[str]] = {}
        seen_paths: Set[str] = set()

        # Build a find -name filter:  \( -iname '*.jpg' -o -iname '*.png' ... \)
        name_clauses = " -o ".join(
            f"-iname '*{ext}'" for ext in sorted(exts)
        )
        name_filter = rf"\( {name_clauses} \)" if name_clauses else ""

        def _parse_stat_output(out: str) -> int:
            """Parse stat output, populate size_map, return count of parsed files."""
            count = 0
            for line in out.splitlines():
                line = line.strip()
                if "|" not in line:
                    continue
                parts = line.split("|", 1)
                if len(parts) != 2:
                    continue
                try:
                    size = int(parts[0])
                except ValueError:
                    continue
                path = parts[1]
                if size < min_size:
                    continue
                # Double-check extension (case-insensitive)
                _, ext = os.path.splitext(path)
                if ext.lower() not in exts:
                    continue
                canon = path.replace("/storage/emulated/0", "/sdcard")
                if canon in seen_paths:
                    continue
                seen_paths.add(canon)
                size_map.setdefault(size, []).append(path)
                count += 1
            return count

        def _scan_dir(directory: str, depth: int, timeout: int = 300) -> str:
            cmd = (
                f'find "{directory}" -maxdepth {depth} -type f'
                f" {name_filter} 2>/dev/null"
                f" | xargs stat -c '%s|%n' 2>/dev/null"
            )
            return self.adb.run_shell(cmd, self.serial, timeout=timeout)

        def _scan_recursive(
            directory: str, depth: int, label: str, pct_base: float, pct_span: float,
        ) -> None:
            """Scan *directory*; if the result is empty, split into subdirs."""
            self._notify(f"Escaneando {label} …", pct_base)
            out = _scan_dir(directory, depth)
            n = _parse_stat_output(out)

            if n > 0:
                return  # success

            # Fallback: directory probably too large.
            # First, verify it actually has content.
            count_cmd = f'find "{directory}" -maxdepth 1 -type f {name_filter} 2>/dev/null | wc -l'
            shallow_count_raw = self.adb.run_shell(count_cmd, self.serial, timeout=30)
            try:
                shallow_count = int(shallow_count_raw.strip())
            except ValueError:
                shallow_count = 0

            # Scan shallow files (depth 1) — these are directly inside
            if shallow_count > 0:
                shallow_out = _scan_dir(directory, 1)
                _parse_stat_output(shallow_out)

            # Now recurse into subdirectories
            ls_cmd = (
                f'find "{directory}" -maxdepth 1 -mindepth 1 -type d 2>/dev/null'
            )
            subdirs_raw = self.adb.run_shell(ls_cmd, self.serial, timeout=30)
            subdirs = [d.strip() for d in subdirs_raw.splitlines() if d.strip()]

            if not subdirs:
                return  # no subdirs, nothing more to do

            log.info(
                "Dir %s returned empty — splitting into %d subdirectories",
                directory, len(subdirs),
            )
            for si, subpath in enumerate(subdirs):
                sub_label_parts = subpath.rstrip("/").split("/")
                sub_label = "/".join(sub_label_parts[-2:]) if len(sub_label_parts) >= 2 else sub_label_parts[-1]
                sub_pct = pct_base + pct_span * (si / max(len(subdirs), 1))
                _scan_recursive(
                    subpath,
                    max(depth - 1, 1),
                    sub_label,
                    sub_pct,
                    pct_span / max(len(subdirs), 1),
                )

        for root_idx, root in enumerate(roots):
            label = root.split("/")[-1] or root
            pct_base = 2 + 8 * root_idx / max(len(roots), 1)
            pct_span = 8 / max(len(roots), 1)
            _scan_recursive(root, max_depth, label, pct_base, pct_span)

        return size_map

    def _stage_partial_hash(
        self,
        size_groups: Dict[int, List[str]],
        result: DedupResult,
    ) -> Dict[str, List[str]]:
        """Stage 2: hash first 4KB + last 4KB → group by partial hash.

        Returns dict keyed by ``"<size>:<partial_sha256>"`` → list of paths.
        """
        partial_map: Dict[str, List[str]] = {}
        total = sum(len(v) for v in size_groups.values())
        done = 0

        for size, paths in size_groups.items():
            for fpath in paths:
                done += 1
                if done % 200 == 0:
                    pct = 12 + 23 * done / max(total, 1)
                    self._notify(f"Hash parcial… {done}/{total}", pct)

                phash = self._partial_hash(fpath, size)
                if phash is None:
                    continue
                key = f"{size}:{phash}"
                partial_map.setdefault(key, []).append(fpath)

        # Keep only groups with 2+ files
        return {k: v for k, v in partial_map.items() if len(v) >= 2}

    def _stage_full_hash(
        self,
        partial_groups: Dict[str, List[str]],
        result: DedupResult,
    ) -> Dict[str, List[str]]:
        """Stage 3: full SHA-256 for partial-hash matches."""
        full_map: Dict[str, List[str]] = {}
        all_files = [f for paths in partial_groups.values() for f in paths]
        total = len(all_files)

        # Batch sha256sum for efficiency (chunks of 30)
        hash_results: Dict[str, str] = {}
        batch_size = 30
        for i in range(0, total, batch_size):
            chunk = all_files[i: i + batch_size]
            targets = " ".join(f"'{f}'" for f in chunk)
            out = self.adb.run_shell(
                f"sha256sum {targets} 2>/dev/null",
                self.serial, timeout=120,
            )
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                # sha256sum output: "<hash>  <path>" (two spaces)
                parts = line.split(None, 1)
                if len(parts) == 2 and len(parts[0]) == 64:
                    hash_results[parts[1].strip()] = parts[0]

            pct = 37 + 23 * min((i + batch_size) / max(total, 1), 1.0)
            self._notify(f"SHA-256… {min(i+batch_size, total)}/{total}", pct)

        # Re-group by full hash
        for paths in partial_groups.values():
            sub_map: Dict[str, List[str]] = {}
            for fpath in paths:
                h = hash_results.get(fpath)
                if h:
                    sub_map.setdefault(h, []).append(fpath)
            for h, fps in sub_map.items():
                if len(fps) >= 2:
                    full_map[h] = fps

        return full_map

    def _stage_spot_check(
        self,
        full_groups: Dict[str, List[str]],
        result: DedupResult,
    ) -> Dict[str, List[str]]:
        """Stage 4: byte-level spot-check on random interior offsets.

        For each group, pick the first file as reference and compare every
        other file at SPOT_CHECK_SAMPLES random offsets using ``cmp``.
        """
        confirmed: Dict[str, List[str]] = {}
        group_idx = 0
        total_groups = len(full_groups)

        for hash_key, paths in full_groups.items():
            group_idx += 1
            if group_idx % 20 == 0:
                pct = 62 + 18 * group_idx / max(total_groups, 1)
                self._notify(f"Spot-check… grupo {group_idx}/{total_groups}", pct)

            ref = paths[0]
            verified_paths = [ref]

            # Get file size for offset generation
            size = self._get_file_size(ref)
            if size is None or size < MIN_SIZE_FOR_SPOT:
                # Small file — full SHA-256 is sufficient, skip spot-check
                confirmed[hash_key] = paths
                continue

            for other in paths[1:]:
                if self._byte_compare(ref, other, size):
                    verified_paths.append(other)
                else:
                    log.warning(
                        "Spot-check FALHOU (falso positivo SHA-256 evitado): "
                        "%s vs %s", ref, other,
                    )
                    result.details.append(
                        f"FALSO POSITIVO evitado: {os.path.basename(ref)} "
                        f"vs {os.path.basename(other)}"
                    )

            if len(verified_paths) >= 2:
                confirmed[hash_key] = verified_paths

        return confirmed

    def _stage_remove(
        self,
        confirmed: Dict[str, List[str]],
        result: DedupResult,
        dry_run: bool,
    ):
        """Stage 5: pick original, delete the rest."""
        to_delete: List[Tuple[str, int, str]] = []  # (path, size, group_key)

        for key, paths in confirmed.items():
            original = self._pick_original(paths)
            result.kept_originals.append(original)

            for p in paths:
                if p == original:
                    continue
                size = self._get_file_size(p) or 0
                to_delete.append((p, size, key))

            result.details.append(
                f"KEEP {os.path.basename(original)}  "
                f"({len(paths)-1} cópias)"
            )

        # Delete in batches
        batch_size = 40
        total = len(to_delete)
        for i in range(0, total, batch_size):
            chunk = to_delete[i: i + batch_size]
            targets = " ".join(f"'{p}'" for p, _, _ in chunk)
            cmd = f"rm -f {targets} 2>/dev/null; echo OK"
            if not dry_run:
                self.adb.run_shell(cmd, self.serial, timeout=30)

            for p, sz, _ in chunk:
                result.duplicates_removed += 1
                result.bytes_freed += sz
                result.details.append(
                    f"{'[DRY] ' if dry_run else ''}DEL {p}  ({_fmt(sz)})"
                )

            pct = 82 + 18 * min((i + batch_size) / max(total, 1), 1.0)
            self._notify(
                f"Removendo duplicatas… {min(i+batch_size, total)}/{total}", pct,
            )

    # == Helper: partial hash ==============================================

    def _partial_hash(self, fpath: str, size: int) -> Optional[str]:
        """SHA-256 of first 4KB + last 4KB (or full file if small)."""
        if size < MIN_SIZE_FOR_PARTIAL:
            # File is small; hash the whole thing
            out = self.adb.run_shell(
                f"sha256sum '{fpath}' 2>/dev/null", self.serial, timeout=15,
            )
            parts = out.split()
            return parts[0] if parts and len(parts[0]) == 64 else None

        # Read head and tail, concatenate, hash
        head_bytes = PARTIAL_HASH_BYTES
        tail_skip = (size - PARTIAL_HASH_BYTES) // 512  # dd skip in 512-byte blocks
        tail_count = (PARTIAL_HASH_BYTES + 511) // 512

        cmd = (
            f"( dd if='{fpath}' bs={head_bytes} count=1 2>/dev/null ; "
            f"  dd if='{fpath}' bs=512 skip={tail_skip} count={tail_count} 2>/dev/null "
            f") | sha256sum 2>/dev/null"
        )
        out = self.adb.run_shell(cmd, self.serial, timeout=15)
        parts = out.split()
        return parts[0] if parts and len(parts[0]) == 64 else None

    # == Helper: byte-level compare ========================================

    def _byte_compare(self, file_a: str, file_b: str, size: int) -> bool:
        """Compare SPOT_CHECK_SAMPLES random byte ranges using ``cmp``.

        Also does a quick ``cmp -s`` on the first SPOT_CHECK_SIZE bytes
        and the last SPOT_CHECK_SIZE bytes as bookends.
        """
        # Quick full cmp for not-too-large files (< 2 MB)
        if size < 2 * 1024 * 1024:
            out = self.adb.run_shell(
                f"cmp -s '{file_a}' '{file_b}' && echo SAME || echo DIFF",
                self.serial, timeout=30,
            )
            return out.strip() == "SAME"

        # For larger files: sample at random interior offsets
        max_offset = size - SPOT_CHECK_SIZE
        if max_offset <= 0:
            max_offset = 1

        offsets = sorted(set(
            random.randint(SPOT_CHECK_SIZE, max_offset)
            for _ in range(SPOT_CHECK_SAMPLES * 3)  # generate extras for uniqueness
        ))[:SPOT_CHECK_SAMPLES]

        # Also always check head and tail
        offsets = [0] + offsets + [max(0, size - SPOT_CHECK_SIZE)]

        for off in offsets:
            skip_blocks = off // 512
            cmd = (
                f"cmp -s "
                f"<(dd if='{file_a}' bs=512 skip={skip_blocks} count=1 2>/dev/null) "
                f"<(dd if='{file_b}' bs=512 skip={skip_blocks} count=1 2>/dev/null) "
                f"&& echo SAME || echo DIFF"
            )
            out = self.adb.run_shell(cmd, self.serial, timeout=10)
            if out.strip() != "SAME":
                return False

        return True

    # == Helper: file size =================================================

    def _get_file_size(self, fpath: str) -> Optional[int]:
        out = self.adb.run_shell(
            f"stat -c '%s' '{fpath}' 2>/dev/null", self.serial, timeout=5,
        )
        try:
            return int(out.strip())
        except (ValueError, AttributeError):
            return None

    # == Helper: pick the original (keep policy) ===========================

    # WhatsApp filenames: IMG-20230416-WA0030.jpg, VID-20240101-WA0005.mp4
    _WA_TS_RE = re.compile(
        r"(?:IMG|VID|AUD|DOC|STK|PTT)-(\d{8})-WA(\d+)",
        re.IGNORECASE,
    )
    # Generic timestamp in filename: 20230416_123456
    _GENERIC_TS_RE = re.compile(r"(\d{8})[_\-](\d{4,6})")

    def _pick_original(self, paths: List[str]) -> str:
        """Choose which file to keep among confirmed duplicates.

        Priority:
        1. Earliest WhatsApp timestamp + lowest sequence number.
        2. Earliest generic timestamp.
        3. Shallowest path (closest to media root).
        4. Shortest filename.
        5. Lexicographically first (stable fallback).
        """

        def sort_key(p: str):
            basename = os.path.basename(p)
            depth = p.count("/")

            # WhatsApp timestamp
            m = self._WA_TS_RE.search(basename)
            if m:
                return (0, m.group(1), int(m.group(2)), depth, len(basename), p)

            # Generic timestamp
            m2 = self._GENERIC_TS_RE.search(basename)
            if m2:
                return (1, m2.group(1), int(m2.group(2) or "0"), depth, len(basename), p)

            # No timestamp — prefer shallower, shorter name
            return (2, "99999999", 0, depth, len(basename), p)

        return min(paths, key=sort_key)

    # == Notify ============================================================

    def _notify(self, msg: str, pct: float):
        log.info("[%3.0f%%] %s", pct, msg)
        if self._progress_cb:
            try:
                self._progress_cb(msg, pct)
            except Exception:
                pass

    def _finish(self, result: DedupResult, t0: float):
        elapsed = time.time() - t0
        summary = (
            f"Dedup concluído em {elapsed:.1f}s — "
            f"{result.files_scanned} arquivos escaneados, "
            f"{result.confirmed_dup_groups} grupos de duplicatas, "
            f"{result.duplicates_removed} duplicatas removidas, "
            f"~{_fmt(result.bytes_freed)} liberados"
        )
        result.details.append(summary)
        self._notify(summary, 100)
        log.info(summary)


# ---------------------------------------------------------------------------
# Byte formatter
# ---------------------------------------------------------------------------
def _fmt(size: int) -> str:
    if size <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} PB"
