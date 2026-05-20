"""Distributed inference across the mesh (Feature 2, v0.2).

The orchestration layer that sits between the scheduler and llama.cpp. The
scheduler chooses an `InferenceConfig` (local / distributed / cloud). The
coordinator brings the chosen configuration up (rpc-server on workers,
llama-server on the host) and streams tokens back through the encrypted
Aries transport.
"""
from .registry import (
    DeviceCapability,
    DeviceRole,
    InferenceConfig,
    InferenceRegistry,
    ModelInfo,
)

__all__ = [
    "DeviceCapability",
    "DeviceRole",
    "InferenceConfig",
    "InferenceRegistry",
    "ModelInfo",
]
