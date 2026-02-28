"""
accelerator.py — Thin wrapper around **pyaccelerate** for the ADB Toolkit.

All heavy lifting (GPU/NPU enumeration, thread-pool management, priority
control, energy profiles) is delegated to the ``pyaccelerate`` package.
This module re-exports types and provides the ADB-specific helpers
(``parallel_checksum``, ``verify_transfer``, ``TransferAccelerator``).

Public API
----------
  - ``TransferAccelerator``      — orchestrator used by TransferManager
  - ``detect_all_gpus()``        — full GPU enumeration
  - ``detect_all_npus()``        — full NPU enumeration
  - ``detect_virtualization()``  — returns VirtInfo
  - ``parallel_checksum()``      — batch-hash files
  - ``verify_transfer()``        — compare checksums after clone
  - ``gpu_available()``          — quick boolean
  - ``npu_available()``          — quick boolean
"""

from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ── pyaccelerate imports ────────────────────────────────────────────────
from pyaccelerate import (
    Engine,
    EnergyProfile,
    MaxMode,
    TaskPriority,
)
from pyaccelerate.gpu import (
    GPUDevice,
    detect_all as _detect_gpus,
    get_all_gpus_info,
    get_gpu_info,
    get_install_hint,
    gpu_available,
)
from pyaccelerate.npu import (
    NPUDevice,
    detect_all as _detect_npus,
    get_all_npus_info,
    get_install_hint as get_npu_install_hint,
    get_npu_info,
    npu_available,
)
from pyaccelerate.virt import VirtInfo
from pyaccelerate.virt import detect as _detect_virt
from pyaccelerate.cpu import detect as _detect_cpu
from pyaccelerate.memory import get_pressure, get_stats as get_mem_stats
from pyaccelerate.threads import get_pool, run_parallel as _threads_run_parallel
from pyaccelerate import (
    balanced as apply_balanced,
    max_performance as apply_max_performance,
    power_saver as apply_power_saver,
)

log = logging.getLogger("adb_toolkit.accelerator")


# ═══════════════════════════════════════════════════════════════════════════
#  Convenience re-exports  (keeps existing import lines working)
# ═══════════════════════════════════════════════════════════════════════════

def detect_all_gpus() -> List[GPUDevice]:
    """Enumerate ALL GPUs (delegates to pyaccelerate.gpu)."""
    return _detect_gpus()


def detect_all_npus() -> List[NPUDevice]:
    """Enumerate ALL NPUs (delegates to pyaccelerate.npu)."""
    return _detect_npus()


def detect_virtualization() -> VirtInfo:
    """Detect hardware virtualization (delegates to pyaccelerate.virt)."""
    return _detect_virt()


# ═══════════════════════════════════════════════════════════════════════════
#  Checksum computation  (ADB-specific — not in pyaccelerate)
# ═══════════════════════════════════════════════════════════════════════════
def _hash_file_cpu(path: str, algo: str = "md5") -> str:
    """Hash a single file on CPU (always available)."""
    h = hashlib.new(algo)
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
    except Exception as exc:
        log.warning("Cannot hash %s: %s", path, exc)
        return ""
    return h.hexdigest()


def parallel_checksum(
    file_paths: List[str],
    algo: str = "md5",
    max_workers: int = 8,
    use_gpu: bool = True,
    multi_gpu: bool = False,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, str]:
    """Compute checksums for many files in parallel.

    Parameters
    ----------
    use_gpu : bool
        Reserved for future GPU-accelerated hashing (currently CPU-only).
    multi_gpu : bool
        Reserved for future multi-GPU hashing.
    """
    results: Dict[str, str] = {}
    total = len(file_paths)
    done_count = 0

    # Use the shared pyaccelerate I/O pool
    try:
        pool = get_pool()
    except Exception:
        pool = ThreadPoolExecutor(max_workers=max_workers)

    futures = {pool.submit(_hash_file_cpu, fp, algo): fp for fp in file_paths}

    for fut in as_completed(futures):
        fp = futures[fut]
        done_count += 1
        try:
            results[fp] = fut.result()
        except Exception:
            results[fp] = ""
        if progress_cb:
            try:
                progress_cb(done_count, total, fp)
            except Exception:
                pass

    log.info("Checksummed %d files via CPU (%d workers)", total, max_workers)
    return results


