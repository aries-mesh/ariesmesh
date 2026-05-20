"""Inference catalog: device capabilities, available models, and feasible configs.

The registry treats every possible way to run a model — single device, multi-
device distributed via llama.cpp RPC, or cloud-routed — as a single
`InferenceConfig` object. The scheduler then scores them against each other
using the same 5-dimension weighting (privacy / capability / latency / cost /
health) that single-agent routing already uses.

Configs are recomputed whenever a device connects/disconnects or reports a
new capability snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..scheduler.router import DeviceHealth, Locality, ScoringWeights, TaskConstraints


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DeviceRole:
    """A device's role inside a single InferenceConfig."""

    device_did: str
    role: str  # "host" | "worker" | "observer"
    memory_allocated_gb: float
    has_gpu: bool = False
    gpu_vram_gb: float = 0.0
    backend: str = "cpu"  # "metal" | "cuda" | "cpu" | "vulkan"


@dataclass
class ModelInfo:
    """Metadata for a GGUF model file located on a specific device."""

    name: str
    filename: str
    size_gb: float
    path: str
    device_did: str
    layer_count: int = 0
    context_window: int = 0


@dataclass
class DeviceCapability:
    """Extended capability report for inference planning.

    Includes everything DeviceHealth tracks plus llama.cpp availability,
    locally-discovered models, and measured per-peer network characteristics.
    """

    device_did: str
    ram_total_gb: float = 0.0
    ram_available_gb: float = 0.0
    has_gpu: bool = False
    gpu_name: str = "none"
    vram_total_gb: float = 0.0
    vram_available_gb: float = 0.0
    backend: str = "cpu"
    disk_read_speed_mbps: float = 0.0

    llama_cpp_available: bool = False
    llama_cpp_path: Optional[str] = None
    rpc_server_path: Optional[str] = None

    available_models: list[ModelInfo] = field(default_factory=list)
    peer_latency_ms: dict[str, float] = field(default_factory=dict)
    peer_bandwidth_mbps: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_did": self.device_did,
            "ram_total_gb": self.ram_total_gb,
            "ram_available_gb": self.ram_available_gb,
            "has_gpu": self.has_gpu,
            "gpu_name": self.gpu_name,
            "vram_total_gb": self.vram_total_gb,
            "vram_available_gb": self.vram_available_gb,
            "backend": self.backend,
            "disk_read_speed_mbps": self.disk_read_speed_mbps,
            "llama_cpp_available": self.llama_cpp_available,
            "llama_cpp_path": self.llama_cpp_path,
            "rpc_server_path": self.rpc_server_path,
            "available_models": [
                {
                    "name": m.name,
                    "filename": m.filename,
                    "size_gb": m.size_gb,
                    "path": m.path,
                    "device_did": m.device_did,
                    "layer_count": m.layer_count,
                    "context_window": m.context_window,
                }
                for m in self.available_models
            ],
            "peer_latency_ms": dict(self.peer_latency_ms),
            "peer_bandwidth_mbps": dict(self.peer_bandwidth_mbps),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeviceCapability":
        models = [ModelInfo(**m) for m in d.get("available_models", [])]
        return cls(
            device_did=d["device_did"],
            ram_total_gb=float(d.get("ram_total_gb", 0.0)),
            ram_available_gb=float(d.get("ram_available_gb", 0.0)),
            has_gpu=bool(d.get("has_gpu", False)),
            gpu_name=str(d.get("gpu_name", "none")),
            vram_total_gb=float(d.get("vram_total_gb", 0.0)),
            vram_available_gb=float(d.get("vram_available_gb", 0.0)),
            backend=str(d.get("backend", "cpu")),
            disk_read_speed_mbps=float(d.get("disk_read_speed_mbps", 0.0)),
            llama_cpp_available=bool(d.get("llama_cpp_available", False)),
            llama_cpp_path=d.get("llama_cpp_path"),
            rpc_server_path=d.get("rpc_server_path"),
            available_models=models,
            peer_latency_ms=dict(d.get("peer_latency_ms", {})),
            peer_bandwidth_mbps=dict(d.get("peer_bandwidth_mbps", {})),
        )


