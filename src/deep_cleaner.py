"""
deep_cleaner.py — Deep device cleanup: caches, preload, dumps, logs & thumbnails.

Performs an aggressive multi-stage wipe of expendable data on a connected
Android device via ADB.  Every deletion is logged so the user has full
visibility.

Stages
------
1. ``pm trim-caches``          — ask the package-manager to flush all app caches
2. Per-app cache rm            — ``rm -rf`` on ``cache/`` and ``code_cache/``
                                 inside every ``/data/data/<pkg>`` directory
3. Deep storage scan           — ``find`` across ``/sdcard``, ``/data/local``,
                                 ``/storage/emulated/0`` (and ext-SD if present)
                                 looking for directories/files whose name
                                 matches *cache*, *preload*, *dump*, *log*,
                                 *thumbnail* / *.thumb* (all case-insensitive)
4. Well-known expendable paths — removes the classic Android junk locations
   (``/data/log*``, ``LOST.DIR``, ``/data/tombstones``, …)
5. Orphan purge                — cross-references directories under
   ``/sdcard/Android/{data,media,obb}`` and ``/data/data`` against the
   list of installed packages;  any folder whose name looks like a package
   but has no matching installed app is considered orphaned and removed.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .adb_core import ADBCore

log = logging.getLogger("adb_toolkit.deep_cleaner")


# ---------------------------------------------------------------------------
# Regex patterns that decide what gets nuked
# ---------------------------------------------------------------------------
# Directories whose *name* (basename) matches any of these are removed
_DIR_PATTERNS = re.compile(
    r"^("
    # cache-family
    r"[^/]*cache[^/]*"
    r"|[^/]*preload[^/]*"
    # dumps
    r"|dumps?|core[-_]?dumps?"
    # logs
    r"|logs?|logcat|bugreports?"
    # thumbnails
    r"|\.?thumbnails?|\.?thumbs?|\.Thumbs"
    # misc junk
    r"|LOST\.DIR|\.Trash|\.trashbin|tmp|temp"
    r")$",
    re.IGNORECASE,
)

# Individual *files* that should be removed (by extension or exact name)
_FILE_PATTERNS = re.compile(
    r"\.(log|logs|tmp|temp|bak|dmp|mdmp|core|thumb|cache)$"
    r"|(^|/)thumbs\.db$"
    r"|(^|/)desktop\.ini$"
    r"|(^|/)Thumbdata[^/]*$"
    r"|(^|/)logcat[^/]*\.txt$",
    re.IGNORECASE,
)

# Absolute paths that are always safe to wipe (contents only)
_KNOWN_JUNK: List[str] = [
    "/data/log",
    "/data/logs",
    "/data/logcat",
    "/data/tombstones",
    "/data/anr",
    "/data/local/tmp",
    "/data/vendor/logs",
    "/sdcard/LOST.DIR",
    "/storage/emulated/0/LOST.DIR",
    "/sdcard/.thumbnails",
    "/storage/emulated/0/.thumbnails",
    "/sdcard/.thumbs",
    "/sdcard/Android/data/com.android.providers.media/albumthumbs",
    "/sdcard/DCIM/.thumbnails",
    "/storage/emulated/0/DCIM/.thumbnails",
]


# ---------------------------------------------------------------------------
# Progress / result dataclass
# ---------------------------------------------------------------------------
@dataclass
class CleanResult:
    """Cumulative result of a deep-clean run."""
    dirs_removed: int = 0
    files_removed: int = 0
    orphans_removed: int = 0
    bytes_freed: int = 0
    errors: List[str] = field(default_factory=list)
    details: List[str] = field(default_factory=list)  # human-readable log


# ---------------------------------------------------------------------------
# Deep Cleaner
# ---------------------------------------------------------------------------
class DeepCleaner:
    """Orchestrates a multi-stage device cleanup."""

    # Roots to scan with ``find``
    _SCAN_ROOTS = [
        "/sdcard",
        "/storage/emulated/0",
        "/data/data",
        "/data/user/0",
        "/data/local",
        "/data/media/0",
    ]

    def __init__(self, adb: ADBCore, serial: str):
        self.adb = adb
        self.serial = serial
        self._progress_cb: Optional[Callable[[str, float], None]] = None

    def set_progress_callback(self, cb: Callable[[str, float], None]):
        """Register ``cb(message, percent)`` to receive live updates."""
        self._progress_cb = cb

    # -- public API --------------------------------------------------------

    def run(self, *, dry_run: bool = False) -> CleanResult:
        """Execute the full cleanup pipeline.

        Parameters
        ----------
        dry_run:
            If ``True``, list what *would* be removed but don't delete.

        Returns
        -------
        CleanResult with stats.
        """
        result = CleanResult()
        t0 = time.time()

        self._notify("Iniciando limpeza profunda…", 0)

        # Stage 1 — pm trim-caches
        self._stage_pm_trim(result, dry_run)

        # Stage 2 — per-app cache rm
        self._stage_per_app_cache(result, dry_run)

        # Stage 3 — deep find on storage roots for matching dirs
        self._stage_deep_scan_dirs(result, dry_run)

        # Stage 4 — deep find for matching *files* (logs, dumps, thumbs)
        self._stage_deep_scan_files(result, dry_run)

        # Stage 5 — well-known junk paths
        self._stage_known_junk(result, dry_run)

        # Stage 6 — orphan purge (uninstalled app leftovers)
        self._stage_orphan_purge(result, dry_run)

        elapsed = time.time() - t0
        summary = (
            f"Limpeza concluída em {elapsed:.1f}s  —  "
            f"{result.dirs_removed} diretórios, "
            f"{result.files_removed} arquivos, "
            f"{result.orphans_removed} órfãos removidos, "
            f"~{_fmt(result.bytes_freed)} liberados"
        )
        result.details.append(summary)
        self._notify(summary, 100)
        log.info(summary)
        return result

    # -- internal stages ---------------------------------------------------

    def _stage_pm_trim(self, res: CleanResult, dry: bool):
        """Stage 1: ``pm trim-caches`` — system-level cache flush."""
        self._notify("Estágio 1/6 — pm trim-caches …", 4)
        if not dry:
            out = self.adb.run_shell(
                "pm trim-caches 999999999999999", self.serial, timeout=120,
            )
            log.info("pm trim-caches: %s", out or "(ok)")
        res.details.append("pm trim-caches executado")

    def _stage_per_app_cache(self, res: CleanResult, dry: bool):
        """Stage 2: rm -rf on cache/ & code_cache/ for every package."""
        self._notify("Estágio 2/6 — limpando cache por app …", 12)
        pkgs = self.adb.list_packages(self.serial, third_party=False)
        log.info("Pacotes instalados: %d", len(pkgs))

        # Build a comprehensive rm command in batches to avoid arg-length limits
        batch_size = 30
        for i in range(0, len(pkgs), batch_size):
            chunk = pkgs[i: i + batch_size]
            parts = []
            for pkg in chunk:
                for base in ("/data/data", "/data/user/0"):
                    parts.append(f"{base}/{pkg}/cache")
                    parts.append(f"{base}/{pkg}/code_cache")
            targets = " ".join(f"'{p}'" for p in parts)
            cmd = f"rm -rf {targets} 2>/dev/null; echo OK"
            if not dry:
                self.adb.run_shell(cmd, self.serial, timeout=30)
            res.dirs_removed += len(parts)

        pct = 25
        self._notify(f"Cache de {len(pkgs)} apps limpo", pct)
        res.details.append(f"Cache de {len(pkgs)} apps removido")

    def _stage_deep_scan_dirs(self, res: CleanResult, dry: bool):
        """Stage 3: find directories matching cache/preload/dump/log/thumb."""
        self._notify("Estágio 3/6 — escaneando diretórios …", 24)

        # Build a single find expression with -iname alternatives
        names = [
            "*cache*", "*Cache*", "*CACHE*",
            "*preload*", "*Preload*",
            "dump", "dumps", "core_dump*",
            "log", "logs", "logcat", "bugreport*",
            ".thumbnails", "thumbnails", ".thumbs", "thumbs", ".Thumbs",
            "LOST.DIR", ".Trash", ".trashbin", "tmp", "temp",
        ]
        # We use -iname so case is covered; keep deduplicated lowercase set
        unique_lower: set[str] = set()
        iname_parts: list[str] = []
        for n in names:
            low = n.lower()
            if low not in unique_lower:
                unique_lower.add(low)
                iname_parts.append(f"-iname '{n}'")

        or_expr = " -o ".join(iname_parts)

        all_dirs: List[Tuple[str, int]] = []  # (path, size)

        for root in self._SCAN_ROOTS:
            cmd = (
                f'find "{root}" -maxdepth 6 -type d '
                f"\\( {or_expr} \\) 2>/dev/null"
            )
            out = self.adb.run_shell(cmd, self.serial, timeout=120)
            for line in out.splitlines():
                d = line.strip()
                if not d or not d.startswith("/"):
                    continue
                # Safety: never delete top-level known system partitions
                if d in ("/data", "/sdcard", "/storage", "/system", "/vendor"):
                    continue
                all_dirs.append((d, 0))

        # Deduplicate (different roots can overlap e.g. /sdcard vs /storage/emulated/0)
        seen: set[str] = set()
        unique_dirs: List[str] = []
        for d, _ in all_dirs:
            canon = d.replace("/storage/emulated/0", "/sdcard")
            if canon not in seen:
                seen.add(canon)
                unique_dirs.append(d)

        log.info("Diretórios candidatos: %d", len(unique_dirs))

        # Measure sizes (optional, best-effort)
        if unique_dirs:
            size_map = self._measure_dirs(unique_dirs)
        else:
            size_map = {}

        # Delete
        batch_size = 20
        for i in range(0, len(unique_dirs), batch_size):
            chunk = unique_dirs[i: i + batch_size]
            targets = " ".join(f"'{p}'" for p in chunk)
            cmd = f"rm -rf {targets} 2>/dev/null; echo OK"
            if not dry:
                self.adb.run_shell(cmd, self.serial, timeout=60)
            for d in chunk:
                sz = size_map.get(d, 0)
                res.bytes_freed += sz
                res.dirs_removed += 1
                res.details.append(f"{'[DRY] ' if dry else ''}rm -rf {d}  ({_fmt(sz)})")

            pct = 24 + int(30 * min((i + batch_size) / max(len(unique_dirs), 1), 1.0))
            self._notify(f"Removendo diretórios… {i + len(chunk)}/{len(unique_dirs)}", pct)

    def _stage_deep_scan_files(self, res: CleanResult, dry: bool):
        """Stage 4: find loose files matching log/dump/thumb patterns."""
        self._notify("Estágio 4/6 — escaneando arquivos avulsos …", 56)

        extensions = ["log", "tmp", "temp", "bak", "dmp", "mdmp", "core", "thumb"]
        iname_parts = " -o ".join(f"-iname '*.{e}'" for e in extensions)
        # also exact names
        exact = [
            "thumbs.db", "desktop.ini", "Thumbdata*", "logcat*.txt",
        ]
        exact_parts = " -o ".join(f"-iname '{n}'" for n in exact)
        full_expr = f"\\( {iname_parts} -o {exact_parts} \\)"

        all_files: List[str] = []

        scan_roots = ["/sdcard", "/storage/emulated/0", "/data/local"]
        for root in scan_roots:
            cmd = (
                f'find "{root}" -maxdepth 8 -type f {full_expr} 2>/dev/null'
            )
            out = self.adb.run_shell(cmd, self.serial, timeout=90)
            for line in out.splitlines():
                f = line.strip()
                if f and f.startswith("/"):
                    all_files.append(f)

        # Deduplicate
        seen: set[str] = set()
        unique_files: List[str] = []
        for f in all_files:
            canon = f.replace("/storage/emulated/0", "/sdcard")
            if canon not in seen:
                seen.add(canon)
                unique_files.append(f)

        log.info("Arquivos candidatos: %d", len(unique_files))

        batch_size = 50
        for i in range(0, len(unique_files), batch_size):
            chunk = unique_files[i: i + batch_size]
            targets = " ".join(f"'{p}'" for p in chunk)
            cmd = f"rm -f {targets} 2>/dev/null; echo OK"
            if not dry:
                self.adb.run_shell(cmd, self.serial, timeout=30)
            res.files_removed += len(chunk)
            for f in chunk:
                res.details.append(f"{'[DRY] ' if dry else ''}rm {f}")

            pct = 56 + int(10 * min((i + batch_size) / max(len(unique_files), 1), 1.0))
            self._notify(f"Removendo arquivos… {i + len(chunk)}/{len(unique_files)}", pct)

    def _stage_known_junk(self, res: CleanResult, dry: bool):
        """Stage 5: wipe well-known Android junk directories."""
        self._notify("Estágio 5/6 — limpando locais conhecidos …", 68)

        for jpath in _KNOWN_JUNK:
            # Check existence first to avoid noisy errors
            exists = self.adb.run_shell(
                f"[ -d '{jpath}' ] && echo Y || echo N", self.serial, timeout=5,
            )
            if not exists.startswith("Y"):
                continue
            size_out = self.adb.run_shell(
                f"du -sk '{jpath}' 2>/dev/null | head -1", self.serial, timeout=10,
            )
            sz = 0
            try:
                sz = int(size_out.split()[0]) * 1024
            except (ValueError, IndexError):
                pass

            if not dry:
                self.adb.run_shell(f"rm -rf '{jpath}' 2>/dev/null", self.serial, timeout=30)

            res.dirs_removed += 1
            res.bytes_freed += sz
            res.details.append(f"{'[DRY] ' if dry else ''}rm -rf {jpath}  ({_fmt(sz)})")

    # Minimum number of packages a real Android device should have.
    # Even a very stripped-down ROM has 30+ system packages.  If we get
    # fewer than this, something went wrong with ``pm list packages``.
    _MIN_PACKAGES_THRESHOLD = 15

    # System packages that **must** exist on any Android device.  We use
    # them as a canary: if none of these appear in the list, the query
    # almost certainly failed.
    _CANARY_PACKAGES = frozenset({
        "android",
        "com.android.settings",
        "com.android.systemui",
        "com.android.phone",
        "com.android.providers.settings",
    })

    def _fetch_installed_packages(self) -> Optional[set[str]]:
        """Return the set of installed packages, or ``None`` on failure.

        Performs two independent attempts and validates the result with a
        minimum-size threshold **and** a canary-package check.  Returns
        ``None`` if validation fails — callers must treat this as
        "unsafe to proceed".
        """
        for attempt in range(1, 3):
            try:
                pkgs = self.adb.list_packages(self.serial, third_party=False)
            except Exception as exc:
                log.warning("list_packages tentativa %d falhou: %s", attempt, exc)
                continue

            pkg_set = set(pkgs)
            count = len(pkg_set)

            # --- Gate 1: minimum size ---
            if count < self._MIN_PACKAGES_THRESHOLD:
                log.warning(
                    "list_packages retornou apenas %d pacotes (tentativa %d). "
                    "Mínimo esperado: %d",
                    count, attempt, self._MIN_PACKAGES_THRESHOLD,
                )
                continue

            # --- Gate 2: canary packages ---
            found_canaries = pkg_set & self._CANARY_PACKAGES
            if not found_canaries:
                log.warning(
                    "Nenhum pacote-canário encontrado na lista (tentativa %d). "
                    "Pacotes-canário: %s",
                    attempt, self._CANARY_PACKAGES,
                )
                continue

            # --- Gate 3 (optional): quick pm path spot-check ---
            canary = next(iter(found_canaries))
            check = self.adb.run_shell(
                f"pm path '{canary}' 2>/dev/null", self.serial, timeout=10,
            )
            if not check.strip():
                log.warning(
                    "pm path '%s' retornou vazio apesar de constar na lista "
                    "(tentativa %d). Resultado incoerente.",
                    canary, attempt,
                )
                continue

            log.info(
                "Lista de pacotes validada: %d pacotes, %d canários (%s)",
                count, len(found_canaries), ", ".join(sorted(found_canaries)),
            )
            return pkg_set

        # Both attempts failed validation
        return None

    def _stage_orphan_purge(self, res: CleanResult, dry: bool):
        """Stage 6: remove directories left behind by uninstalled apps.

        Scans ``/sdcard/Android/{data,media,obb}`` and ``/data/data`` for
        sub-directories that look like Java package names (e.g.
        ``com.example.app``) but have **no** matching installed package.

        **Safety**: if the installed-packages list cannot be reliably
        obtained (empty, too small, or missing expected system packages),
        this stage is skipped entirely to avoid false-positive deletions.
        """
        self._notify("Estágio 6/6 — detectando arquivos órfãos …", 78)

        # 1. Get installed packages with robust validation
        installed = self._fetch_installed_packages()

        if installed is None:
            msg = (
                "⚠️  ABORTANDO orphan purge — não foi possível obter uma "
                "lista confiável de pacotes instalados. Nenhum órfão será removido."
            )
            log.error(msg)
            res.errors.append(msg)
            res.details.append(msg)
            self._notify(msg, 85)
            return

        log.info("Pacotes instalados (para detecção de órfãos): %d", len(installed))

        # 2. Directories to scan for package-named sub-folders
        orphan_roots = [
            "/sdcard/Android/data",
            "/sdcard/Android/media",
            "/sdcard/Android/obb",
            "/storage/emulated/0/Android/data",
            "/storage/emulated/0/Android/media",
            "/storage/emulated/0/Android/obb",
            "/data/data",
            "/data/user/0",
        ]

        # 3. Collect all sub-dirs (depth 1) under each root
        pkg_re = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")
        orphans: List[Tuple[str, str]] = []  # (full_path, pkg_name)

        for root in orphan_roots:
            out = self.adb.run_shell(
                f"ls -1 '{root}' 2>/dev/null", self.serial, timeout=15,
            )
            for name in out.splitlines():
                name = name.strip()
                if not name or not pkg_re.match(name):
                    continue
                if name in installed:
                    continue
                # Skip known system/android packages that may not appear in pm list
                if name.startswith(("com.android.", "com.google.android.")):
                    # Double-check with pm path just to be safe
                    check = self.adb.run_shell(
                        f"pm path '{name}' 2>/dev/null", self.serial, timeout=5,
                    )
                    if check.strip():
                        continue
                full = f"{root}/{name}"
                orphans.append((full, name))

        # Deduplicate across overlapping mount-points
        seen: set[str] = set()
        unique_orphans: List[Tuple[str, str]] = []
        for full, pkg in orphans:
            canon = full.replace("/storage/emulated/0", "/sdcard")
            if canon not in seen:
                seen.add(canon)
                unique_orphans.append((full, pkg))

        log.info("Pastas órfãs detectadas: %d", len(unique_orphans))
        res.details.append(f"Pastas órfãs detectadas: {len(unique_orphans)}")

        if not unique_orphans:
            self._notify("Nenhum órfão encontrado", 85)
            return

        # 4. Measure and delete
        orphan_dirs = [full for full, _ in unique_orphans]
        size_map = self._measure_dirs(orphan_dirs) if orphan_dirs else {}

        batch_size = 15
        for i in range(0, len(unique_orphans), batch_size):
            chunk = unique_orphans[i: i + batch_size]
            targets = " ".join(f"'{full}'" for full, _ in chunk)
            cmd = f"rm -rf {targets} 2>/dev/null; echo OK"
            if not dry:
                self.adb.run_shell(cmd, self.serial, timeout=60)
            for full, pkg in chunk:
                sz = size_map.get(full, 0)
                res.bytes_freed += sz
                res.dirs_removed += 1
                res.orphans_removed += 1
                res.details.append(
                    f"{'[DRY] ' if dry else ''}ÓRFÃO rm -rf {full}  "
                    f"(pkg={pkg}, {_fmt(sz)})"
                )

            pct = 78 + int(17 * min((i + batch_size) / max(len(unique_orphans), 1), 1.0))
            self._notify(
                f"Removendo órfãos… {min(i + batch_size, len(unique_orphans))}/{len(unique_orphans)}",
                pct,
            )

    # -- helpers -----------------------------------------------------------

    def _measure_dirs(self, dirs: List[str]) -> Dict[str, int]:
        """Best-effort ``du -sk`` on a list of directories."""
        result: Dict[str, int] = {}
        batch_size = 20
        for i in range(0, len(dirs), batch_size):
            chunk = dirs[i: i + batch_size]
            targets = " ".join(f"'{d}'" for d in chunk)
            out = self.adb.run_shell(
                f"du -sk {targets} 2>/dev/null", self.serial, timeout=60,
            )
            for line in out.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    try:
                        kb = int(parts[0])
                        result[parts[1]] = kb * 1024
                    except ValueError:
                        pass
        return result

    def _notify(self, msg: str, pct: float):
        log.info("[%3.0f%%] %s", pct, msg)
        if self._progress_cb:
            try:
                self._progress_cb(msg, pct)
            except Exception:
                pass


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