# ═══════════════════════════════════════════════════════════════════════════
#  Transfer verification  (ADB-specific)
# ═══════════════════════════════════════════════════════════════════════════
def verify_transfer(
    staging_dir: Path,
    storage_path: str,
    adb_core,
    target_serial: str,
    algo: str = "md5",
    max_workers: int = 8,
    use_gpu: bool = True,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[int, int, List[str]]:
    """Verify pushed files match local staging copies."""
    storage_staging = staging_dir / "storage"
    if not storage_staging.exists():
        return 0, 0, []

    local_files = [str(f) for f in storage_staging.rglob("*") if f.is_file()]
    if not local_files:
        return 0, 0, []

    local_sums = parallel_checksum(
        local_files, algo=algo, max_workers=max_workers,
        use_gpu=use_gpu, progress_cb=progress_cb,
    )

    rel_to_local: Dict[str, str] = {}
    for fp, digest in local_sums.items():
        rel = Path(fp).relative_to(storage_staging).as_posix()
        rel_to_local[rel] = digest

    md5_cmd = "md5sum" if algo == "md5" else f"{algo}sum"
    matched = 0
    total = len(rel_to_local)
    mismatched: List[str] = []

    rel_paths = list(rel_to_local.keys())
    batch_size = 50
    for i in range(0, len(rel_paths), batch_size):
        batch = rel_paths[i: i + batch_size]
        remote_paths = [f"{storage_path}/{r}" for r in batch]
        paths_str = "' '".join(remote_paths)
        try:
            out = adb_core.run_shell(
                f"{md5_cmd} '{paths_str}'", target_serial, timeout=120,
            )
            remote_digests: Dict[str, str] = {}
            for line in out.splitlines():
                line = line.strip()
                if not line or "No such file" in line:
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    rpath = parts[1].strip()
                    if rpath.startswith(storage_path):
                        rel = rpath[len(storage_path):].lstrip("/")
                        remote_digests[rel] = parts[0]

            for rel in batch:
                ld = rel_to_local.get(rel, "")
                rd = remote_digests.get(rel, "")
                if ld and rd and ld == rd:
                    matched += 1
                else:
                    mismatched.append(rel)
        except Exception as exc:
            log.warning("Remote checksum batch failed: %s", exc)
            mismatched.extend(batch)

    log.info(
        "Verification: %d/%d matched, %d mismatched", matched, total, len(mismatched),
    )
    return matched, total, mismatched


# ═══════════════════════════════════════════════════════════════════════════
#  TransferAccelerator  — wraps pyaccelerate.Engine
# ═══════════════════════════════════════════════════════════════════════════
class TransferAccelerator:
    """Controls GPU/NPU acceleration, multi-threading, energy profile,
    and virtualization for the transfer pipeline.

    Wraps :class:`pyaccelerate.Engine` while preserving the same external
    API used by the rest of ADB Toolkit.

    New pyaccelerate features exposed
    ----------------------------------
    * **NPU detection / toggle** — ``npus``, ``usable_npus``, ``best_npu``,
      ``set_npu_enabled()``.
    * **Max-Performance mode** — ``max_mode()`` context manager that pins
      OS priority to HIGH, energy to ULTRA_PERFORMANCE, and I/O priority
      to high.
    * **Energy profiles** — ``set_energy()``, ``get_energy()`` for
      POWER_SAVER / BALANCED / PERFORMANCE / ULTRA_PERFORMANCE.
    * **Task priority** — ``set_priority()``, ``get_priority()`` for
      IDLE / BELOW_NORMAL / NORMAL / ABOVE_NORMAL / HIGH / REALTIME.
    * **Presets** — ``preset_balanced()``, ``preset_max_performance()``,
      ``preset_power_saver()``.
    """

    # --- static helper: dynamic thread calculation ---------------------------
    @staticmethod
    def compute_dynamic_workers() -> Tuple[int, int]:
        """Return ``(pull_workers, push_workers)`` tuned to the host.

        Heuristic
        ---------
        * ADB operations are I/O-bound (>95 % time is USB/subprocess wait).
        * Pull: ``min(cpu_cores × 2, 16)``
        * Push: ``min(cpu_cores × 2, 12)``
        * Low-RAM (< 4 GB) clamp applied.
        """
        cores = os.cpu_count() or 4
        try:
            import psutil  # type: ignore[import-untyped]
            ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        except Exception:
            ram_gb = 8.0

        pull = min(cores * 2, 16)
        push = min(cores * 2, 12)
        if ram_gb < 4:
            pull = min(pull, 6)
            push = min(push, 4)
        pull = max(2, pull)
        push = max(2, push)
        return pull, push

    @staticmethod
    def io_pool_size() -> int:
        """Optimal size for the shared I/O thread pool (delegates to Engine)."""
        e = Engine()
        return e.io_workers

    # --- constructor ---------------------------------------------------------
    def __init__(
        self,
        max_pull_workers: int = 4,
        max_push_workers: int = 4,
        verify_checksums: bool = True,
        checksum_algo: str = "md5",
        gpu_enabled: bool = True,
        multi_gpu: bool = True,
        npu_enabled: bool = True,
        virt_enabled: bool = True,
        auto_threads: bool = True,
    ):
        # Core engine (handles GPU, NPU, CPU, virt, pools)
        self._engine = Engine()

        # Dynamic thread calculation overrides explicit values
        if auto_threads:
            max_pull_workers, max_push_workers = self.compute_dynamic_workers()

        self.max_pull_workers = max_pull_workers
        self.max_push_workers = max_push_workers
        self.auto_threads = auto_threads
        self.verify_checksums = verify_checksums
        self.checksum_algo = checksum_algo

        # Propagate toggles to engine
        self._engine.set_gpu_enabled(gpu_enabled)
        self._engine.set_multi_gpu(multi_gpu)
        self._engine.set_npu_enabled(npu_enabled)
        self.virt_enabled = virt_enabled

        log.info(
            "TransferAccelerator: auto=%s  workers=%d/%d  gpu=%s  multi_gpu=%s  npu=%s  virt=%s",
            auto_threads, self.max_pull_workers, self.max_push_workers,
            gpu_enabled, multi_gpu, npu_enabled, virt_enabled,
        )

    # --- engine access -------------------------------------------------------
    @property
    def engine(self) -> Engine:
        """Direct access to the underlying pyaccelerate Engine."""
        return self._engine

    # --- runtime toggles (delegate to engine) --------------------------------
    def set_gpu_enabled(self, on: bool):
        self._engine.set_gpu_enabled(on)

    def set_multi_gpu(self, on: bool):
        self._engine.set_multi_gpu(on)

    def set_npu_enabled(self, on: bool):
        self._engine.set_npu_enabled(on)

    def set_virt_enabled(self, on: bool):
        self.virt_enabled = on

    # --- GPU data (delegated) ------------------------------------------------
    @property
    def gpus(self) -> List[GPUDevice]:
        return self._engine.gpus

    @property
    def usable_gpus(self) -> List[GPUDevice]:
        return self._engine.usable_gpus

    @property
    def best_gpu(self) -> Optional[GPUDevice]:
        return self._engine.best_gpu

    @property
    def gpu_info(self) -> Dict[str, str]:
        return get_gpu_info()

    # --- NPU data (NEW — delegated) ------------------------------------------
    @property
    def npus(self) -> List[NPUDevice]:
        return self._engine.npus

    @property
    def usable_npus(self) -> List[NPUDevice]:
        return self._engine.usable_npus

    @property
    def best_npu(self) -> Optional[NPUDevice]:
        return self._engine.best_npu

    @property
    def npu_info(self) -> Dict[str, str]:
        return get_npu_info()

    # --- Virtualization (delegated) ------------------------------------------
    @property
    def virt(self) -> VirtInfo:
        return self._engine.virt

    # --- Priority / Energy (NEW) ---------------------------------------------
    def set_priority(self, priority: TaskPriority) -> bool:
        """Set OS scheduling priority for the current process."""
        return self._engine.set_priority(priority)

    def get_priority(self) -> TaskPriority:
        return self._engine.get_priority()

    def set_energy(self, profile: EnergyProfile) -> bool:
        """Set system energy/performance profile."""
        return self._engine.set_energy(profile)

    def get_energy(self) -> EnergyProfile:
        return self._engine.get_energy()

    def priority_info(self) -> Dict[str, str]:
        """Current priority and energy information."""
        return self._engine.priority_info()

    # --- Presets (NEW) -------------------------------------------------------
    @staticmethod
    def preset_max_performance() -> Dict[str, bool]:
        """Apply max-performance preset (HIGH priority + ULTRA_PERFORMANCE)."""
        return apply_max_performance()

    @staticmethod
    def preset_balanced() -> Dict[str, bool]:
        """Apply balanced preset (NORMAL priority + BALANCED energy)."""
        return apply_balanced()

    @staticmethod
    def preset_power_saver() -> Dict[str, bool]:
        """Apply power-saver preset (BELOW_NORMAL + POWER_SAVER)."""
        return apply_power_saver()

    # --- Max Mode context manager (NEW) --------------------------------------
    def max_mode(self, *, set_priority: bool = True, set_energy: bool = True):
        """Return a MaxMode context manager for peak throughput.

        Usage::

            with accel.max_mode() as m:
                ...  # OS priority = HIGH, energy = ULTRA_PERFORMANCE
        """
        return self._engine.max_mode(
            set_priority=set_priority,
            set_energy=set_energy,
        )

    # --- Workers (ADB-specific heuristic) ------------------------------------
    def optimal_workers(self, file_count: int, avg_size_bytes: int = 0) -> int:
        """Return the ideal worker count for a batch of *file_count* files."""
        if file_count <= 1:
            return 1
        cores = os.cpu_count() or 4
        if avg_size_bytes > 50 * 1024 * 1024:
            cap = min(3, cores)
        elif avg_size_bytes > 10 * 1024 * 1024:
            cap = min(4, cores)
        else:
            cap = min(self.max_pull_workers, cores * 2, 16)
        return min(cap, file_count)

    # --- Summary / status (delegated with ADB-specific extras) ---------------
    def summary(self) -> str:
        """Human-readable multi-line summary (pyaccelerate Engine report
        plus ADB-specific worker/verification info).
        """
        lines: List[str] = [self._engine.summary()]

        # ADB-specific addendum
        cores = os.cpu_count() or "?"
        mode_label = "dynamic" if self.auto_threads else "manual"
        lines.append(
            f"  Threads pull/push: {self.max_pull_workers}/{self.max_push_workers}"
            f"  ({mode_label}, {cores} cores)"
        )
        lines.append(
            f"  Verification: {'ON' if self.verify_checksums else 'OFF'}"
            f" ({self.checksum_algo.upper()})"
        )
        return "\n".join(lines)

    def status_line(self) -> str:
        """One-line summary for the status bar (delegates to engine)."""
        return self._engine.status_line()

    def as_dict(self) -> Dict:
        """Machine-readable snapshot (engine + ADB extras)."""
        d = self._engine.as_dict()
        d["adb"] = {
            "max_pull_workers": self.max_pull_workers,
            "max_push_workers": self.max_push_workers,
            "auto_threads": self.auto_threads,
            "verify_checksums": self.verify_checksums,
            "checksum_algo": self.checksum_algo,
        }
        return d
