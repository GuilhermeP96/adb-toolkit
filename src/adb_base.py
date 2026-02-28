"""
adb_base.py — Shared base infrastructure for Backup, Restore and Transfer managers.

Centralises:
  • OperationProgress — unified progress dataclass (superset of the old
    BackupProgress / TransferProgress).
  • CACHE_PATTERNS / THUMBNAIL_DUMP_PATTERNS — compiled regexes for
    path-level filtering.
  • ADBManagerBase — mixin that provides cancel-flag management, elapsed-time
    tracking, progress emission and common ADB helpers (list_remote_files,
    pull_with_progress, push_with_progress).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple, TYPE_CHECKING

from .adb_core import ADBCore

if TYPE_CHECKING:
    from .accelerator import TransferAccelerator

log = logging.getLogger("adb_toolkit.base")


# ---------------------------------------------------------------------------
# Filter regexes (used by backup, transfer, and optionally restore)
# ---------------------------------------------------------------------------
CACHE_PATTERNS = re.compile(
    r"(/|^)([^/]*(?:cache|preload)[^/]*|tmp|temp)(/|$)",
    re.IGNORECASE,
)

THUMBNAIL_DUMP_PATTERNS = re.compile(
    r"(/|^)(\.thumbnails|\.Thumbs|thumbs|thumbnails|thumbnail|"
    r"\.thumb|dump|\.dump|\.trashbin|\.Trash|LOST\.DIR)(/|$)"
    r"|\.(thumb|dmp|mdmp|core)$"
    r"|(thumbs\.db|desktop\.ini)$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Unified progress dataclass
# ---------------------------------------------------------------------------
@dataclass
class OperationProgress:
    """Progress update that every manager can emit.

    Fields are a *superset* of the old BackupProgress and TransferProgress.
    Consumers may ignore fields that are irrelevant for a given operation.
    """

    phase: str = ""
    sub_phase: str = ""
    current_item: str = ""
    items_done: int = 0
    items_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    percent: float = 0.0
    speed_bps: float = 0.0
    elapsed_seconds: float = 0.0
    eta_seconds: float = 0.0
    source_device: str = ""
    target_device: str = ""
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Convenience helper
# ---------------------------------------------------------------------------
def safe_percent(done: float, total: float) -> float:
    """Return ``done / total * 100`` without division by zero."""
    return (done / total * 100) if total > 0 else 0.0


# ---------------------------------------------------------------------------
# Base manager class
# ---------------------------------------------------------------------------
class ADBManagerBase:
    """Common infrastructure shared by BackupManager, RestoreManager and
    TransferManager.

    Sub-classes should call ``super().__init__(adb)`` at the top of their
    own ``__init__``.
    """

    def __init__(self, adb: ADBCore):
        self.adb = adb
        self._cancel_flag = threading.Event()
        self._progress_cb: Optional[Callable[[OperationProgress], None]] = None
        self._confirmation_cb: Optional[Callable[[str, str], None]] = None
        self._confirmation_dismiss_cb: Optional[Callable[[], None]] = None
        self._start_time: Optional[float] = None
        self._errors: List[str] = []
        self._accelerator: Optional[TransferAccelerator] = None

    # -- accelerator (lazy) ---------------------------------------------------
    @property
    def accelerator(self) -> TransferAccelerator:
        """Lazy-init TransferAccelerator for dynamic threading / GPU."""
        if self._accelerator is None:
            from .accelerator import TransferAccelerator as _TA
            self._accelerator = _TA()
        return self._accelerator

    # -- progress / cancel API ------------------------------------------------
    def set_progress_callback(self, cb: Callable[[OperationProgress], None]):
        self._progress_cb = cb

    def set_confirmation_callback(
        self,
        show_cb: Callable[[str, str], None],
        dismiss_cb: Callable[[], None],
    ):
        """Register callbacks for device-side confirmation prompts.

        Parameters
        ----------
        show_cb:
            ``show_cb(title, message)`` — called when the device is about
            to display a confirmation banner that the user must accept.
            Implementers should show a prominent, non-blocking notification.
        dismiss_cb:
            ``dismiss_cb()`` — called after the device-side action completes
            (or is cancelled) so the notification can be hidden.
        """
        self._confirmation_cb = show_cb
        self._confirmation_dismiss_cb = dismiss_cb

    def cancel(self):
        self._cancel_flag.set()

    def _is_cancelled(self) -> bool:
        return self._cancel_flag.is_set()

    def _begin_operation(self):
        """Call at the start of any top-level operation."""
        self._cancel_flag.clear()
        self._start_time = time.time()
        self._errors.clear()

    # -- device confirmation helpers ------------------------------------------
    def _request_device_confirmation(self, title: str, message: str) -> None:
        """Notify the UI that the device requires user confirmation.

        This is non-blocking — it only triggers the UI overlay.
        """
        log.info("Device confirmation requested: %s — %s", title, message)
        if self._confirmation_cb:
            try:
                self._confirmation_cb(title, message)
            except Exception:
                pass

    def _dismiss_device_confirmation(self) -> None:
        """Hide the device-confirmation overlay in the UI."""
        if self._confirmation_dismiss_cb:
            try:
                self._confirmation_dismiss_cb()
            except Exception:
                pass

    def _run_with_confirmation(
        self,
        args: List[str],
        serial: str,
        *,
        title: str,
        message: str,
        timeout: int = 7200,
    ):
        """Run an ADB command that requires device-side user confirmation.

        Shows the confirmation overlay before executing, waits for the
        command to finish (up to *timeout* seconds), then dismisses the
        overlay.  Returns the ``CompletedProcess`` result.
        """
        self._request_device_confirmation(title, message)
        try:
            result = self.adb.run(args, serial=serial, timeout=timeout)
        finally:
            self._dismiss_device_confirmation()
        return result

    def _emit(self, progress: OperationProgress):
        """Send *progress* to the registered callback.

        Automatically fills ``elapsed_seconds`` from ``_start_time`` and
        ``eta_seconds`` if enough data is available.
        """
        if self._start_time is not None:
            progress.elapsed_seconds = time.time() - self._start_time
            # Compute ETA when percent > 0
            if progress.percent > 0:
                elapsed = progress.elapsed_seconds
                remaining_pct = 100.0 - progress.percent
                progress.eta_seconds = elapsed / progress.percent * remaining_pct
        # Attach accumulated errors
        if self._errors and not progress.errors:
            progress.errors = list(self._errors)
        if self._progress_cb:
            try:
                self._progress_cb(progress)
            except Exception:
                pass

    # -- shared ADB helpers ---------------------------------------------------
    def list_remote_files(
        self,
        serial: str,
        paths: List[str],
        *,
        ignore_cache: bool = False,
        ignore_thumbnails: bool = False,
        timeout: int = 180,
    ) -> List[Tuple[str, int]]:
        """List files on the device under *paths*.

        Runs ``find … | xargs stat -c '%n|%s'`` and parses the output.
        Optionally applies cache / thumbnail filters.

        Returns a list of ``(remote_path, size_bytes)`` tuples.
        """
        files: List[Tuple[str, int]] = []
        for rpath in paths:
            if self._is_cancelled():
                break
            try:
                cmd = (
                    f'find "{rpath}" -type f 2>/dev/null'
                    f" | xargs stat -c '%n|%s' 2>/dev/null"
                )
                out = self.adb.run_shell(cmd, serial, timeout=timeout)
                for line in out.splitlines():
                    line = line.strip()
                    if "|" not in line:
                        continue
                    parts = line.rsplit("|", 1)
                    if len(parts) != 2:
                        continue
                    path_str = parts[0]
                    try:
                        size = int(parts[1])
                    except ValueError:
                        size = 0

                    if ignore_cache and CACHE_PATTERNS.search(path_str):
                        continue
                    if ignore_thumbnails and THUMBNAIL_DUMP_PATTERNS.search(path_str):
                        continue

                    files.append((path_str, size))
            except Exception as exc:
                log.warning("Error listing files in %s: %s", rpath, exc)
        return files

    def pull_with_progress(
        self,
        serial: str,
        file_list: List[Tuple[str, int]],
        dest_root: Path,
        *,
        phase: str = "copying",
        sub_phase: str = "",
        strip_prefix: str = "/",
        pct_range: Tuple[float, float] = (0.0, 100.0),
    ) -> Tuple[int, int]:
        """Pull *file_list* from device to local *dest_root*.

        Uses :class:`TransferAccelerator` to determine the optimal number
        of parallel workers based on CPU cores, file count and average
        file size.  Falls back to sequential execution for tiny batches.

        Parameters
        ----------
        file_list:
            List of ``(remote_path, size_bytes)`` tuples.
        dest_root:
            Local directory to pull files into (directory structure preserved).
        strip_prefix:
            Prefix to strip from the remote path before joining with
            *dest_root*.  Defaults to ``"/"``.
        pct_range:
            ``(min_pct, max_pct)`` — maps progress to this percent range.

        Returns
        -------
        ``(success_count, total_bytes_pulled)``
        """
        total_files = len(file_list)
        if total_files == 0:
            return 0, 0

        total_bytes = sum(s for _, s in file_list)
        pct_lo, pct_hi = pct_range
        pct_span = pct_hi - pct_lo

        # --- determine parallelism ---
        avg_size = total_bytes // max(total_files, 1)
        workers = self.accelerator.optimal_workers(
            total_files, avg_size_bytes=avg_size,
        )

        if workers <= 1 or total_files <= 2:
            # Sequential for trivial batches
            return self._pull_sequential(
                serial, file_list, dest_root,
                phase=phase, sub_phase=sub_phase,
                strip_prefix=strip_prefix, pct_range=pct_range,
            )

        log.info(
            "Parallel pull: %d files, %d workers (avg %d bytes)",
            total_files, workers, avg_size,
        )

        # Pre-create local directories
        for remote_path, _ in file_list:
            rel = remote_path.lstrip(strip_prefix).lstrip("/")
            (dest_root / rel).parent.mkdir(parents=True, exist_ok=True)

        _lock = threading.Lock()
        counters = {"ok": 0, "bytes": 0, "items": 0}

        def _pull_one(remote_path: str, fsize: int) -> None:
            if self._is_cancelled():
                return
            rel = remote_path.lstrip(strip_prefix).lstrip("/")
            local_path = dest_root / rel
            ok = False
            try:
                self.adb.pull(remote_path, str(local_path), serial)
                ok = True
            except Exception as exc:
                log.warning("Pull failed: %s — %s", remote_path, exc)
                with _lock:
                    self._errors.append(
                        f"Pull falhou: {os.path.basename(remote_path)}"
                    )
            with _lock:
                if ok:
                    counters["ok"] += 1
                counters["bytes"] += fsize
                counters["items"] += 1
                pct = pct_lo + (
                    safe_percent(counters["bytes"], total_bytes)
                    / 100.0 * pct_span
                )
                self._emit(OperationProgress(
                    phase=phase,
                    sub_phase=sub_phase,
                    current_item=os.path.basename(remote_path),
                    items_done=counters["items"],
                    items_total=total_files,
                    bytes_done=counters["bytes"],
                    bytes_total=total_bytes,
                    percent=pct,
                ))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_pull_one, rp, fs): rp
                for rp, fs in file_list
            }
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    pass

        return counters["ok"], counters["bytes"]

    # -- sequential fallback for pull --
    def _pull_sequential(
        self,
        serial: str,
        file_list: List[Tuple[str, int]],
        dest_root: Path,
        *,
        phase: str = "copying",
        sub_phase: str = "",
        strip_prefix: str = "/",
        pct_range: Tuple[float, float] = (0.0, 100.0),
    ) -> Tuple[int, int]:
        """Sequential pull — used when parallelism is unnecessary."""
        total_files = len(file_list)
        total_bytes = sum(s for _, s in file_list)
        pct_lo, pct_hi = pct_range
        pct_span = pct_hi - pct_lo

        success_count = 0
        bytes_done = 0

        for idx, (remote_path, fsize) in enumerate(file_list):
            if self._is_cancelled():
                break

            rel = remote_path.lstrip(strip_prefix).lstrip("/")
            local_path = dest_root / rel
            local_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                self.adb.pull(remote_path, str(local_path), serial)
                success_count += 1
            except Exception as exc:
                log.warning("Pull failed: %s — %s", remote_path, exc)
                self._errors.append(f"Pull falhou: {os.path.basename(remote_path)}")

            bytes_done += fsize
            pct = pct_lo + safe_percent(bytes_done, total_bytes) / 100.0 * pct_span

            self._emit(OperationProgress(
                phase=phase,
                sub_phase=sub_phase,
                current_item=os.path.basename(remote_path),
                items_done=idx + 1,
                items_total=total_files,
                bytes_done=bytes_done,
                bytes_total=total_bytes,
                percent=pct,
            ))

        return success_count, bytes_done

    def push_with_progress(
        self,
        serial: str,
        files_to_push: List[Tuple[Path, str]],
        *,
        phase: str = "restoring",
        sub_phase: str = "",
        pct_range: Tuple[float, float] = (0.0, 100.0),
    ) -> Tuple[int, int]:
        """Push local files to device.

        Uses :class:`TransferAccelerator` for optimal parallel worker
        count.  Falls back to sequential for tiny batches.

        Parameters
        ----------
        files_to_push:
            List of ``(local_path, remote_path)`` tuples.
        pct_range:
            ``(min_pct, max_pct)`` — maps progress to this percent range.

        Returns
        -------
        ``(success_count, total_bytes_pushed)``
        """
        total = len(files_to_push)
        if total == 0:
            return 0, 0

        total_bytes = sum(lp.stat().st_size for lp, _ in files_to_push)
        pct_lo, pct_hi = pct_range
        pct_span = pct_hi - pct_lo

        # Pre-create remote directories in batch (always)
        parent_dirs = set()
        for _, remote in files_to_push:
            parent_dirs.add(os.path.dirname(remote))

        dir_list = sorted(parent_dirs)
        for i in range(0, len(dir_list), 50):
            chunk = dir_list[i: i + 50]
            dirs_str = "' '".join(chunk)
            try:
                self.adb.run_shell(f"mkdir -p '{dirs_str}'", serial)
            except Exception:
                for d in chunk:
                    try:
                        self.adb.run_shell(f"mkdir -p '{d}'", serial)
                    except Exception:
                        pass

        # --- determine parallelism ---
        avg_size = total_bytes // max(total, 1)
        workers = self.accelerator.optimal_workers(
            total, avg_size_bytes=avg_size,
        )

        if workers <= 1 or total <= 2:
            return self._push_sequential(
                serial, files_to_push,
                phase=phase, sub_phase=sub_phase,
                pct_range=pct_range,
                total_bytes=total_bytes,
            )

        log.info(
            "Parallel push: %d files, %d workers (avg %d bytes)",
            total, workers, avg_size,
        )

        _lock = threading.Lock()
        counters = {"ok": 0, "bytes": 0, "items": 0}

        def _push_one(local_path: Path, remote_path: str, fsize: int) -> None:
            if self._is_cancelled():
                return
            ok = False
            try:
                if self.adb.push(str(local_path), remote_path, serial):
                    ok = True
            except Exception as exc:
                log.warning("Push failed: %s — %s", remote_path, exc)
                with _lock:
                    self._errors.append(
                        f"Push falhou: {os.path.basename(remote_path)}"
                    )
            with _lock:
                if ok:
                    counters["ok"] += 1
                counters["bytes"] += fsize
                counters["items"] += 1
                pct = pct_lo + (
                    safe_percent(counters["bytes"], total_bytes)
                    / 100.0 * pct_span
                )
                self._emit(OperationProgress(
                    phase=phase,
                    sub_phase=sub_phase,
                    current_item=os.path.basename(str(local_path)),
                    items_done=counters["items"],
                    items_total=total,
                    bytes_done=counters["bytes"],
                    bytes_total=total_bytes,
                    percent=pct,
                ))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {}
            for local_path, remote_path in files_to_push:
                fsize = local_path.stat().st_size
                fut = pool.submit(_push_one, local_path, remote_path, fsize)
                futs[fut] = remote_path
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    pass

        return counters["ok"], counters["bytes"]

    # -- sequential fallback for push --
    def _push_sequential(
        self,
        serial: str,
        files_to_push: List[Tuple[Path, str]],
        *,
        phase: str = "restoring",
        sub_phase: str = "",
        pct_range: Tuple[float, float] = (0.0, 100.0),
        total_bytes: int = 0,
    ) -> Tuple[int, int]:
        """Sequential push — used when parallelism is unnecessary."""
        total = len(files_to_push)
        if total_bytes == 0:
            total_bytes = sum(lp.stat().st_size for lp, _ in files_to_push)
        pct_lo, pct_hi = pct_range
        pct_span = pct_hi - pct_lo

        success_count = 0
        bytes_done = 0

        for idx, (local_path, remote_path) in enumerate(files_to_push):
            if self._is_cancelled():
                break

            fsize = local_path.stat().st_size

            try:
                if self.adb.push(str(local_path), remote_path, serial):
                    success_count += 1
            except Exception as exc:
                log.warning("Push failed: %s — %s", remote_path, exc)
                self._errors.append(f"Push falhou: {os.path.basename(remote_path)}")

            bytes_done += fsize
            pct = pct_lo + safe_percent(bytes_done, total_bytes) / 100.0 * pct_span

            self._emit(OperationProgress(
                phase=phase,
                sub_phase=sub_phase,
                current_item=os.path.basename(str(local_path)),
                items_done=idx + 1,
                items_total=total,
                bytes_done=bytes_done,
                bytes_total=total_bytes,
                percent=pct,
            ))

        return success_count, bytes_done
