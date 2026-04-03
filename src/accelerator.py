"""
accelerator.py — Thin wrapper around **pyaccelerate** for the ADB Toolkit.

All heavy lifting (GPU/NPU enumeration, thread-pool management, priority
control, energy profiles, auto-tuning, memory management) is delegated to
the ``pyaccelerate`` package (>= 0.10.0).
This module re-exports types and provides the ADB-specific helpers
(``parallel_checksum``, ``verify_transfer``, ``TransferAccelerator``).

Public API
----------
  - ``TransferAccelerator``      — orchestrator used by TransferManager
  - ``detect_all_gpus()``        — full GPU enumeration
  - ``detect_all_npus()``        — full NPU enumeration
  - ``detect_virtualization()``  — returns VirtInfo
  - ``parallel_checksum()``      — batch-hash files (work-stealing scheduler)
  - ``verify_transfer()``        — compare checksums after clone
  - ``gpu_available()``          — quick boolean
  - ``npu_available()``          — quick boolean
"""

from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── pyaccelerate imports ────────────────────────────────────────────────
from pyaccelerate import (
    AdaptiveConfig,
    AdaptiveScheduler,
    Engine,
    EnergyProfile,
    MaxMode,
    TaskPriority,
    WorkStealingScheduler,
    ws_map,
    ws_submit,
    auto_tune as _auto_tune,
    get_or_tune as _get_or_tune,
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
from pyaccelerate.cpu import detect as _detect_cpu, recommend_workers as _recommend_workers
from pyaccelerate.memory import (
    BufferPool,
    Pressure,
    clamp_workers,
    get_pressure,
    get_stats as get_mem_stats,
)
from pyaccelerate.threads import get_pool, run_parallel as _threads_run_parallel
from pyaccelerate.work_stealing import get_scheduler as _get_ws_scheduler
from pyaccelerate.profiler import timed
from pyaccelerate.pipeline import Pipeline, Stage, PipelineResult  # v0.10.0
from pyaccelerate.retry import RetryPolicy, retry_call  # v0.10.0
from pyaccelerate.circuit_breaker import CircuitBreaker, CircuitOpenError  # v0.10.0
from pyaccelerate.rate_limiter import RateLimiter  # v0.10.0
from pyaccelerate.health import health_check as _health_check, HealthReport  # v0.10.0
from pyaccelerate.gpu import gpu_hash_file, gpu_hash_batch, gpu_hash_available  # v0.10.0
from pyaccelerate import (
    balanced as apply_balanced,
    max_performance as apply_max_performance,
    power_saver as apply_power_saver,
)

log = logging.getLogger("adb_toolkit.accelerator")

# Shared buffer pool for file hashing (1 MB buffers, reused across calls)
_hash_buffer_pool = BufferPool(buffer_size=1 << 20, max_buffers=16)


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
    """Hash a single file on CPU using pooled buffers for efficiency."""
    h = hashlib.new(algo)
    buf = _hash_buffer_pool.acquire()
    try:
        with open(path, "rb") as f:
            while True:
                n = f.readinto(buf)
                if not n:
                    break
                h.update(buf[:n])
    except Exception as exc:
        log.warning("Cannot hash %s: %s", path, exc)
        return ""
    finally:
        _hash_buffer_pool.release(buf)
    return h.hexdigest()


@timed(label="parallel_checksum", level=logging.INFO)
def parallel_checksum(
    file_paths: List[str],
    algo: str = "md5",
    max_workers: int = 8,
    use_gpu: bool = True,
    multi_gpu: bool = False,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, str]:
    """Compute checksums for many files in parallel.

    Uses the pyaccelerate **work-stealing scheduler** (v0.7.0) for
    CPU-bound hashing — better load balancing and cache locality
    than a plain thread pool.

    Parameters
    ----------
    use_gpu : bool
        Reserved for future GPU-accelerated hashing (currently CPU-only).
    multi_gpu : bool
        Reserved for future multi-GPU hashing.
    """
    total = len(file_paths)
    if not total:
        return {}

    # Work-stealing scheduler: optimal for CPU-bound hashing
    try:
        hashes = ws_map(
            _hash_file_cpu,
            [(fp, algo) for fp in file_paths],
        )
        results = dict(zip(file_paths, hashes))
    except Exception:
        # Fallback to I/O pool if work-stealing unavailable
        results = {}
        pool = get_pool()
        futures = {pool.submit(_hash_file_cpu, fp, algo): fp for fp in file_paths}
        for fut in as_completed(futures):
            fp = futures[fut]
            try:
                results[fp] = fut.result()
            except Exception:
                results[fp] = ""

    if progress_cb:
        for i, fp in enumerate(file_paths, 1):
            try:
                progress_cb(i, total, fp)
            except Exception:
                pass

    log.info("Checksummed %d files via work-stealing scheduler", total)
    return results


# ═══════════════════════════════════════════════════════════════════════════
#  Transfer verification  (ADB-specific)
# ═══════════════════════════════════════════════════════════════════════════
@timed(label="verify_transfer", level=logging.INFO)
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
    * **Work-stealing scheduler** (v0.7.0) — ``ws_submit()``, ``ws_map()``,
      ``work_stealing_scheduler`` for CPU-bound batch tasks with optimal
      load balancing via Chase-Lev deques.
    * **Adaptive scheduler** (v0.7.0) — ``adaptive_scheduler()`` auto-tunes
      worker count based on latency, CPU & memory pressure.
    * **Pipeline** (v0.10.0) — ``run_pipeline()`` for multi-stage processing
      with backpressure and per-stage statistics.
    * **Retry / Circuit Breaker** (v0.10.0) — ``submit_with_retry()``,
      ``circuit_breaker`` for robust transfer operations.
    * **Rate Limiter** (v0.10.0) — ``rate_limiter`` for throttling I/O.
    * **GPU Hashing** (v0.10.0) — ``gpu_hash_file()``, ``gpu_hash_batch()``
      for GPU-accelerated checksums.
    * **Health Check** (v0.10.0) — ``health_check()`` for aggregated system
      health (CPU, memory, disk, GPU).
    """

    # --- static helper: dynamic thread calculation ---------------------------
    @staticmethod
    def compute_dynamic_workers() -> Tuple[int, int]:
        """Return ``(pull_workers, push_workers)`` tuned to the host.

        Uses pyaccelerate's ``recommend_workers`` and ``clamp_workers``
        for hardware-aware, memory-pressure-aware sizing.

        Heuristic
        ---------
        * ADB operations are I/O-bound (>95 % time is USB/subprocess wait).
        * Pull: ``min(recommended_io, 16)``
        * Push: ``min(recommended_io, 12)``
        * Memory-pressure clamp applied automatically.
        """
        io_rec = _recommend_workers(io_bound=True)
        pull = clamp_workers(min(io_rec, 16), floor=2)
        push = clamp_workers(min(io_rec, 12), floor=2)
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
        """Return the ideal worker count for a batch of *file_count* files.

        Automatically clamped by memory pressure via pyaccelerate.
        """
        if file_count <= 1:
            return 1
        cores = os.cpu_count() or 4
        if avg_size_bytes > 50 * 1024 * 1024:
            cap = min(3, cores)
        elif avg_size_bytes > 10 * 1024 * 1024:
            cap = min(4, cores)
        else:
            cap = min(self.max_pull_workers, cores * 2, 16)
        desired = min(cap, file_count)
        return clamp_workers(desired, floor=1)

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
        pressure = get_pressure()
        lines.append(f"  Memory pressure: {pressure.name}")
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

    # --- Work-Stealing Scheduler (v0.7.0) ------------------------------------
    @property
    def work_stealing_scheduler(self) -> WorkStealingScheduler:
        """Return the work-stealing scheduler sized for this hardware.

        Lazily created and reused.  Ideal for CPU-bound batch tasks
        (hashing, compression, dedup comparison) where load balancing
        across cores matters more than I/O concurrency.
        """
        return self._engine.get_work_stealing_scheduler()

    def ws_submit(self, fn, *args, **kwargs):
        """Submit a single task to the work-stealing scheduler."""
        return self._engine.ws_submit(fn, *args, **kwargs)

    def ws_map(self, fn, items, timeout=None):
        """Map *fn* over *items* using work-stealing (returns ordered results)."""
        return self._engine.ws_map(fn, items, timeout=timeout)

    # --- Adaptive Scheduler (v0.7.0) -----------------------------------------
    def adaptive_scheduler(
        self,
        config: Optional[AdaptiveConfig] = None,
    ) -> AdaptiveScheduler:
        """Return an :class:`AdaptiveScheduler` that dynamically rescales
        workers based on latency, CPU & memory pressure.

        Usage::

            with accel.adaptive_scheduler() as sched:
                results = sched.map(heavy_fn, [(item,) for item in data])
                snap = sched.snapshot()
        """
        return self._engine.adaptive_scheduler(config=config)

    # --- Memory pressure (v0.7.0) --------------------------------------------
    @property
    def memory_pressure(self) -> Pressure:
        """Current system memory pressure level."""
        return self._engine.memory_pressure

    # --- CPU info (v0.7.0) ---------------------------------------------------
    @property
    def cpu_info(self) -> Any:
        """CPU detection snapshot (arch, cores, flags, ARM clusters, etc.)."""
        return self._engine.cpu

    # --- Auto-tune (v0.7.0) --------------------------------------------------
    def auto_tune(self, *, quick: bool = True, apply: bool = True) -> Dict[str, Any]:
        """Run an auto-tuning cycle and optionally apply results.

        Benchmarks the current hardware, saves a :class:`TuneProfile`,
        and adjusts pools, priority, and energy profile when *apply* is True.
        Returns the profile as a dict.
        """
        return self._engine.auto_tune(quick=quick, apply=apply)

    @staticmethod
    def get_or_tune() -> Any:
        """Load an existing tune profile or run a new tuning cycle if stale."""
        return _get_or_tune()

    # --- Shutdown (v0.7.0) ---------------------------------------------------
    def shutdown(self, wait_for: bool = True) -> None:
        """Shut down all shared pools and schedulers. Call during app exit."""
        self._engine.shutdown(wait_for=wait_for)

    # --- Pipeline (v0.10.0) --------------------------------------------------
    def run_pipeline(
        self,
        stages: Any,
        items: Any,
    ) -> PipelineResult:
        """Run a multi-stage pipeline with backpressure.

        Parameters
        ----------
        stages
            A :class:`Pipeline` instance or a list of :class:`Stage` objects.
        items
            Iterable of input items for the first stage.

        Returns
        -------
        PipelineResult
        """
        return self._engine.run_pipeline(stages, items)

    # --- Retry (v0.10.0) -----------------------------------------------------
    def submit_with_retry(
        self,
        fn: Callable,
        *args: Any,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        **kwargs: Any,
    ) -> Any:
        """Execute *fn* with automatic retry on failure."""
        return self._engine.submit_with_retry(
            fn, *args, max_attempts=max_attempts,
            backoff_base=backoff_base, **kwargs,
        )

    # --- Circuit Breaker (v0.10.0) -------------------------------------------
    def circuit_breaker(
        self,
        name: str = "adb-transfer",
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ) -> CircuitBreaker:
        """Create a :class:`CircuitBreaker` for fault-tolerant operations.

        Usage::

            cb = accel.circuit_breaker()
            with cb:
                do_flaky_transfer()
        """
        return CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            name=name,
        )

    # --- Rate Limiter (v0.10.0) ----------------------------------------------
    def rate_limiter(
        self,
        rate: float = 10.0,
        burst: int = 0,
    ) -> RateLimiter:
        """Create a :class:`RateLimiter` for throttling I/O operations.

        Parameters
        ----------
        rate
            Max operations per second.
        burst
            Token bucket capacity (0 = same as rate).
        """
        return RateLimiter(rate=rate, burst=burst or int(rate))

    # --- GPU Hashing (v0.10.0) -----------------------------------------------
    def gpu_hash_files(
        self,
        paths: List[str],
        algo: str = "sha256",
    ) -> Dict[str, str]:
        """Hash files using GPU when available, CPU fallback otherwise.

        Uses pyaccelerate's gpu_hash_batch for batch acceleration.
        """
        if not paths:
            return {}
        return gpu_hash_batch(paths, algo=algo)

    # --- Health Check (v0.10.0) ----------------------------------------------
    def health_check(self, include_gpu: bool = True) -> HealthReport:
        """Run aggregated system health checks.

        Returns a :class:`HealthReport` with per-component status
        (CPU, memory, disk, GPU).
        """
        return _health_check(include_gpu=include_gpu)
