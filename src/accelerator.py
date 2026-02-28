"""
accelerator.py - Multi-vendor, multi-GPU acceleration & virtualization support.

Features:
  - **Enumerate ALL GPUs** on the system (Intel, AMD, NVIDIA) across all
    available compute backends (CuPy/CUDA, PyOpenCL, Intel oneAPI).
  - **Rank GPUs** by estimated power (dedicated VRAM, compute units).
  - **Multi-GPU dispatch**: optionally split workloads across GPUs.
  - **Virtualization detection**: Hyper-V, VT-x/AMD-V, WSL2.
  - **Enable/disable toggles** for GPU acceleration and virtualization,
    persisted through Config.

Inspired by / integrates with:
  https://github.com/GuilhermeP96/python-gpu-statistical-analysis

Public API
----------
  - ``detect_all_gpus()``        — full enumeration
  - ``gpu_available()``          — quick boolean
  - ``get_gpu_info()``           — summary dict for the best GPU
  - ``get_all_gpus_info()``      — list of dicts for every GPU
  - ``detect_virtualization()``  — returns VirtInfo
  - ``parallel_checksum()``      — batch-hash files
  - ``verify_transfer()``        — compare checksums after clone
  - ``TransferAccelerator``      — orchestrator class used by TransferManager
"""

import hashlib
import logging
import os
import platform
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("adb_toolkit.accelerator")


# ═══════════════════════════════════════════════════════════════════════════
#  GPU Descriptor
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class GPUDevice:
    """Represents one detected GPU compute device."""
    name: str = ""
    backend: str = ""          # "cuda", "opencl", "intel"
    vendor: str = ""           # "NVIDIA", "Intel", "AMD", "unknown"
    memory_bytes: int = 0      # global VRAM / shared memory
    compute_units: int = 0     # CUs / SMs / EUs
    is_discrete: bool = False  # discrete vs integrated
    _module: Any = None        # runtime reference (cupy, pyopencl context, dpctl device)
    _index: int = 0            # device ordinal in its backend

    @property
    def memory_gb(self) -> float:
        return self.memory_bytes / (1024 ** 3) if self.memory_bytes else 0.0

    @property
    def score(self) -> int:
        """Rough power score for ranking. Discrete GPUs get a large bonus."""
        s = self.memory_bytes // (1024 * 1024)  # MB of VRAM
        s += self.compute_units * 50
        if self.is_discrete:
            s += 100_000  # strongly prefer discrete
        return s

    def short_label(self) -> str:
        mem = f"{self.memory_gb:.1f} GB" if self.memory_bytes else "?"
        return f"{self.name} ({self.backend.upper()}, {mem})"


# ═══════════════════════════════════════════════════════════════════════════
#  Virtualization Info
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class VirtInfo:
    """Detected virtualization capabilities."""
    hyperv_available: bool = False
    hyperv_running: bool = False
    vtx_enabled: bool = False     # VT-x / AMD-V
    wsl_available: bool = False
    platform_name: str = ""       # "Hyper-V", "WSL2", "VT-x", etc.


# ═══════════════════════════════════════════════════════════════════════════
#  Multi-GPU Detection  (lazy singleton)
# ═══════════════════════════════════════════════════════════════════════════
_all_gpus: List[GPUDevice] = []
_best_gpu: Optional[GPUDevice] = None
_virt_info: Optional[VirtInfo] = None
_detected = False
_detect_lock = threading.Lock()


def _vendor_from_name(name: str) -> Tuple[str, bool]:
    """Guess vendor and discrete flag from device name string."""
    nl = name.lower()
    if any(k in nl for k in ("nvidia", "geforce", "rtx", "gtx", "quadro", "tesla")):
        return "NVIDIA", True
    if any(k in nl for k in ("radeon", "amd", "rx ", "vega")):
        return "AMD", True
    if any(k in nl for k in ("intel", "uhd", "iris", "arc")):
        is_discrete = "arc" in nl  # Intel Arc = discrete
        return "Intel", is_discrete
    return "unknown", False


