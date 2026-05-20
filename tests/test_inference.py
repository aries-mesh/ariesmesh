"""Tests for distributed inference (Feature 2 / v0.2).

Twelve tests covering: registry scoring and config computation, capability
probing, message-type round-trip, coordinator setup/teardown protocol over
a real (encrypted) transport pair, and the invoke() fallback path when no
distributed configuration is available.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from aries.adapters.base import Message
from aries.adapters.mock_adapter import MockAdapter
from aries.identity.keys import KeyPair
from aries.inference.capability import probe_inference_capability
from aries.inference.coordinator import InferenceCoordinator
from aries.inference.registry import (
    DeviceCapability,
    DeviceRole,
    InferenceConfig,
    InferenceRegistry,
    ModelInfo,
)
from aries.node import AriesNode
from aries.scheduler.router import (
    Locality,
    ScoringWeights,
)
from aries.transport.peer import (
    AriesMessage,
    MessageTypes,
    PeerConnection,
    PeerInfo,
    TransportServer,
)


# ---------------------------------------------------------------------------
# Test 1 — scoring: privacy weight dominates over capability/latency
# ---------------------------------------------------------------------------


def test_inference_config_scoring() -> None:
    distributed = InferenceConfig(
        config_id="dist-1",
        model_name="llama-70b",
        model_size_gb=40.0,
        config_type="distributed",
        devices=[
            DeviceRole(device_did="a", role="host", memory_allocated_gb=20.0),
            DeviceRole(device_did="b", role="worker", memory_allocated_gb=20.0),
        ],
        estimated_tok_s=6.0,
        estimated_ttft_s=2.0,
        privacy_score=1.0,
        capability_score=0.9,
        latency_score=0.3,
        cost_score=1.0,
        health_score=0.8,
        trusted_device_did="a",
    )
    cloud = InferenceConfig(
        config_id="cloud-1",
        model_name="claude-sonnet",
        model_size_gb=0.0,
        config_type="cloud",
        devices=[DeviceRole(device_did="cloud", role="host", memory_allocated_gb=0.0)],
        estimated_tok_s=50.0,
        estimated_ttft_s=0.5,
        privacy_score=0.2,
        capability_score=0.95,
        latency_score=0.9,
        cost_score=1.0,
        health_score=0.8,
        trusted_device_did="a",
    )
    weights = ScoringWeights()  # defaults: privacy=3.0
    assert distributed.weighted_score(weights) > cloud.weighted_score(weights)


# ---------------------------------------------------------------------------
# Test 2 — registry produces a local config when one device fits the model
# ---------------------------------------------------------------------------


def test_registry_computes_local_config() -> None:
    registry = InferenceRegistry()
    cap = DeviceCapability(
        device_did="dev-a",
        ram_total_gb=64.0,
        ram_available_gb=64.0,
        available_models=[
            ModelInfo(
                name="llama-70b",
                filename="llama-70b.gguf",
                size_gb=40.0,
                path="/m/llama-70b.gguf",
                device_did="dev-a",
                context_window=4096,
            )
        ],
    )
    registry.update_device("dev-a", cap)
    configs = registry.get_configs()
    assert len(configs) == 1
    assert configs[0].config_type == "local"
    assert configs[0].model_name == "llama-70b"


# ---------------------------------------------------------------------------
# Test 3 — distributed config across two devices, tensor_split ≈ [0.45, 0.55]
# ---------------------------------------------------------------------------


def test_registry_computes_distributed_config() -> None:
    registry = InferenceRegistry()
    cap_a = DeviceCapability(
        device_did="dev-a",
        ram_total_gb=32.0,
        ram_available_gb=20.0,
        available_models=[
            ModelInfo(
                name="llama-70b",
                filename="llama-70b.gguf",
                size_gb=40.0,
                path="/m/llama-70b.gguf",
                device_did="dev-a",
                context_window=4096,
            )
        ],
    )
    cap_b = DeviceCapability(
        device_did="dev-b",
        ram_total_gb=32.0,
        ram_available_gb=24.0,
    )
    registry.update_device("dev-a", cap_a)
    registry.update_device("dev-b", cap_b)

    configs = [c for c in registry.get_configs() if c.config_type == "distributed"]
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.tensor_split is not None
    assert cfg.tensor_split[0] == pytest.approx(20 / 44, abs=0.01)
    assert cfg.tensor_split[1] == pytest.approx(24 / 44, abs=0.01)


# ---------------------------------------------------------------------------
# Test 4 — no config when aggregate memory is insufficient
# ---------------------------------------------------------------------------


def test_registry_no_config_when_insufficient_memory() -> None:
    registry = InferenceRegistry()
    cap_a = DeviceCapability(
        device_did="dev-a",
        ram_total_gb=8.0,
        ram_available_gb=8.0,
        available_models=[
            ModelInfo(
                name="llama-70b",
                filename="llama-70b.gguf",
                size_gb=40.0,
                path="/m/llama-70b.gguf",
                device_did="dev-a",
                context_window=4096,
            )
        ],
    )
    cap_b = DeviceCapability(
        device_did="dev-b",
        ram_total_gb=8.0,
        ram_available_gb=8.0,
    )
    registry.update_device("dev-a", cap_a)
    registry.update_device("dev-b", cap_b)

    configs = registry.get_configs()
    assert configs == []


# ---------------------------------------------------------------------------
# Test 5 — removing a device removes its distributed configs
# ---------------------------------------------------------------------------


def test_registry_removes_device_on_disconnect() -> None:
    registry = InferenceRegistry()
    cap_a = DeviceCapability(
        device_did="dev-a",
        ram_total_gb=64.0,
        ram_available_gb=64.0,  # can host alone (local config exists)
        available_models=[
            ModelInfo(
                name="llama-70b",
                filename="llama-70b.gguf",
                size_gb=40.0,
                path="/m/llama-70b.gguf",
                device_did="dev-a",
                context_window=4096,
            )
        ],
    )
    cap_b = DeviceCapability(
        device_did="dev-b",
        ram_total_gb=32.0,
        ram_available_gb=24.0,
    )
    registry.update_device("dev-a", cap_a)
    registry.update_device("dev-b", cap_b)

    initial = registry.get_configs()
    assert any(c.config_type == "distributed" for c in initial)
    assert any(c.config_type == "local" for c in initial)

    registry.remove_device("dev-b")
    after = registry.get_configs()
    assert not any(c.config_type == "distributed" for c in after)
    assert any(c.config_type == "local" for c in after)


# ---------------------------------------------------------------------------
# Coordinator test helpers
# ---------------------------------------------------------------------------


class _MockNode:
    """Minimal duck-typed AriesNode for coordinator tests."""

    def __init__(self, transport: TransportServer, device_did: str) -> None:
        self.transport = transport
        self.household = SimpleNamespace(device_did=device_did)
        self.memory = None
        self._inference_registry = None
        self._inference_ready_futures: dict[str, asyncio.Future[bool]] = {}
        self._inference_rpc_processes: dict[str, asyncio.subprocess.Process] = {}


async def _make_host_worker_pair() -> tuple[_MockNode, TransportServer, str, KeyPair, KeyPair]:
    """Create a host MockNode and a separate worker TransportServer; connect them.

    Returns (host_node, worker_transport, worker_did, host_keypair, worker_keypair).
    """
    host_kp = KeyPair.generate()
    worker_kp = KeyPair.generate()
    worker_did = "did:key:worker-test"

    worker_transport = TransportServer(device_keypair=worker_kp)
    await worker_transport.start()

    host_transport = TransportServer(device_keypair=host_kp)
    host_node = _MockNode(host_transport, device_did="did:key:host-test")

    # Wire host's INFERENCE_READY handler to resolve the future.
    async def _on_ready(msg: AriesMessage, conn: PeerConnection) -> None:
        fut = host_node._inference_ready_futures.pop(msg.sender_did, None)
        if fut is not None and not fut.done():
            fut.set_result(True)

    host_transport.on_message(MessageTypes.INFERENCE_READY, _on_ready)

    # Connect host to worker
    worker_peer = PeerInfo(
        device_did=worker_did,
        name="worker",
        host="127.0.0.1",
        port=worker_transport.port,
        household_tag="",
    )
    await host_transport.connect_to_peer(worker_peer)

    return host_node, worker_transport, worker_did, host_kp, worker_kp


def _distributed_config(host_did: str, worker_did: str) -> InferenceConfig:
    return InferenceConfig(
        config_id="test-session-1",
        model_name="test-model",
        model_size_gb=10.0,
        config_type="distributed",
        devices=[
            DeviceRole(device_did=host_did, role="host", memory_allocated_gb=5.0),
            DeviceRole(device_did=worker_did, role="worker", memory_allocated_gb=5.0),
        ],
        estimated_tok_s=5.0,
        estimated_ttft_s=1.0,
        privacy_score=0.9,
        capability_score=0.5,
        latency_score=0.3,
        cost_score=1.0,
        health_score=0.8,
        trusted_device_did=host_did,
        rpc_endpoints=[f"{worker_did}:50052"],
        tensor_split=[0.5, 0.5],
    )


# ---------------------------------------------------------------------------
# Test 6 — coordinator sends INFERENCE_SETUP and resolves on INFERENCE_READY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_setup_sends_inference_setup() -> None:
    host_node, worker_transport, worker_did, _, _ = await _make_host_worker_pair()

    received: list[AriesMessage] = []

    async def _on_setup(msg: AriesMessage, conn: PeerConnection) -> None:
        received.append(msg)
        ready = AriesMessage(
            type=MessageTypes.INFERENCE_READY,
            sender_did=worker_did,
            body={"session_id": msg.body.get("session_id", ""), "port": 50052},
        )
        await conn.send(ready)

    worker_transport.on_message(MessageTypes.INFERENCE_SETUP, _on_setup)

    config = _distributed_config(host_node.household.device_did, worker_did)
    coordinator = InferenceCoordinator(node=host_node, config=config, setup_timeout=2.0)

    ok = await coordinator.setup()
    assert ok is True
    assert len(received) == 1
    assert received[0].type == MessageTypes.INFERENCE_SETUP
    assert received[0].body["session_id"] == "test-session-1"
    assert coordinator.workers_ready.get(worker_did) is True

    await coordinator.teardown()
    await host_node.transport.stop()
    await worker_transport.stop()


# ---------------------------------------------------------------------------
# Test 7 — coordinator setup times out when no INFERENCE_READY arrives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_setup_timeout() -> None:
    host_node, worker_transport, worker_did, _, _ = await _make_host_worker_pair()

    # Register a no-op handler so INFERENCE_SETUP is silently consumed.
    async def _swallow(msg: AriesMessage, conn: PeerConnection) -> None:
        return

    worker_transport.on_message(MessageTypes.INFERENCE_SETUP, _swallow)

    config = _distributed_config(host_node.household.device_did, worker_did)
    coordinator = InferenceCoordinator(node=host_node, config=config, setup_timeout=0.3)

    ok = await coordinator.setup()
    assert ok is False

    await host_node.transport.stop()
    await worker_transport.stop()


# ---------------------------------------------------------------------------
# Test 8 — teardown sends INFERENCE_TEARDOWN and clears active state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_teardown_sends_teardown() -> None:
    host_node, worker_transport, worker_did, _, _ = await _make_host_worker_pair()

    setup_received: list[AriesMessage] = []
    teardown_received: list[AriesMessage] = []

    async def _on_setup(msg: AriesMessage, conn: PeerConnection) -> None:
        setup_received.append(msg)
        await conn.send(
            AriesMessage(
                type=MessageTypes.INFERENCE_READY,
                sender_did=worker_did,
                body={"session_id": msg.body.get("session_id", "")},
            )
        )

    async def _on_teardown(msg: AriesMessage, conn: PeerConnection) -> None:
        teardown_received.append(msg)

    worker_transport.on_message(MessageTypes.INFERENCE_SETUP, _on_setup)
    worker_transport.on_message(MessageTypes.INFERENCE_TEARDOWN, _on_teardown)

    config = _distributed_config(host_node.household.device_did, worker_did)
    coordinator = InferenceCoordinator(node=host_node, config=config, setup_timeout=2.0)
    assert await coordinator.setup() is True
    assert coordinator.is_active is True

    await coordinator.teardown()
    # Give the wire a moment to deliver the teardown message.
    for _ in range(20):
        if teardown_received:
            break
        await asyncio.sleep(0.05)

    assert len(teardown_received) == 1
    assert teardown_received[0].type == MessageTypes.INFERENCE_TEARDOWN
    assert teardown_received[0].body["session_id"] == "test-session-1"
    assert coordinator.is_active is False

    await host_node.transport.stop()
    await worker_transport.stop()


# ---------------------------------------------------------------------------
# Test 9 — STREAM_CHUNK message CBOR round-trip
# ---------------------------------------------------------------------------


def test_stream_chunk_message_format() -> None:
    msg = AriesMessage(
        type=MessageTypes.STREAM_CHUNK,
        sender_did="did:key:test-host",
        body={"task_id": "task_abc", "token": "hello", "index": 0, "done": False},
    )
    blob = msg.to_cbor()
    restored = AriesMessage.from_cbor(blob)
    assert restored.type == MessageTypes.STREAM_CHUNK
    assert restored.sender_did == "did:key:test-host"
    assert restored.body["task_id"] == "task_abc"
    assert restored.body["token"] == "hello"
    assert restored.body["index"] == 0
    assert restored.body["done"] is False


# ---------------------------------------------------------------------------
# Test 10 — probe gracefully handles a host with no llama-cpp installed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capability_probe_detects_no_llama_cpp() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cap = await probe_inference_capability("did:key:probe-test", Path(tmp))
    # Test environment does not have llama-server or rpc-server installed.
    assert cap.llama_cpp_available is False
    assert cap.llama_cpp_path is None
    assert cap.rpc_server_path is None
    # No GGUF files in a fresh tmp dir.
    assert cap.available_models == []
    # ram_total_gb should be populated by psutil.
    assert cap.ram_total_gb > 0


# ---------------------------------------------------------------------------
# Test 11 — invoke() falls back to MockAdapter when no distributed configs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_falls_back_when_no_distributed_available() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        node = AriesNode(data_dir=tmp)
        await node.initialize(device_name="solo", platform="linux")
        await node.start(enable_discovery=False, enable_profiler=False, enable_api=False)
        try:
            adapter = MockAdapter(canned_response="[MOCK reply]")
            node.register_agent(adapter)

            # Sanity: registry is initialized but has no distributed configs
            # because the test environment has no GGUF files.
            assert node._inference_registry is not None
            configs = node._inference_registry.get_configs()
            assert not any(c.config_type == "distributed" for c in configs)

            resp = await node.invoke(
                messages=[Message(role="user", content="hello")],
                capability="text.qa",
                locality=Locality.HOUSEHOLD,
            )
            assert "[MOCK reply]" in resp.content
        finally:
            await node.stop()


# ---------------------------------------------------------------------------
# Test 12 — DeviceCapability serialization round-trip
# ---------------------------------------------------------------------------


def test_device_capability_serialization() -> None:
    original = DeviceCapability(
        device_did="did:key:abc",
        ram_total_gb=32.0,
        ram_available_gb=20.0,
        has_gpu=True,
        gpu_name="RTX 4090",
        vram_total_gb=24.0,
        vram_available_gb=22.0,
        backend="cuda",
        disk_read_speed_mbps=850.0,
        llama_cpp_available=True,
        llama_cpp_path="/usr/local/bin/llama-server",
        rpc_server_path="/usr/local/bin/rpc-server",
        available_models=[
            ModelInfo(
                name="llama-70b",
                filename="llama-70b.gguf",
                size_gb=40.0,
                path="/m/llama-70b.gguf",
                device_did="did:key:abc",
                layer_count=80,
                context_window=8192,
            )
        ],
        peer_latency_ms={"did:key:peer1": 12.5},
        peer_bandwidth_mbps={"did:key:peer1": 940.0},
    )
    blob = original.to_dict()
    restored = DeviceCapability.from_dict(blob)
    assert restored.device_did == original.device_did
    assert restored.ram_total_gb == original.ram_total_gb
    assert restored.has_gpu is True
    assert restored.gpu_name == "RTX 4090"
    assert restored.backend == "cuda"
    assert restored.llama_cpp_path == "/usr/local/bin/llama-server"
    assert restored.rpc_server_path == "/usr/local/bin/rpc-server"
    assert len(restored.available_models) == 1
    assert restored.available_models[0].name == "llama-70b"
    assert restored.available_models[0].layer_count == 80
    assert restored.peer_latency_ms["did:key:peer1"] == 12.5
    assert restored.peer_bandwidth_mbps["did:key:peer1"] == 940.0