@dataclass
class InferenceConfig:
    """A single feasible way to run inference on the mesh."""

    config_id: str
    model_name: str
    model_size_gb: float
    config_type: str  # "local" | "distributed" | "cloud"
    devices: list[DeviceRole]

    estimated_tok_s: float
    estimated_ttft_s: float

    privacy_score: float
    capability_score: float
    latency_score: float
    cost_score: float
    health_score: float

    trusted_device_did: str
    rpc_endpoints: list[str] = field(default_factory=list)
    tensor_split: Optional[list[float]] = None

    def weighted_score(self, weights: Optional[ScoringWeights] = None) -> float:
        w = weights or ScoringWeights()
        total = (
            w.privacy * self.privacy_score
            + w.capability * self.capability_score
            + w.latency * self.latency_score
            + w.cost * self.cost_score
            + w.health * self.health_score
        )
        denom = w.privacy + w.capability + w.latency + w.cost + w.health
        return total / denom if denom else 0.0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class InferenceRegistry:
    """Catalog of all feasible InferenceConfigs across the mesh."""

    def __init__(self, weights: Optional[ScoringWeights] = None) -> None:
        self._models: dict[str, list[ModelInfo]] = {}
        self._device_capabilities: dict[str, DeviceCapability] = {}
        self._configs: list[InferenceConfig] = []
        self._weights = weights or ScoringWeights()

    # --- mutators -----------------------------------------------------------

    def update_device(self, device_did: str, capability: DeviceCapability) -> None:
        self._device_capabilities[device_did] = capability
        for model in capability.available_models:
            self._register_model_no_recompute(model)
        self._compute_configs()

    def remove_device(self, device_did: str) -> None:
        self._device_capabilities.pop(device_did, None)
        for name, lst in list(self._models.items()):
            lst[:] = [m for m in lst if m.device_did != device_did]
            if not lst:
                self._models.pop(name, None)
        self._compute_configs()

    def register_model(self, model_info: ModelInfo) -> None:
        self._register_model_no_recompute(model_info)
        self._compute_configs()

    def _register_model_no_recompute(self, model_info: ModelInfo) -> None:
        lst = self._models.setdefault(model_info.name, [])
        # Replace any prior record for the same device
        lst[:] = [m for m in lst if m.device_did != model_info.device_did]
        lst.append(model_info)

    # --- accessors ----------------------------------------------------------

    def get_configs(self, min_capability: str = "text.qa") -> list[InferenceConfig]:
        return list(self._configs)

    def get_best_config(
        self,
        task_constraints: TaskConstraints,
        device_healths: Optional[dict[str, DeviceHealth]] = None,
    ) -> Optional[InferenceConfig]:
        device_healths = device_healths or {}
        candidates = self._filter_by_constraints(task_constraints)
        if not candidates:
            return None
        return max(candidates, key=lambda c: self._live_score(c, device_healths))

    # --- internals ----------------------------------------------------------

    def _live_score(
        self, config: InferenceConfig, device_healths: dict[str, DeviceHealth]
    ) -> float:
        health = config.health_score
        for role in config.devices:
            live = device_healths.get(role.device_did)
            if live is not None:
                health = min(health, live.health_score)
        w = self._weights
        total = (
            w.privacy * config.privacy_score
            + w.capability * config.capability_score
            + w.latency * config.latency_score
            + w.cost * config.cost_score
            + w.health * health
        )
        denom = w.privacy + w.capability + w.latency + w.cost + w.health
        return total / denom if denom else 0.0

    def _filter_by_constraints(
        self, constraints: TaskConstraints
    ) -> list[InferenceConfig]:
        out: list[InferenceConfig] = []
        for config in self._configs:
            if constraints.locality is Locality.LOCAL_ONLY and config.config_type == "cloud":
                continue
            out.append(config)
        return out

    def _compute_configs(self) -> None:
        configs: list[InferenceConfig] = []
        for model_name, model_list in self._models.items():
            for model_info in model_list:
                host_did = model_info.device_did
                host_cap = self._device_capabilities.get(host_did)
                if host_cap is None:
                    continue

                host_mem = _usable_memory(host_cap)

                # Local config — model fits on the host alone.
                if host_mem >= model_info.size_gb:
                    configs.append(self._build_local_config(host_cap, model_info))

                # Distributed configs — host + each other capable device as worker.
                for worker_did, worker_cap in self._device_capabilities.items():
                    if worker_did == host_did:
                        continue
                    worker_mem = _usable_memory(worker_cap)
                    total_mem = host_mem + worker_mem
                    if total_mem < model_info.size_gb:
                        continue
                    configs.append(
                        self._build_distributed_config(
                            host_cap, worker_cap, model_info, host_mem, worker_mem
                        )
                    )

        self._configs = configs

    def _build_local_config(
        self, host_cap: DeviceCapability, model_info: ModelInfo
    ) -> InferenceConfig:
        tok_s = _estimate_local_tok_s(host_cap, model_info)
        return InferenceConfig(
            config_id=f"local-{model_info.name}-{host_cap.device_did[:8]}",
            model_name=model_info.name,
            model_size_gb=model_info.size_gb,
            config_type="local",
            devices=[
                DeviceRole(
                    device_did=host_cap.device_did,
                    role="host",
                    memory_allocated_gb=model_info.size_gb,
                    has_gpu=host_cap.has_gpu,
                    gpu_vram_gb=host_cap.vram_total_gb,
                    backend=host_cap.backend,
                )
            ],
            estimated_tok_s=tok_s,
            estimated_ttft_s=_estimate_ttft(host_cap, model_info),
            privacy_score=1.0,
            capability_score=_capability_score(model_info),
            latency_score=min(tok_s / 30.0, 1.0),
            cost_score=1.0,
            health_score=0.8,
            trusted_device_did=host_cap.device_did,
            rpc_endpoints=[],
            tensor_split=None,
        )

    def _build_distributed_config(
        self,
        host_cap: DeviceCapability,
        worker_cap: DeviceCapability,
        model_info: ModelInfo,
        host_mem: float,
        worker_mem: float,
    ) -> InferenceConfig:
        total_mem = host_mem + worker_mem
        split = [host_mem / total_mem, worker_mem / total_mem]

        latency_ms = host_cap.peer_latency_ms.get(worker_cap.device_did, 50.0)
        bandwidth_mbps = host_cap.peer_bandwidth_mbps.get(worker_cap.device_did, 100.0)
        # Two transfers per token, plus serialization latency.
        per_token_overhead_s = (latency_ms / 1000.0) * 2 + (1.0 / max(bandwidth_mbps, 1.0))
        base_tok_s = _estimate_local_tok_s(host_cap, model_info)
        tok_s = 1.0 / (1.0 / max(base_tok_s, 1.0) + per_token_overhead_s)

        return InferenceConfig(
            config_id=(
                f"dist-{model_info.name}-{host_cap.device_did[:8]}-{worker_cap.device_did[:8]}"
            ),
            model_name=model_info.name,
            model_size_gb=model_info.size_gb,
            config_type="distributed",
            devices=[
                DeviceRole(
                    device_did=host_cap.device_did,
                    role="host",
                    memory_allocated_gb=model_info.size_gb * split[0],
                    has_gpu=host_cap.has_gpu,
                    gpu_vram_gb=host_cap.vram_total_gb,
                    backend=host_cap.backend,
                ),
                DeviceRole(
                    device_did=worker_cap.device_did,
                    role="worker",
                    memory_allocated_gb=model_info.size_gb * split[1],
                    has_gpu=worker_cap.has_gpu,
                    gpu_vram_gb=worker_cap.vram_total_gb,
                    backend=worker_cap.backend,
                ),
            ],
            estimated_tok_s=tok_s,
            estimated_ttft_s=_estimate_ttft(host_cap, model_info),
            privacy_score=0.9,  # still household-local, but over LAN
            capability_score=_capability_score(model_info),
            latency_score=min(tok_s / 30.0, 1.0),
            cost_score=1.0,
            health_score=min(0.8, _min_health(host_cap, worker_cap)),
            trusted_device_did=host_cap.device_did,
            # Real IP is resolved at setup() time from the live PeerConnection.
            rpc_endpoints=[f"{worker_cap.device_did}:50052"],
            tensor_split=split,
        )