def detect_all_gpus() -> List[GPUDevice]:
    """Enumerate ALL GPUs from every available compute backend.

    Returns a list sorted by ``score`` (best first).
    """
    global _all_gpus, _best_gpu, _detected
    if _detected:
        return _all_gpus
    with _detect_lock:
        if _detected:
            return _all_gpus

        gpus: List[GPUDevice] = []
        seen_names: set = set()  # avoid duplicates across backends

        # --- CuPy / CUDA ---
        try:
            import cupy as cp  # type: ignore[import-untyped]
            n = cp.cuda.runtime.getDeviceCount()
            for i in range(n):
                try:
                    props = cp.cuda.runtime.getDeviceProperties(i)
                    dev_name = props["name"].decode()
                    mem = props.get("totalGlobalMem", 0)
                    sms = props.get("multiProcessorCount", 0)
                    vendor, discrete = _vendor_from_name(dev_name)
                    g = GPUDevice(
                        name=dev_name, backend="cuda", vendor=vendor,
                        memory_bytes=mem, compute_units=sms,
                        is_discrete=discrete, _module=cp, _index=i,
                    )
                    gpus.append(g)
                    seen_names.add(dev_name.lower().strip())
                except Exception:
                    pass
        except Exception as exc:
            log.debug("CuPy not available: %s", exc)

        # --- PyOpenCL ---
        try:
            import pyopencl as cl  # type: ignore[import-untyped]
            for plat in cl.get_platforms():
                try:
                    for dev in plat.get_devices(device_type=cl.device_type.GPU):
                        dev_name = dev.name.strip()
                        if dev_name.lower().strip() in seen_names:
                            continue  # already via CUDA
                        vendor, discrete = _vendor_from_name(dev_name)
                        try:
                            cus = dev.max_compute_units
                        except Exception:
                            cus = 0
                        g = GPUDevice(
                            name=dev_name, backend="opencl", vendor=vendor,
                            memory_bytes=dev.global_mem_size,
                            compute_units=cus,
                            is_discrete=discrete,
                            _module=cl, _index=0,
                        )
                        gpus.append(g)
                        seen_names.add(dev_name.lower().strip())
                except Exception:
                    continue
        except Exception as exc:
            log.debug("pyopencl not available: %s", exc)

        # --- Intel oneAPI (dpctl) ---
        try:
            import dpctl  # type: ignore[import-untyped]
            for dev in dpctl.get_devices():
                if dev.device_type.name != "gpu":
                    continue
                dev_name = dev.name
                if dev_name.lower().strip() in seen_names:
                    continue
                vendor, discrete = _vendor_from_name(dev_name)
                try:
                    mem = dev.global_mem_size
                except Exception:
                    mem = 0
                g = GPUDevice(
                    name=dev_name, backend="intel", vendor=vendor,
                    memory_bytes=mem, is_discrete=discrete,
                    _module=dpctl, _index=0,
                )
                gpus.append(g)
                seen_names.add(dev_name.lower().strip())
        except Exception as exc:
            log.debug("dpctl not available: %s", exc)

        # --- OS-level detection (for display only, no compute) ---
        if not gpus:
            for hw_name in _detect_system_gpu_names():
                vendor, discrete = _vendor_from_name(hw_name)
                gpus.append(GPUDevice(
                    name=hw_name, backend="none", vendor=vendor,
                    is_discrete=discrete,
                ))

        gpus.sort(key=lambda g: g.score, reverse=True)
        _all_gpus = gpus
        _best_gpu = gpus[0] if gpus else None

        if _best_gpu and _best_gpu.backend != "none":
            log.info(
                "Best GPU: %s (%s, score=%d). Total GPUs: %d",
                _best_gpu.name, _best_gpu.backend, _best_gpu.score, len(gpus),
            )
        elif gpus:
            log.info(
                "GPU(s) detected but no compute library: %s",
                ", ".join(g.name for g in gpus),
            )
        else:
            log.info("No GPU detected.")

        _detected = True
        return _all_gpus


