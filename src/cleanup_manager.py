"""
cleanup_manager.py — Modular device cleanup with estimate → review → execute.

Each cleanup mode can be scanned independently (dry-run) to show the user
exactly what will be freed, then executed when confirmed.  Every mode
reports progress through its own callback so the GUI can render independent
progress bars.

Modes
-----
1. ``app_cache``     — pm trim-caches + per-app cache/code_cache
2. ``junk_dirs``     — deep scan for cache/preload/dump/log/thumb dirs
3. ``junk_files``    — loose .log/.tmp/.dmp/.thumb/etc. files
4. ``known_junk``    — well-known Android expendable paths
5. ``orphans``       — leftover dirs from uninstalled apps
6. ``duplicates``    — find duplicate files by hash (*recommended last*)
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Set, Tuple

from .adb_base import get_io_pool

from .adb_core import ADBCore
from .utils import format_bytes

log = logging.getLogger("adb_toolkit.cleanup")

# ---------------------------------------------------------------------------
# Enums & constants
# ---------------------------------------------------------------------------

class CleanupMode(str, Enum):
    APP_CACHE   = "app_cache"
    JUNK_DIRS   = "junk_dirs"
    JUNK_FILES  = "junk_files"
    KNOWN_JUNK  = "known_junk"
    ORPHANS     = "orphans"
    DUPLICATES  = "duplicates"


MODE_LABELS: Dict[CleanupMode, str] = {
    CleanupMode.APP_CACHE:  "Cache de Aplicativos",
    CleanupMode.JUNK_DIRS:  "Diretórios de Lixo",
    CleanupMode.JUNK_FILES: "Arquivos Avulsos",
    CleanupMode.KNOWN_JUNK: "Locais Conhecidos",
    CleanupMode.ORPHANS:    "Órfãos de Apps",
    CleanupMode.DUPLICATES: "Arquivos Duplicados",
}

MODE_DESCRIPTIONS: Dict[CleanupMode, str] = {
    CleanupMode.APP_CACHE:  "Cache, code_cache e pm trim-caches de todos os apps",
    CleanupMode.JUNK_DIRS:  "Diretórios cache/preload/dump/log/thumbnails no armazenamento",
    CleanupMode.JUNK_FILES: "Arquivos .log, .tmp, .dmp, thumbs.db, etc.",
    CleanupMode.KNOWN_JUNK: "LOST.DIR, tombstones, ANR traces, bugreports, etc.",
    CleanupMode.ORPHANS:    "Pastas de apps desinstalados em Android/data, obb, media",
    CleanupMode.DUPLICATES: "Arquivos duplicados por hash (recomendado após demais limpezas)",
}

# Execution order (duplicates last)
MODE_ORDER: List[CleanupMode] = [
    CleanupMode.APP_CACHE,
    CleanupMode.JUNK_DIRS,
    CleanupMode.JUNK_FILES,
    CleanupMode.KNOWN_JUNK,
    CleanupMode.ORPHANS,
    CleanupMode.DUPLICATES,
]

# Scan roots
_SCAN_ROOTS = [
    "/sdcard",
    "/storage/emulated/0",
    "/data/data",
    "/data/user/0",
    "/data/local",
    "/data/media/0",
]

_FILE_SCAN_ROOTS = ["/sdcard", "/storage/emulated/0", "/data/local"]

_KNOWN_JUNK_PATHS: List[str] = [
    "/data/log", "/data/logs", "/data/logcat",
    "/data/tombstones", "/data/anr", "/data/local/tmp",
    "/data/vendor/logs",
    "/sdcard/LOST.DIR", "/storage/emulated/0/LOST.DIR",
    "/sdcard/.thumbnails", "/storage/emulated/0/.thumbnails",
    "/sdcard/.thumbs",
    "/sdcard/Android/data/com.android.providers.media/albumthumbs",
    "/sdcard/DCIM/.thumbnails", "/storage/emulated/0/DCIM/.thumbnails",
]

_ORPHAN_ROOTS = [
    "/sdcard/Android/data", "/sdcard/Android/media", "/sdcard/Android/obb",
    "/storage/emulated/0/Android/data",
    "/storage/emulated/0/Android/media",
    "/storage/emulated/0/Android/obb",
    "/data/data", "/data/user/0",
]

_DUPLICATE_SCAN_ROOTS = [
    "/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Download",
    "/sdcard/Documents", "/sdcard/Movies", "/sdcard/Music",
    "/storage/emulated/0/DCIM", "/storage/emulated/0/Pictures",
    "/storage/emulated/0/Download", "/storage/emulated/0/Documents",
]

_MIN_PACKAGES_THRESHOLD = 15
_CANARY_PACKAGES = frozenset({
    "android", "com.android.settings", "com.android.systemui",
    "com.android.phone", "com.android.providers.settings",
})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CleanupItem:
    """One file or directory that can be removed."""
    path: str
    size_bytes: int = 0
    item_type: str = "dir"              # "dir" | "file"
    detail: str = ""                    # human-readable description
    group: str = ""                     # for duplicates: hash group id


@dataclass
class ModeEstimate:
    """Result of scanning one cleanup mode."""
    mode: CleanupMode
    items: List[CleanupItem] = field(default_factory=list)
    total_bytes: int = 0
    total_items: int = 0
    error: str = ""

    @property
    def label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode.value)


@dataclass
class ModeResult:
    """Result of executing one cleanup mode."""
    mode: CleanupMode
    items_removed: int = 0
    bytes_freed: int = 0
    errors: List[str] = field(default_factory=list)


@dataclass
class ModeProgress:
    """Progress for a single cleanup mode."""
    mode: CleanupMode
    phase: str = ""             # "scanning" | "cleaning" | "complete" | "error"
    message: str = ""
    percent: float = 0.0
    items_done: int = 0
    items_total: int = 0
    bytes_freed: int = 0


# Type alias for the progress callback
ProgressCb = Callable[[ModeProgress], None]


# ---------------------------------------------------------------------------
# Cleanup Manager
# ---------------------------------------------------------------------------

class CleanupManager:
    """Manages device cleanup with independent per-mode estimate & execute."""

    def __init__(self, adb: ADBCore):
        self.adb = adb
        self._cancel_flag = threading.Event()
        self._progress_cbs: Dict[CleanupMode, ProgressCb] = {}

    def set_mode_progress_callback(self, mode: CleanupMode, cb: ProgressCb):
        self._progress_cbs[mode] = cb

    def cancel(self):
        self._cancel_flag.set()

    def reset(self):
        self._cancel_flag.clear()

    # ------------------------------------------------------------------
    # Estimate (dry-run scan)
    # ------------------------------------------------------------------

    def estimate(
        self, serial: str, modes: List[CleanupMode],
    ) -> Dict[CleanupMode, ModeEstimate]:
        """Scan device and return estimated items for each mode.

        Independent modes are scanned in parallel for faster results.
        """
        self._cancel_flag.clear()
        results: Dict[CleanupMode, ModeEstimate] = {}

        # Run independent scans concurrently (cap at 3 to avoid
        # overwhelming the ADB daemon with too many shell sessions).
        workers = min(len(modes), 3)
        if workers <= 1:
            # Single mode — run inline
            for mode in modes:
                if self._cancel_flag.is_set():
                    break
                try:
                    results[mode] = self._estimate_mode(serial, mode)
                except Exception as exc:
                    log.exception("Estimate failed for %s: %s", mode, exc)
                    results[mode] = ModeEstimate(mode=mode, error=str(exc))
            return results

        pool = get_io_pool()
        from concurrent.futures import as_completed
        future_to_mode = {
            pool.submit(self._estimate_mode, serial, mode): mode
            for mode in modes
            if not self._cancel_flag.is_set()
        }
        for fut in as_completed(future_to_mode):
            mode = future_to_mode[fut]
            try:
                results[mode] = fut.result()
            except Exception as exc:
                log.exception("Estimate failed for %s: %s", mode, exc)
                results[mode] = ModeEstimate(mode=mode, error=str(exc))
        return results

    def _estimate_mode(self, serial: str, mode: CleanupMode) -> ModeEstimate:
        dispatch = {
            CleanupMode.APP_CACHE:  self._scan_app_cache,
            CleanupMode.JUNK_DIRS:  self._scan_junk_dirs,
            CleanupMode.JUNK_FILES: self._scan_junk_files,
            CleanupMode.KNOWN_JUNK: self._scan_known_junk,
            CleanupMode.ORPHANS:    self._scan_orphans,
            CleanupMode.DUPLICATES: self._scan_duplicates,
        }
        fn = dispatch[mode]
        self._emit(mode, ModeProgress(mode=mode, phase="scanning", message="Escaneando…", percent=0))
        est = fn(serial)
        est.total_items = len(est.items)
        est.total_bytes = sum(i.size_bytes for i in est.items)
        self._emit(mode, ModeProgress(
            mode=mode, phase="complete",
            message=f"{est.total_items} itens ({format_bytes(est.total_bytes)})",
            percent=100, items_total=est.total_items,
        ))
        return est

    # ------------------------------------------------------------------
    # Execute (actual cleanup)
    # ------------------------------------------------------------------

    def execute(
        self, serial: str, estimates: Dict[CleanupMode, ModeEstimate],
    ) -> Dict[CleanupMode, ModeResult]:
        """Run cleanup for the given previously-estimated modes."""
        self._cancel_flag.clear()
        results: Dict[CleanupMode, ModeResult] = {}
        for mode in MODE_ORDER:
            if mode not in estimates:
                continue
            if self._cancel_flag.is_set():
                break
            est = estimates[mode]
            if not est.items:
                results[mode] = ModeResult(mode=mode)
                continue
            try:
                results[mode] = self._execute_mode(serial, est)
            except Exception as exc:
                log.exception("Execute failed for %s: %s", mode, exc)
                results[mode] = ModeResult(mode=mode, errors=[str(exc)])
        return results

    def _execute_mode(self, serial: str, est: ModeEstimate) -> ModeResult:
        dispatch = {
            CleanupMode.APP_CACHE:  self._clean_app_cache,
            CleanupMode.JUNK_DIRS:  self._clean_dirs,
            CleanupMode.JUNK_FILES: self._clean_files,
            CleanupMode.KNOWN_JUNK: self._clean_dirs,
            CleanupMode.ORPHANS:    self._clean_dirs,
            CleanupMode.DUPLICATES: self._clean_files,
        }
        fn = dispatch[est.mode]
        return fn(serial, est)

    # ------------------------------------------------------------------
    # Scan implementations
    # ------------------------------------------------------------------

    def _scan_app_cache(self, serial: str) -> ModeEstimate:
        est = ModeEstimate(mode=CleanupMode.APP_CACHE)
        self._emit(CleanupMode.APP_CACHE, ModeProgress(
            mode=CleanupMode.APP_CACHE, phase="scanning",
            message="Listando pacotes…", percent=10,
        ))
        pkgs = self.adb.list_packages(serial, third_party=False)
        # Estimate total cache size via du on first N packages
        total_est_bytes = 0
        sample = pkgs[:50]
        if sample:
            paths_str = " ".join(
                f"/data/data/{p}/cache /data/data/{p}/code_cache"
                for p in sample
            )
            out = self.adb.run_shell(
                f"du -sk {paths_str} 2>/dev/null", serial, timeout=60,
            )
            for line in out.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    try:
                        total_est_bytes += int(parts[0]) * 1024
                    except ValueError:
                        pass
            # Extrapolate
            if len(pkgs) > len(sample):
                total_est_bytes = int(total_est_bytes * len(pkgs) / len(sample))

        for pkg in pkgs:
            for suffix in ("cache", "code_cache"):
                est.items.append(CleanupItem(
                    path=f"/data/data/{pkg}/{suffix}",
                    item_type="dir",
                    detail=f"{pkg}/{suffix}",
                ))

        # Set estimated size evenly across items (approximation)
        if est.items and total_est_bytes > 0:
            per_item = total_est_bytes // len(est.items)
            for item in est.items:
                item.size_bytes = per_item

        self._emit(CleanupMode.APP_CACHE, ModeProgress(
            mode=CleanupMode.APP_CACHE, phase="scanning",
            message=f"{len(pkgs)} apps encontrados", percent=80,
        ))
        return est

    def _scan_junk_dirs(self, serial: str) -> ModeEstimate:
        est = ModeEstimate(mode=CleanupMode.JUNK_DIRS)
        names = [
            "*cache*", "*preload*", "dump", "dumps", "core_dump*",
            "log", "logs", "logcat", "bugreport*",
            ".thumbnails", "thumbnails", ".thumbs", "thumbs", ".Thumbs",
            "LOST.DIR", ".Trash", ".trashbin", "tmp", "temp",
        ]
        unique_lower: set = set()
        iname_parts: list = []
        for n in names:
            low = n.lower()
            if low not in unique_lower:
                unique_lower.add(low)
                iname_parts.append(f"-iname '{n}'")
        or_expr = " -o ".join(iname_parts)

        all_dirs: List[str] = []
        for idx, root in enumerate(_SCAN_ROOTS):
            if self._cancel_flag.is_set():
                break
            self._emit(CleanupMode.JUNK_DIRS, ModeProgress(
                mode=CleanupMode.JUNK_DIRS, phase="scanning",
                message=f"Escaneando {root}…",
                percent=10 + 60 * idx / len(_SCAN_ROOTS),
            ))
            cmd = (
                f'find "{root}" -maxdepth 6 -type d '
                f"\\( {or_expr} \\) 2>/dev/null"
            )
            out = self.adb.run_shell(cmd, serial, timeout=120)
            for line in out.splitlines():
                d = line.strip()
                if d and d.startswith("/") and d not in (
                    "/data", "/sdcard", "/storage", "/system", "/vendor"
                ):
                    all_dirs.append(d)

        # Deduplicate
        seen: set = set()
        unique: List[str] = []
        for d in all_dirs:
            canon = d.replace("/storage/emulated/0", "/sdcard")
            if canon not in seen:
                seen.add(canon)
                unique.append(d)

        # Measure
        size_map = self._measure_dirs(serial, unique) if unique else {}
        for d in unique:
            est.items.append(CleanupItem(
                path=d, size_bytes=size_map.get(d, 0),
                item_type="dir", detail=d,
            ))
        return est

    def _scan_junk_files(self, serial: str) -> ModeEstimate:
        est = ModeEstimate(mode=CleanupMode.JUNK_FILES)
        extensions = ["log", "tmp", "temp", "bak", "dmp", "mdmp", "core", "thumb"]
        iname_parts = " -o ".join(f"-iname '*.{e}'" for e in extensions)
        exact = ["thumbs.db", "desktop.ini", "Thumbdata*", "logcat*.txt"]
        exact_parts = " -o ".join(f"-iname '{n}'" for n in exact)
        full_expr = f"\\( {iname_parts} -o {exact_parts} \\)"

        all_files: List[str] = []
        for idx, root in enumerate(_FILE_SCAN_ROOTS):
            if self._cancel_flag.is_set():
                break
            self._emit(CleanupMode.JUNK_FILES, ModeProgress(
                mode=CleanupMode.JUNK_FILES, phase="scanning",
                message=f"Escaneando {root}…",
                percent=10 + 60 * idx / len(_FILE_SCAN_ROOTS),
            ))
            cmd = f'find "{root}" -maxdepth 8 -type f {full_expr} 2>/dev/null'
            out = self.adb.run_shell(cmd, serial, timeout=90)
            for line in out.splitlines():
                f = line.strip()
                if f and f.startswith("/"):
                    all_files.append(f)

        # Deduplicate
        seen: set = set()
        unique: List[str] = []
        for f in all_files:
            canon = f.replace("/storage/emulated/0", "/sdcard")
            if canon not in seen:
                seen.add(canon)
                unique.append(f)

        # Measure file sizes via stat
        if unique:
            size_map = self._measure_files(serial, unique)
        else:
            size_map = {}

        for f in unique:
            est.items.append(CleanupItem(
                path=f, size_bytes=size_map.get(f, 0),
                item_type="file", detail=f,
            ))
        return est

    def _scan_known_junk(self, serial: str) -> ModeEstimate:
        est = ModeEstimate(mode=CleanupMode.KNOWN_JUNK)
        for idx, jpath in enumerate(_KNOWN_JUNK_PATHS):
            if self._cancel_flag.is_set():
                break
            self._emit(CleanupMode.KNOWN_JUNK, ModeProgress(
                mode=CleanupMode.KNOWN_JUNK, phase="scanning",
                message=f"Verificando {jpath}…",
                percent=10 + 80 * idx / len(_KNOWN_JUNK_PATHS),
            ))
            exists = self.adb.run_shell(
                f"[ -d '{jpath}' ] && echo Y || echo N", serial, timeout=5,
            )
            if not exists.startswith("Y"):
                continue
            sz = 0
            size_out = self.adb.run_shell(
                f"du -sk '{jpath}' 2>/dev/null | head -1", serial, timeout=10,
            )
            try:
                sz = int(size_out.split()[0]) * 1024
            except (ValueError, IndexError):
                pass
            est.items.append(CleanupItem(
                path=jpath, size_bytes=sz, item_type="dir", detail=jpath,
            ))
        return est

    def _scan_orphans(self, serial: str) -> ModeEstimate:
        est = ModeEstimate(mode=CleanupMode.ORPHANS)
        self._emit(CleanupMode.ORPHANS, ModeProgress(
            mode=CleanupMode.ORPHANS, phase="scanning",
            message="Obtendo lista de pacotes…", percent=5,
        ))

        installed = self._fetch_installed_packages(serial)
        if installed is None:
            est.error = "Não foi possível obter lista confiável de pacotes instalados"
            return est

        pkg_re = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")
        orphans: List[Tuple[str, str]] = []

        for idx, root in enumerate(_ORPHAN_ROOTS):
            if self._cancel_flag.is_set():
                break
            self._emit(CleanupMode.ORPHANS, ModeProgress(
                mode=CleanupMode.ORPHANS, phase="scanning",
                message=f"Escaneando {root}…",
                percent=15 + 50 * idx / len(_ORPHAN_ROOTS),
            ))
            out = self.adb.run_shell(f"ls -1 '{root}' 2>/dev/null", serial, timeout=15)
            for name in out.splitlines():
                name = name.strip()
                if not name or not pkg_re.match(name):
                    continue
                if name in installed:
                    continue
                if name.startswith(("com.android.", "com.google.android.")):
                    check = self.adb.run_shell(
                        f"pm path '{name}' 2>/dev/null", serial, timeout=5,
                    )
                    if check.strip():
                        continue
                orphans.append((f"{root}/{name}", name))

        # Deduplicate
        seen: set = set()
        unique: List[Tuple[str, str]] = []
        for full, pkg in orphans:
            canon = full.replace("/storage/emulated/0", "/sdcard")
            if canon not in seen:
                seen.add(canon)
                unique.append((full, pkg))

        dirs = [full for full, _ in unique]
        size_map = self._measure_dirs(serial, dirs) if dirs else {}

        for full, pkg in unique:
            est.items.append(CleanupItem(
                path=full, size_bytes=size_map.get(full, 0),
                item_type="dir", detail=f"Órfão: {pkg}",
            ))
        return est

    def _scan_duplicates(self, serial: str) -> ModeEstimate:
        est = ModeEstimate(mode=CleanupMode.DUPLICATES)
        self._emit(CleanupMode.DUPLICATES, ModeProgress(
            mode=CleanupMode.DUPLICATES, phase="scanning",
            message="Indexando arquivos…", percent=5,
        ))

        # 1. Get all files with sizes
        all_files: List[Tuple[str, int]] = []
        for idx, root in enumerate(_DUPLICATE_SCAN_ROOTS):
            if self._cancel_flag.is_set():
                break
            self._emit(CleanupMode.DUPLICATES, ModeProgress(
                mode=CleanupMode.DUPLICATES, phase="scanning",
                message=f"Indexando {root}…",
                percent=5 + 30 * idx / max(len(_DUPLICATE_SCAN_ROOTS), 1),
            ))
            cmd = (
                f"find '{root}' -type f 2>/dev/null"
                f" | xargs stat -c '%n|%s' 2>/dev/null"
            )
            out = self.adb.run_shell(cmd, serial, timeout=180)
            for line in out.splitlines():
                line = line.strip()
                if "|" not in line:
                    continue
                parts = line.rsplit("|", 1)
                if len(parts) != 2:
                    continue
                try:
                    sz = int(parts[1])
                except ValueError:
                    continue
                if sz > 1024:  # skip tiny files
                    all_files.append((parts[0], sz))

        # Deduplicate paths
        seen: set = set()
        unique_files: List[Tuple[str, int]] = []
        for path, sz in all_files:
            canon = path.replace("/storage/emulated/0", "/sdcard")
            if canon not in seen:
                seen.add(canon)
                unique_files.append((path, sz))

        # 2. Group by size (potential duplicates have same size)
        size_groups: Dict[int, List[str]] = {}
        for path, sz in unique_files:
            size_groups.setdefault(sz, []).append(path)
        candidates = {sz: paths for sz, paths in size_groups.items() if len(paths) > 1}

        if not candidates:
            return est

        # 3. Compute MD5 for candidate groups
        self._emit(CleanupMode.DUPLICATES, ModeProgress(
            mode=CleanupMode.DUPLICATES, phase="scanning",
            message=f"Calculando hashes de {sum(len(v) for v in candidates.values())} arquivos…",
            percent=40,
        ))

        hash_groups: Dict[str, List[Tuple[str, int]]] = {}
        total_to_hash = sum(len(v) for v in candidates.values())
        hashed = 0
        batch_paths: List[Tuple[str, int]] = []

        for sz, paths in candidates.items():
            for p in paths:
                batch_paths.append((p, sz))

        # Hash in batches
        HASH_BATCH = 30
        for i in range(0, len(batch_paths), HASH_BATCH):
            if self._cancel_flag.is_set():
                break
            chunk = batch_paths[i:i + HASH_BATCH]
            paths_str = " ".join(f"'{p}'" for p, _ in chunk)
            cmd = f"md5sum {paths_str} 2>/dev/null"
            out = self.adb.run_shell(cmd, serial, timeout=120)
            for line in out.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    md5, fpath = parts
                    # Find its size
                    fsz = 0
                    for p, s in chunk:
                        if p == fpath:
                            fsz = s
                            break
                    hash_groups.setdefault(md5, []).append((fpath, fsz))
            hashed += len(chunk)
            self._emit(CleanupMode.DUPLICATES, ModeProgress(
                mode=CleanupMode.DUPLICATES, phase="scanning",
                message=f"Hashing… {hashed}/{total_to_hash}",
                percent=40 + 50 * hashed / max(total_to_hash, 1),
            ))

        # 4. Build items: for each group with >1 file, mark all but first as removable
        for md5, group in hash_groups.items():
            if len(group) < 2:
                continue
            # Keep the first, mark the rest
            for fpath, fsz in group[1:]:
                est.items.append(CleanupItem(
                    path=fpath, size_bytes=fsz,
                    item_type="file",
                    detail=f"Duplicata de {group[0][0]}",
                    group=md5,
                ))

        return est

    # ------------------------------------------------------------------
    # Execute implementations
    # ------------------------------------------------------------------

    def _clean_app_cache(self, serial: str, est: ModeEstimate) -> ModeResult:
        res = ModeResult(mode=CleanupMode.APP_CACHE)
        mode = CleanupMode.APP_CACHE

        # pm trim-caches
        self._emit(mode, ModeProgress(
            mode=mode, phase="cleaning", message="pm trim-caches…", percent=5,
        ))
        try:
            self.adb.run_shell("pm trim-caches 999999999999999", serial, timeout=120)
        except Exception as exc:
            res.errors.append(f"pm trim-caches: {exc}")

        # rm -rf per-app caches in batches
        total = len(est.items)
        batch_size = 60  # 30 packages × 2 paths
        for i in range(0, total, batch_size):
            if self._cancel_flag.is_set():
                break
            chunk = est.items[i:i + batch_size]
            targets = " ".join(f"'{item.path}'" for item in chunk)
            try:
                self.adb.run_shell(f"rm -rf {targets} 2>/dev/null", serial, timeout=30)
            except Exception as exc:
                res.errors.append(str(exc))
            res.items_removed += len(chunk)
            res.bytes_freed += sum(item.size_bytes for item in chunk)
            pct = 10 + 90 * min((i + batch_size) / max(total, 1), 1.0)
            self._emit(mode, ModeProgress(
                mode=mode, phase="cleaning",
                message=f"Limpando cache… {min(i + batch_size, total)}/{total}",
                percent=pct, items_done=res.items_removed, items_total=total,
                bytes_freed=res.bytes_freed,
            ))

        self._emit(mode, ModeProgress(
            mode=mode, phase="complete",
            message=f"Concluído — {format_bytes(res.bytes_freed)} liberados",
            percent=100, items_done=res.items_removed, items_total=total,
            bytes_freed=res.bytes_freed,
        ))
        return res

    def _clean_dirs(self, serial: str, est: ModeEstimate) -> ModeResult:
        """Generic directory removal (used by junk_dirs, known_junk, orphans)."""
        res = ModeResult(mode=est.mode)
        total = len(est.items)
        batch_size = 20

        for i in range(0, total, batch_size):
            if self._cancel_flag.is_set():
                break
            chunk = est.items[i:i + batch_size]
            targets = " ".join(f"'{item.path}'" for item in chunk)
            try:
                self.adb.run_shell(f"rm -rf {targets} 2>/dev/null", serial, timeout=60)
            except Exception as exc:
                res.errors.append(str(exc))
            res.items_removed += len(chunk)
            res.bytes_freed += sum(item.size_bytes for item in chunk)
            pct = 100 * min((i + batch_size) / max(total, 1), 1.0)
            self._emit(est.mode, ModeProgress(
                mode=est.mode, phase="cleaning",
                message=f"Removendo… {min(i + batch_size, total)}/{total}",
                percent=pct, items_done=res.items_removed, items_total=total,
                bytes_freed=res.bytes_freed,
            ))

        self._emit(est.mode, ModeProgress(
            mode=est.mode, phase="complete",
            message=f"Concluído — {format_bytes(res.bytes_freed)} liberados",
            percent=100, items_done=res.items_removed, items_total=total,
            bytes_freed=res.bytes_freed,
        ))
        return res

    def _clean_files(self, serial: str, est: ModeEstimate) -> ModeResult:
        """Generic file removal (used by junk_files, duplicates)."""
        res = ModeResult(mode=est.mode)
        total = len(est.items)
        batch_size = 50

        for i in range(0, total, batch_size):
            if self._cancel_flag.is_set():
                break
            chunk = est.items[i:i + batch_size]
            targets = " ".join(f"'{item.path}'" for item in chunk)
            try:
                self.adb.run_shell(f"rm -f {targets} 2>/dev/null", serial, timeout=30)
            except Exception as exc:
                res.errors.append(str(exc))
            res.items_removed += len(chunk)
            res.bytes_freed += sum(item.size_bytes for item in chunk)
            pct = 100 * min((i + batch_size) / max(total, 1), 1.0)
            self._emit(est.mode, ModeProgress(
                mode=est.mode, phase="cleaning",
                message=f"Removendo… {min(i + batch_size, total)}/{total}",
                percent=pct, items_done=res.items_removed, items_total=total,
                bytes_freed=res.bytes_freed,
            ))

        self._emit(est.mode, ModeProgress(
            mode=est.mode, phase="complete",
            message=f"Concluído — {format_bytes(res.bytes_freed)} liberados",
            percent=100, items_done=res.items_removed, items_total=total,
            bytes_freed=res.bytes_freed,
        ))
        return res

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(self, mode: CleanupMode, progress: ModeProgress):
        cb = self._progress_cbs.get(mode)
        if cb:
            try:
                cb(progress)
            except Exception:
                pass

    def _measure_dirs(self, serial: str, dirs: List[str]) -> Dict[str, int]:
        result: Dict[str, int] = {}
        batch = 20
        for i in range(0, len(dirs), batch):
            chunk = dirs[i:i + batch]
            targets = " ".join(f"'{d}'" for d in chunk)
            out = self.adb.run_shell(
                f"du -sk {targets} 2>/dev/null", serial, timeout=60,
            )
            for line in out.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    try:
                        result[parts[1]] = int(parts[0]) * 1024
                    except ValueError:
                        pass
        return result

    def _measure_files(self, serial: str, files: List[str]) -> Dict[str, int]:
        result: Dict[str, int] = {}
        batch = 50
        for i in range(0, len(files), batch):
            chunk = files[i:i + batch]
            targets = " ".join(f"'{f}'" for f in chunk)
            out = self.adb.run_shell(
                f"stat -c '%n|%s' {targets} 2>/dev/null", serial, timeout=30,
            )
            for line in out.splitlines():
                line = line.strip()
                if "|" not in line:
                    continue
                parts = line.rsplit("|", 1)
                if len(parts) == 2:
                    try:
                        result[parts[0]] = int(parts[1])
                    except ValueError:
                        pass
        return result

    def _fetch_installed_packages(self, serial: str) -> Optional[Set[str]]:
        for attempt in range(1, 3):
            try:
                pkgs = self.adb.list_packages(serial, third_party=False)
            except Exception as exc:
                log.warning("list_packages attempt %d failed: %s", attempt, exc)
                continue
            pkg_set = set(pkgs)
            if len(pkg_set) < _MIN_PACKAGES_THRESHOLD:
                continue
            found_canaries = pkg_set & _CANARY_PACKAGES
            if not found_canaries:
                continue
            canary = next(iter(found_canaries))
            check = self.adb.run_shell(
                f"pm path '{canary}' 2>/dev/null", serial, timeout=10,
            )
            if not check.strip():
                continue
            return pkg_set
        return None