# ---------------------------------------------------------------------------
# Estimation helpers
# ---------------------------------------------------------------------------


def _usable_memory(cap: DeviceCapability) -> float:
    if cap.has_gpu and cap.vram_available_gb > 0:
        return cap.vram_available_gb
    return cap.ram_available_gb


def _estimate_local_tok_s(cap: DeviceCapability, model: ModelInfo) -> float:
    """Rough first-pass estimate. v0.3 will replace this with measured benchmarks."""
    headroom = _usable_memory(cap) / max(model.size_gb, 1.0)
    if cap.has_gpu and cap.vram_available_gb > 0:
        return min(20.0 * headroom, 60.0)
    return min(5.0 * headroom, 20.0)


def _estimate_ttft(cap: DeviceCapability, model: ModelInfo) -> float:
    """Time to first token ≈ how long it takes to read the weights into memory."""
    mbps = max(cap.disk_read_speed_mbps, 50.0)
    gb_per_s = mbps / 1000.0
    return model.size_gb / max(gb_per_s, 0.1)


def _capability_score(model: ModelInfo) -> float:
    # 7B (~4 GB) gets ~0.15, 70B (~40 GB) gets ~1.0.
    size_score = min(model.size_gb / 35.0, 1.0)
    ctx_score = min(model.context_window / 128_000.0, 1.0) if model.context_window else 0.3
    return 0.7 * size_score + 0.3 * ctx_score


def _min_health(*caps: DeviceCapability) -> float:
    # No live DeviceHealth here; assume 0.8 as a placeholder which get_best_config
    # overrides with min(live device_healths) when actually picking a config.
    return 0.8