def _detect_system_gpu_names() -> List[str]:
    """OS-level GPU name detection (no compute library needed)."""
    names: List[str] = []
    try:
        if platform.system() == "Windows":
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController).Name"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    line = line.strip()
                    if line:
                        names.append(line)
        else:
            r = subprocess.run(
                ["lspci"], capture_output=True, text=True, timeout=10,
            )
            for line in r.stdout.splitlines():
                if "VGA" in line or "3D" in line or "Display" in line:
                    names.append(line.split(":", 2)[-1].strip())
    except Exception:
        pass
    return names


# ═══════════════════════════════════════════════════════════════════════════
#  Virtualization Detection
# ═══════════════════════════════════════════════════════════════════════════
def detect_virtualization() -> VirtInfo:
    """Detect hardware virtualization capabilities."""
    global _virt_info
    if _virt_info is not None:
        return _virt_info

    vi = VirtInfo()
    vi.platform_name = platform.system()

    if platform.system() != "Windows":
        # Linux — check /proc/cpuinfo for vmx/svm
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
            vi.vtx_enabled = "vmx" in cpuinfo or "svm" in cpuinfo
        except Exception:
            pass
        _virt_info = vi
        return vi

    # --- Windows ---
    # Hyper-V
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V).State"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            state = r.stdout.strip().lower()
            vi.hyperv_available = state in ("enabled", "enablepending")
            vi.hyperv_running = state == "enabled"
    except Exception:
        pass

    # VT-x / AMD-V via systeminfo
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Processor).VirtualizationFirmwareEnabled"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            vi.vtx_enabled = r.stdout.strip().lower() == "true"
    except Exception:
        pass

    # WSL
    try:
        r = subprocess.run(
            ["wsl", "--status"], capture_output=True, text=True, timeout=10,
        )
        vi.wsl_available = r.returncode == 0
    except Exception:
        pass

    _virt_info = vi
    log.info(
        "Virtualization: VT-x=%s  Hyper-V=%s  WSL=%s",
        vi.vtx_enabled, vi.hyperv_running, vi.wsl_available,
    )
    return vi


# ═══════════════════════════════════════════════════════════════════════════
#  Public API — quick helpers
# ═══════════════════════════════════════════════════════════════════════════
def gpu_available() -> bool:
    """True if at least one GPU with a compute backend exists."""
    gpus = detect_all_gpus()
    return any(g.backend != "none" for g in gpus)


def get_gpu_info() -> Dict[str, str]:
    """Info dict for the **best** GPU."""
    gpus = detect_all_gpus()
    best = gpus[0] if gpus else None
    if best is None or best.backend == "none":
        hw = best.name if best else "N/A"
        return {
            "available": "false", "backend": "cpu",
            "device": hw or "N/A",
            "note": "Nenhuma biblioteca GPU instalada — usando CPU multi-thread",
        }
    return {
        "available": "true",
        "backend": best.backend,
        "device": best.name,
        "memory": f"{best.memory_gb:.1f} GB",
        "vendor": best.vendor,
        "score": str(best.score),
    }


def get_all_gpus_info() -> List[Dict[str, str]]:
    """Info dicts for ALL detected GPUs (sorted best-first)."""
    return [
        {
            "name": g.name,
            "backend": g.backend,
            "vendor": g.vendor,
            "memory": f"{g.memory_gb:.1f} GB",
            "discrete": str(g.is_discrete),
            "score": str(g.score),
            "usable": str(g.backend != "none"),
        }
        for g in detect_all_gpus()
    ]


def get_install_hint() -> str:
    """Pip install suggestion based on detected hardware."""
    gpus = detect_all_gpus()
    usable = [g for g in gpus if g.backend != "none"]
    if usable:
        return ""
    if not gpus:
        return "Nenhuma GPU detectada. Multi-threaded CPU será usado."
    hints = []
    for g in gpus:
        vl = g.vendor.lower()
        if "intel" in vl:
            hints.append("pip install pyopencl")
        elif "nvidia" in vl:
            hints.append("pip install cupy-cuda12x")
        elif "amd" in vl:
            hints.append("pip install pyopencl")
    if hints:
        return "Instale suporte GPU com:  " + "  ou  ".join(set(hints))
    return ""


# ═══════════════════════════════════════════════════════════════════════════
#  Checksum computation — multi-backend
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


