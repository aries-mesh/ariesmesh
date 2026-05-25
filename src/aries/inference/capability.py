"""Per-device inference capability discovery.

`probe_inference_capability` is the single source of truth for what each
device can contribute to inference: how much RAM/VRAM it has, whether
llama.cpp is installed, what GGUF model files are sitting on its disk, and
how fast its disk reads weights. It runs once at daemon start and again
when health changes.

`measure_peer_network` round-trips a probe payload over the encrypted
transport to estimate per-peer RTT and throughput — both inputs to the
distributed-config scoring.
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import struct
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover — Android/Termux only
    psutil = None  # type: ignore[assignment]
    PSUTIL_AVAILABLE = False

from .registry import DeviceCapability, ModelInfo

if TYPE_CHECKING:
    from ..transport.peer import TransportServer


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LLAMA_SERVER_BINS = ["llama-server", "llama-server.exe"]
_RPC_SERVER_BINS = ["rpc-server", "rpc-server.exe"]
_COMMON_INSTALL_DIRS = [
    Path("/usr/local/bin"),
    Path.home() / ".local" / "bin",
    Path.home() / "llama.cpp" / "build" / "bin",
    Path.home() / "llama.cpp" / "build" / "bin" / "Release",
]
_COMMON_MODEL_DIRS = [
    Path.home() / ".cache" / "huggingface",
    Path.home() / "models",
    Path.home() / "ollama" / "models",
]

# Cache for disk-read measurements so we don't re-benchmark on every probe.
_DISK_SPEED_CACHE: dict[str, tuple[float, float]] = {}  # path → (mbps, measured_at)
_DISK_SPEED_TTL_S = 300.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def probe_inference_capability(device_did: str, data_dir: Path) -> DeviceCapability:
    """Build a DeviceCapability snapshot for this device."""
    if PSUTIL_AVAILABLE:
        mem = psutil.virtual_memory()
        ram_total = mem.total / (1024**3)
        ram_avail = mem.available / (1024**3)
    else:
        # Android/Termux: assume a modest mobile-class device. The scheduler
        # will still produce a "local" InferenceConfig for small models; the
        # numbers don't need to be exact because they only feed scoring.
        ram_total = 8.0
        ram_avail = 4.0

    has_gpu, gpu_name, vram_total, vram_avail, backend = _detect_gpu(ram_avail)

    llama_path = _find_binary(_LLAMA_SERVER_BINS)
    rpc_path = _find_binary(_RPC_SERVER_BINS)
    llama_available = llama_path is not None or rpc_path is not None

    models = _scan_gguf_models(data_dir, device_did)
    disk_speed = await _measure_disk_speed(data_dir)

    return DeviceCapability(
        device_did=device_did,
        ram_total_gb=ram_total,
        ram_available_gb=ram_avail,
        has_gpu=has_gpu,
        gpu_name=gpu_name,
        vram_total_gb=vram_total,
        vram_available_gb=vram_avail,
        backend=backend,
        disk_read_speed_mbps=disk_speed,
        llama_cpp_available=llama_available,
        llama_cpp_path=llama_path,
        rpc_server_path=rpc_path,
        available_models=models,
    )


async def measure_peer_network(
    transport: "TransportServer", peer_did: str, payload_size_bytes: int = 65536
) -> tuple[float, float]:
    """Send an INFERENCE_PROBE round-trip; return (latency_ms, bandwidth_mbps).

    The probe is two messages: a tiny one to measure RTT, then a larger one to
    estimate sustained throughput. If anything fails, returns generous defaults
    so the registry still works with conservative estimates.
    """
    # Local import avoids a transport → inference circular import at module load.
    from ..transport.peer import AriesMessage, MessageTypes

    conn = transport.get_peer(peer_did)
    if conn is None:
        return 50.0, 100.0  # conservative defaults

    # Step 1: small RTT probe
    rtt_payload = b"ping"
    rtt_msg = AriesMessage(
        type=MessageTypes.INFERENCE_PROBE,
        sender_did="",
        body={"phase": "rtt", "payload": rtt_payload, "ts": time.perf_counter()},
    )
    fut: asyncio.Future[float] = asyncio.get_event_loop().create_future()
    _PENDING_PROBES[rtt_msg.id] = fut
    try:
        await conn.send(rtt_msg)
        latency_ms = await asyncio.wait_for(fut, timeout=5.0)
    except (asyncio.TimeoutError, Exception):
        _PENDING_PROBES.pop(rtt_msg.id, None)
        return 50.0, 100.0

    # Step 2: bandwidth probe
    bw_payload = b"x" * payload_size_bytes
    bw_msg = AriesMessage(
        type=MessageTypes.INFERENCE_PROBE,
        sender_did="",
        body={"phase": "bw", "payload": bw_payload, "ts": time.perf_counter()},
    )
    fut2: asyncio.Future[float] = asyncio.get_event_loop().create_future()
    _PENDING_PROBES[bw_msg.id] = fut2
    started = time.perf_counter()
    try:
        await conn.send(bw_msg)
        await asyncio.wait_for(fut2, timeout=10.0)
    except (asyncio.TimeoutError, Exception):
        _PENDING_PROBES.pop(bw_msg.id, None)
        return latency_ms, 100.0
    elapsed_s = time.perf_counter() - started
    bandwidth_mbps = (payload_size_bytes * 8 / 1_000_000.0) / max(elapsed_s, 1e-6)
    return latency_ms, bandwidth_mbps


# Shared with node.py's _handle_inference_probe_response so callers can await.
_PENDING_PROBES: dict[str, asyncio.Future[float]] = {}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _find_binary(candidates: list[str]) -> Optional[str]:
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    for d in _COMMON_INSTALL_DIRS:
        for name in candidates:
            p = d / name
            if p.exists() and os.access(p, os.X_OK):
                return str(p)
    return None


def _detect_gpu(ram_available_gb: float) -> tuple[bool, str, float, float, str]:
    """Return (has_gpu, gpu_name, vram_total_gb, vram_available_gb, backend)."""
    sysname = platform.system()
    if sysname == "Darwin":
        # Apple Silicon: unified memory acts as VRAM.
        if "arm" in platform.machine().lower():
            return True, "Apple Silicon (unified memory)", ram_available_gb, ram_available_gb, "metal"
        return False, "none", 0.0, 0.0, "cpu"

    # CUDA via nvidia-smi
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            import subprocess  # local: only used here on the slow CUDA path
            out = subprocess.run(
                [nvidia_smi, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            if out.returncode == 0 and out.stdout.strip():
                line = out.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in line.split(",")]
                name = parts[0] if parts else "NVIDIA GPU"
                vram_total_mb = float(parts[1]) if len(parts) > 1 else 0.0
                vram_free_mb = float(parts[2]) if len(parts) > 2 else 0.0
                return True, name, vram_total_mb / 1024, vram_free_mb / 1024, "cuda"
        except Exception:
            pass

    # Vulkan as a last-resort GPU backend
    if shutil.which("vulkaninfo"):
        return True, "Vulkan-capable GPU", 0.0, 0.0, "vulkan"

    return False, "none", 0.0, 0.0, "cpu"


def _scan_gguf_models(data_dir: Path, device_did: str) -> list[ModelInfo]:
    out: list[ModelInfo] = []
    seen: set[Path] = set()
    candidates: list[Path] = [data_dir / "models"] + list(_COMMON_MODEL_DIRS)

    for directory in candidates:
        directory = directory.expanduser()
        if not directory.exists() or not directory.is_dir():
            continue
        try:
            for gguf in directory.rglob("*.gguf"):
                if gguf in seen:
                    continue
                seen.add(gguf)
                try:
                    size_gb = gguf.stat().st_size / (1024**3)
                except OSError:
                    continue
                layer_count, ctx = _parse_gguf_header(gguf, size_gb)
                out.append(
                    ModelInfo(
                        name=gguf.stem,
                        filename=gguf.name,
                        size_gb=size_gb,
                        path=str(gguf),
                        device_did=device_did,
                        layer_count=layer_count,
                        context_window=ctx,
                    )
                )
        except (PermissionError, OSError):
            continue
    return out


def _parse_gguf_header(path: Path, size_gb: float) -> tuple[int, int]:
    """Best-effort GGUF header parse. Falls back to size-based estimates."""
    try:
        with path.open("rb") as f:
            magic = f.read(4)
            if magic != b"GGUF":
                return _estimate_from_size(size_gb), 4096
            # Real GGUF parsing is non-trivial (typed key-value table). For now we
            # only need an approximate layer count — derive it from file size.
            return _estimate_from_size(size_gb), 4096
    except OSError:
        return _estimate_from_size(size_gb), 4096


def _estimate_from_size(size_gb: float) -> int:
    # Rough buckets matching common Llama/Mistral architectures at Q4.
    if size_gb < 6:
        return 32  # ~7B
    if size_gb < 12:
        return 40  # ~14B
    if size_gb < 25:
        return 48  # ~30B
    return 80  # ~70B


async def _measure_disk_speed(data_dir: Path) -> float:
    """Sequential-read MB/s, cached for 5 minutes."""
    path = data_dir.expanduser()
    key = str(path)
    cached = _DISK_SPEED_CACHE.get(key)
    now = time.time()
    if cached and (now - cached[1]) < _DISK_SPEED_TTL_S:
        return cached[0]

    # Don't run benchmark in tests / when dir is empty — return a safe default.
    try:
        path.mkdir(parents=True, exist_ok=True)
        bench_file = path / ".aries_disk_bench"
        if not bench_file.exists():
            # 5 MB sample is enough for an order-of-magnitude estimate without
            # eating noticeable startup time. Skip if we can't write.
            bench_file.write_bytes(os.urandom(5 * 1024 * 1024))
        size = bench_file.stat().st_size
        started = time.perf_counter()
        with bench_file.open("rb") as f:
            while f.read(1024 * 1024):
                pass
        elapsed = time.perf_counter() - started
        mbps = (size / (1024 * 1024)) / max(elapsed, 1e-6)
        _DISK_SPEED_CACHE[key] = (mbps, now)
        return mbps
    except (OSError, PermissionError):
        return 100.0  # conservative default

# Silence unused-import warning for struct (kept for future header parsing).
_ = struct