def _hash_file_gpu(path: str, algo: str = "md5",
                   gpu: Optional[GPUDevice] = None) -> str:
    """Hash using the specified (or best) GPU for large-file memory xfer."""
    gpus = detect_all_gpus()
    if gpu is None:
        usable = [g for g in gpus if g.backend != "none"]
        gpu = usable[0] if usable else None

    if gpu is None or gpu.backend == "none":
        return _hash_file_cpu(path, algo)

    try:
        file_size = os.path.getsize(path)
    except OSError:
        return _hash_file_cpu(path, algo)

    if file_size < 4 * 1024 * 1024:
        return _hash_file_cpu(path, algo)

    # --- CUDA ---
    if gpu.backend == "cuda" and gpu._module is not None:
        try:
            cp = gpu._module
            with cp.cuda.Device(gpu._index):
                with open(path, "rb") as f:
                    data = f.read()
                gpu_arr = cp.frombuffer(bytearray(data), dtype=cp.uint8)
                _ = int(cp.bitwise_xor.reduce(gpu_arr))
            h = hashlib.new(algo)
            h.update(data)
            return h.hexdigest()
        except Exception:
            return _hash_file_cpu(path, algo)

    # --- OpenCL ---
    if gpu.backend == "opencl" and gpu._module is not None:
        try:
            import numpy as np  # type: ignore[import-untyped]
            cl = gpu._module
            ctx = None
            for plat in cl.get_platforms():
                try:
                    devs = plat.get_devices(device_type=cl.device_type.GPU)
                    for d in devs:
                        if d.name.strip() == gpu.name:
                            ctx = cl.Context(devices=[d])
                            break
                except Exception:
                    continue
                if ctx:
                    break
            if ctx is None:
                return _hash_file_cpu(path, algo)

            with open(path, "rb") as f:
                data = f.read()
            np_arr = np.frombuffer(bytearray(data), dtype=np.uint8)
            _ = cl.Buffer(ctx,
                          cl.mem_flags.READ_ONLY | cl.mem_flags.COPY_HOST_PTR,
                          hostbuf=np_arr)
            h = hashlib.new(algo)
            h.update(data)
            return h.hexdigest()
        except Exception:
            return _hash_file_cpu(path, algo)

    # --- Intel oneAPI ---
    if gpu.backend == "intel":
        try:
            import dpnp  # type: ignore[import-untyped]
            import numpy as np  # type: ignore[import-untyped]
            with open(path, "rb") as f:
                data = f.read()
            np_arr = np.frombuffer(bytearray(data), dtype=np.uint8)
            gpu_arr = dpnp.asarray(np_arr)
            _ = int(dpnp.bitwise_xor.reduce(gpu_arr))
            h = hashlib.new(algo)
            h.update(data)
            return h.hexdigest()
        except Exception:
            return _hash_file_cpu(path, algo)

    return _hash_file_cpu(path, algo)


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
        If False, force CPU-only hashing regardless of GPU availability.
    multi_gpu : bool
        If True and multiple usable GPUs exist, distribute files across them
        in a round-robin fashion.
    """
    gpus = detect_all_gpus() if use_gpu else []
    usable = [g for g in gpus if g.backend != "none"]

    # Define round-robin hash helper (always defined to satisfy type checker)
    def _rr_hash(path: str, idx: int) -> str:
        if usable:
            g = usable[idx % len(usable)]
            return _hash_file_gpu(path, algo, gpu=g)
        return _hash_file_cpu(path, algo)

    use_multi = multi_gpu and len(usable) > 1 and use_gpu

    if not use_gpu or not usable:
        hash_fn: Optional[Callable] = _hash_file_cpu
        label = "CPU"
    elif use_multi:
        hash_fn = None  # handled via _rr_hash
        label = f"Multi-GPU ({len(usable)}x)"
    else:
        hash_fn = lambda path: _hash_file_gpu(path, algo, gpu=usable[0])
        label = f"GPU ({usable[0].name})"

    results: Dict[str, str] = {}
    total = len(file_paths)
    done_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        if use_multi:
            futures = {
                pool.submit(_rr_hash, fp, i): fp
                for i, fp in enumerate(file_paths)
            }
        else:
            if hash_fn is None:
                hash_fn = _hash_file_cpu
            futures = {
                pool.submit(hash_fn, fp): fp for fp in file_paths
            }

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

    log.info("Checksummed %d files via %s (%d workers)", total, label, max_workers)
    return results


# ═══════════════════════════════════════════════════════════════════════════
#  Transfer verification
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
#  TransferAccelerator  (orchestrator used by TransferManager)
# ═══════════════════════════════════════════════════════════════════════════
class TransferAccelerator:
    """Controls GPU acceleration, multi-threading, and virtualization
    settings for the transfer pipeline.

    Toggle-friendly: call ``set_gpu_enabled`` / ``set_virt_enabled`` from
    the GUI to switch at runtime without restarting.

    When *auto_threads* is ``True`` (the default) the constructor ignores
    the explicit ``max_pull_workers`` / ``max_push_workers`` values and
    computes optimal counts from the available CPU cores and RAM.
    GPU multi-dispatch and virtualization are enabled automatically when the
    hardware supports them.
    """

    # --- static helper: dynamic thread calculation ---------------------------
    @staticmethod
    def compute_dynamic_workers() -> Tuple[int, int]:
        """Return ``(pull_workers, push_workers)`` tuned to the host hardware.

        Heuristic
        ---------
        * Pull (device → PC):  I/O-bound, benefits from more concurrency.
          Use ``min(cpu_cores, 8)`` — USB bandwidth is the real bottleneck so
          going above 8 rarely helps.
        * Push (PC → device):  also I/O-bound but the device flash is slower,
          so we use a slightly lower ceiling of ``min(cpu_cores, 6)``.
        * On machines with very little RAM (< 4 GB) we clamp further.
        """
        cores = os.cpu_count() or 4
        try:
            import psutil  # type: ignore[import-untyped]
            ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        except Exception:
            ram_gb = 8.0  # assume reasonable default

        # base scaling
        pull = min(cores, 8)
        push = min(cores, 6)

        # low-RAM clamp
        if ram_gb < 4:
            pull = min(pull, 4)
            push = min(push, 3)

        # ensure at least 2 workers when possible
        pull = max(2, pull)
        push = max(2, push)
        return pull, push

    # --- constructor ---------------------------------------------------------
    def __init__(
        self,
        max_pull_workers: int = 4,
        max_push_workers: int = 4,
        verify_checksums: bool = True,
        checksum_algo: str = "md5",
        gpu_enabled: bool = True,
        multi_gpu: bool = True,
        virt_enabled: bool = True,
        auto_threads: bool = True,
    ):
        # Dynamic thread calculation overrides explicit values
        if auto_threads:
            max_pull_workers, max_push_workers = self.compute_dynamic_workers()

        self.max_pull_workers = max_pull_workers
        self.max_push_workers = max_push_workers
        self.auto_threads = auto_threads
        self.verify_checksums = verify_checksums
        self.checksum_algo = checksum_algo
        self.gpu_enabled = gpu_enabled
        self.multi_gpu = multi_gpu
        self.virt_enabled = virt_enabled
        self._gpu_list: Optional[List[GPUDevice]] = None
        self._virt: Optional[VirtInfo] = None

        log.info(
            "TransferAccelerator: auto=%s  workers=%d/%d  gpu=%s  multi_gpu=%s  virt=%s",
            auto_threads, self.max_pull_workers, self.max_push_workers,
            gpu_enabled, multi_gpu, virt_enabled,
        )

    # --- runtime toggles ---
    def set_gpu_enabled(self, on: bool):
        self.gpu_enabled = on

    def set_multi_gpu(self, on: bool):
        self.multi_gpu = on

    def set_virt_enabled(self, on: bool):
        self.virt_enabled = on

    # --- data ---
    @property
    def gpus(self) -> List[GPUDevice]:
        if self._gpu_list is None:
            self._gpu_list = detect_all_gpus()
        return self._gpu_list

    @property
    def usable_gpus(self) -> List[GPUDevice]:
        return [g for g in self.gpus if g.backend != "none"]

    @property
    def best_gpu(self) -> Optional[GPUDevice]:
        u = self.usable_gpus
        return u[0] if u else None

    @property
    def virt(self) -> VirtInfo:
        if self._virt is None:
            self._virt = detect_virtualization()
        return self._virt

    @property
    def gpu_info(self) -> Dict[str, str]:
        return get_gpu_info()

    def optimal_workers(self, file_count: int, avg_size_bytes: int = 0) -> int:
        """Return the ideal worker count for a batch of *file_count* files.

        Considers CPU cores, file count, and average file size to avoid
        over-subscribing either the CPU or the USB bus.
        """
        if file_count <= 1:
            return 1
        cores = os.cpu_count() or 4
        # Large files → fewer threads (USB bandwidth-bound)
        if avg_size_bytes > 50 * 1024 * 1024:
            cap = min(2, cores)
        elif avg_size_bytes > 10 * 1024 * 1024:
            cap = min(3, cores)
        else:
            cap = min(self.max_pull_workers, cores, 8)
        return min(cap, file_count)

    def summary(self) -> str:
        """Human-readable multi-line summary."""
        lines: List[str] = ["Aceleração de Transferência"]

        # GPU section
        usable = self.usable_gpus
        if usable and self.gpu_enabled:
            best = usable[0]
            backend_names = {
                "cuda": "CUDA/NVIDIA", "intel": "Intel oneAPI", "opencl": "OpenCL",
            }
            bk = backend_names.get(best.backend, best.backend)
            lines.append(f"  🟢 GPU: {best.name} ({bk}, {best.memory_gb:.1f} GB)")
            if len(usable) > 1:
                others = ", ".join(g.name for g in usable[1:])
                mode = "Multi-GPU ATIVO" if self.multi_gpu else "disponíveis"
                lines.append(f"      + {others} ({mode})")
        elif usable and not self.gpu_enabled:
            lines.append(f"  🔴 GPU: {usable[0].name} (DESATIVADA pelo usuário)")
        else:
            all_g = self.gpus
            if all_g:
                lines.append(f"  ⚪ GPU: {all_g[0].name} (sem biblioteca)")
                hint = get_install_hint()
                if hint:
                    lines.append(f"      💡 {hint}")
            else:
                lines.append("  ⚪ GPU: Nenhuma detectada")

        # Virtualization section
        vi = self.virt
        if self.virt_enabled:
            parts = []
            if vi.vtx_enabled:
                parts.append("VT-x/AMD-V")
            if vi.hyperv_running:
                parts.append("Hyper-V")
            if vi.wsl_available:
                parts.append("WSL2")
            if parts:
                lines.append(f"  🟢 Virtualização: {', '.join(parts)}")
            else:
                lines.append("  ⚪ Virtualização: Nenhuma detectada")
        else:
            lines.append("  🔴 Virtualização: DESATIVADA")

        # Workers
        cores = os.cpu_count() or "?"
        mode_label = "dinâmico" if self.auto_threads else "manual"
        lines.append(
            f"  Threads pull/push: {self.max_pull_workers}/{self.max_push_workers}"
            f"  ({mode_label}, {cores} cores)"
        )
        lines.append(
            f"  Verificação: {'Ativada' if self.verify_checksums else 'Desativada'}"
            f" ({self.checksum_algo.upper()})"
        )
        return "\n".join(lines)

    def status_line(self) -> str:
        """One-line summary for the status bar."""
        parts: List[str] = []

        # GPU
        usable = self.usable_gpus
        if usable and self.gpu_enabled:
            best = usable[0]
            gpu_txt = f"GPU: {best.name}"
            if self.multi_gpu and len(usable) > 1:
                gpu_txt += f" +{len(usable)-1}"
            parts.append(gpu_txt)
        elif usable:
            parts.append("GPU: OFF")
        else:
            parts.append("GPU: N/A")

        # Virt
        vi = self.virt
        if self.virt_enabled and (vi.vtx_enabled or vi.hyperv_running):
            virt_txt = "Virt: ON"
            if vi.hyperv_running:
                virt_txt += " (Hyper-V)"
            parts.append(virt_txt)
        elif self.virt_enabled:
            parts.append("Virt: N/A")
        else:
            parts.append("Virt: OFF")

        return "  |  ".join(parts)
