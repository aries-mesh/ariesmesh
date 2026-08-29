"""Tests for distributed inference (Feature 2 / v0.2).

Twelve tests covering: registry scoring and config computation, capability
probing, message-type round-trip, coordinator setup/teardown protocol over
a real (encrypted) transport pair, and the invoke() fallback path when no
distributed configuration is available.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aries.adapters.base import Message
from aries.adapters.mock_adapter import MockAdapter
from aries.identity.did import public_key_to_did
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
    worker_did = public_key_to_did(worker_kp.public_bytes)

    worker_transport = TransportServer(device_keypair=worker_kp)
    await worker_transport.start()

    host_transport = TransportServer(device_keypair=host_kp)
    host_node = _MockNode(
        host_transport, device_did=public_key_to_did(host_kp.public_bytes)
    )

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


# ---------------------------------------------------------------------------
# Test 13 — max_tokens reaches the adapter on the single-agent path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_forwards_max_tokens_to_adapter() -> None:
    """A caller's token budget must survive the trip; the flag used to be dropped."""

    class _RecordingAdapter(MockAdapter):
        def __init__(self) -> None:
            super().__init__(canned_response="[recorded]")
            self.seen: list[int] = []

        async def invoke(self, request):  # type: ignore[override]
            self.seen.append(request.max_tokens)
            return await super().invoke(request)

    with tempfile.TemporaryDirectory() as tmp:
        node = AriesNode(data_dir=tmp)
        await node.initialize(device_name="solo", platform="linux")
        await node.start(enable_discovery=False, enable_profiler=False, enable_api=False)
        try:
            adapter = _RecordingAdapter()
            node.register_agent(adapter)

            await node.invoke(
                messages=[Message(role="user", content="hello")], max_tokens=77
            )
            assert adapter.seen == [77]

            # Callers that say nothing still get the documented default.
            await node.invoke(messages=[Message(role="user", content="hello")])
            assert adapter.seen == [77, 4096]
        finally:
            await node.stop()


# ---------------------------------------------------------------------------
# Test 14 — a pinned config is the one that runs, and carries max_tokens with it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pinned_config_runs_and_forwards_max_tokens(monkeypatch) -> None:
    """`aries inference run --model X --max-tokens N` must honour both X and N.

    Stubs the coordinator so no llama-server is needed — what is under test is
    the wiring from invoke() down to generate(), not llama.cpp itself.
    """
    seen: dict[str, object] = {}

    class _FakeCoordinator:
        def __init__(self, node, config) -> None:
            self.config = config

        async def setup(self) -> bool:
            return True

        async def generate(
            self, *, prompt, system_prompt=None, max_tokens=4096,
            temperature=0.7, stream=True,
        ):
            seen["max_tokens"] = max_tokens
            seen["config_id"] = self.config.config_id
            yield "ok"

        async def teardown(self) -> None:
            return None

    monkeypatch.setattr("aries.node.InferenceCoordinator", _FakeCoordinator)

    with tempfile.TemporaryDirectory() as tmp:
        node = AriesNode(data_dir=tmp)
        await node.initialize(device_name="solo", platform="linux")
        await node.start(enable_discovery=False, enable_profiler=False, enable_api=False)
        try:
            config = _distributed_config("did:key:host", "did:key:worker")
            resp = await node.invoke(
                messages=[Message(role="user", content="hi")],
                inference_config=config,
                max_tokens=123,
            )
            assert seen["max_tokens"] == 123
            # The pinned config ran — the scheduler did not substitute its own.
            assert seen["config_id"] == config.config_id
            assert resp.content == "ok"
        finally:
            await node.stop()


# ---------------------------------------------------------------------------
# Test 15 — `--model` selects by name, not by score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_inference_config_honours_the_named_model() -> None:
    """Shared by `inference run` and `inference benchmark`; both used to ignore
    `--model` entirely and let the scheduler pick whatever scored highest."""
    from aries.cli.main import _resolve_inference_config

    wanted = _distributed_config("did:key:host", "did:key:worker")
    wanted.model_name = "wanted-model"
    decoy = _distributed_config("did:key:host", "did:key:worker")
    decoy.model_name = "other-model"
    decoy.privacy_score = 1.0  # outscores `wanted`, but is the wrong model
    decoy.capability_score = 1.0

    node = SimpleNamespace(
        _inference_registry=SimpleNamespace(get_configs=lambda: [decoy, wanted]),
        transport=None,
    )
    assert decoy.weighted_score() > wanted.weighted_score()
    assert await _resolve_inference_config(node, "wanted-model", wait_s=0.0) is wanted

    # Unknown model and missing registry both refuse rather than guessing.
    assert await _resolve_inference_config(node, "no-such-model", wait_s=0.0) is None
    empty = SimpleNamespace(_inference_registry=None, transport=None)
    assert await _resolve_inference_config(empty, "x", wait_s=0.0) is None


# ---------------------------------------------------------------------------
# Test 16 — the benchmark timer separates prefill from decode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timed_runs_measures_ttft_not_total() -> None:
    """TTFT used to be reported as total wall time, which made every arm of a
    comparison look identical on that axis. Prefill and decode must be distinct."""
    from aries.cli.main import _timed_runs

    calls = {"n": 0}

    async def _stream():
        calls["n"] += 1
        await asyncio.sleep(0.10)  # prefill
        for _ in range(5):
            yield "tok"
            await asyncio.sleep(0.02)  # inter-token gap

    records = await _timed_runs(_stream, runs=2, warmup=1)

    # Warmup executed but is not in the medians.
    assert calls["n"] == 3
    assert len(records) == 2

    for r in records:
        assert r["tokens"] == 5
        # The headline regression: TTFT is prefill, not the whole request.
        assert r["ttft_s"] < r["total_s"] * 0.7
        assert r["ttft_s"] >= 0.08
        # 4 intervals across a ~0.10s decode window; wide bounds because sleep
        # granularity on Windows is coarse.
        assert 5.0 < r["decode_tok_s"] < 500.0


@pytest.mark.asyncio
async def test_timed_runs_excludes_empty_streams() -> None:
    from aries.cli.main import _timed_runs

    async def _empty():
        return
        yield ""  # pragma: no cover — makes this an async generator

    assert await _timed_runs(_empty, runs=2, warmup=0) == []


# ---------------------------------------------------------------------------
# Capability exchange helpers
# ---------------------------------------------------------------------------


async def _started_node(tmp: str, name: str) -> AriesNode:
    node = AriesNode(data_dir=tmp)
    await node.initialize(device_name=name, platform="linux")
    await node.start(enable_discovery=False, enable_profiler=False, enable_api=False)
    return node


async def _wait_for(predicate, timeout_s: float = 5.0) -> bool:
    """Poll until `predicate()` is true; the exchange is a two-message round trip."""
    deadline = 0.0
    while deadline < timeout_s:
        if predicate():
            return True
        await asyncio.sleep(0.05)
        deadline += 0.05
    return predicate()


def _fake_capability(device_did: str, ram_gb: float, models) -> DeviceCapability:
    return DeviceCapability(
        device_did=device_did,
        ram_total_gb=ram_gb,
        ram_available_gb=ram_gb,
        llama_cpp_available=True,
        llama_cpp_path="/usr/local/bin/llama-server",
        rpc_server_path="/usr/local/bin/rpc-server",
        available_models=list(models),
    )


# ---------------------------------------------------------------------------
# Test 17 — peers exchange capabilities on connect, both directions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peers_exchange_inference_capabilities() -> None:
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        node_a = await _started_node(tmp_a, "box-a")
        node_b = await _started_node(tmp_b, "box-b")
        did_a = node_a.household.device_did or ""
        did_b = node_b.household.device_did or ""
        try:
            await node_a._connect_and_announce(
                PeerInfo(
                    device_did=did_b,
                    name="box-b",
                    host="127.0.0.1",
                    port=node_b.transport.port,
                    household_tag="",
                )
            )

            # A pushed its capability with reply=True; B registers it and answers
            # once, so both registries end up holding both devices.
            assert await _wait_for(
                lambda: len(node_b._inference_registry._device_capabilities) == 2
            ), "peer capability never reached B"
            assert await _wait_for(
                lambda: len(node_a._inference_registry._device_capabilities) == 2
            ), "reply capability never reached A"

            assert did_a in node_b._inference_registry._device_capabilities
            assert did_b in node_a._inference_registry._device_capabilities

            # Dropping the link retires the peer's capability.
            for conn in list(node_a.transport._connections.values()):
                await conn.close()
            assert await _wait_for(
                lambda: did_b not in node_a._inference_registry._device_capabilities
            ), "capability outlived the connection"
        finally:
            await node_a.stop()
            await node_b.stop()


# ---------------------------------------------------------------------------
# Test 18 — a model too big for one box yields a distributed config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distributed_config_forms_across_two_devices() -> None:
    """The whole point of the exchange: before it, `_compute_configs` only ever
    saw one device and the worker loop ran zero iterations."""
    registry = InferenceRegistry()
    big_model = ModelInfo(
        name="llama-70b-q4",
        filename="llama-70b-q4.gguf",
        size_gb=40.0,
        path="/models/llama-70b-q4.gguf",
        device_did="did:key:host",
        layer_count=80,
        context_window=8192,
    )

    # Host alone cannot hold a 40 GB model.
    registry.update_device("did:key:host", _fake_capability("did:key:host", 24.0, [big_model]))
    assert registry.get_configs() == []

    # With a worker's memory published, the pair can.
    registry.update_device("did:key:worker", _fake_capability("did:key:worker", 24.0, []))
    configs = registry.get_configs()
    assert [c.config_type for c in configs] == ["distributed"]
    assert configs[0].model_name == "llama-70b-q4"
    assert {r.device_did for r in configs[0].devices} == {"did:key:host", "did:key:worker"}

    # And it goes away again when the worker leaves.
    registry.remove_device("did:key:worker")
    assert registry.get_configs() == []


# ---------------------------------------------------------------------------
# Test 19 — worker DIDs resolve to real addresses before llama-server sees them
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_resolves_worker_dids_to_addresses() -> None:
    host_node, worker_transport, worker_did, _, _ = await _make_host_worker_pair()

    async def _on_setup(msg: AriesMessage, conn: PeerConnection) -> None:
        await conn.send(
            AriesMessage(
                type=MessageTypes.INFERENCE_READY,
                sender_did=worker_did,
                body={"session_id": msg.body.get("session_id", "")},
            )
        )

    worker_transport.on_message(MessageTypes.INFERENCE_SETUP, _on_setup)

    config = _distributed_config(host_node.household.device_did, worker_did)
    # What the registry stores is a placeholder, not something llama.cpp can dial.
    assert config.rpc_endpoints == [f"{worker_did}:50052"]

    coordinator = InferenceCoordinator(node=host_node, config=config, setup_timeout=2.0)
    assert await coordinator.setup() is True
    assert coordinator.resolved_rpc_endpoints == ["127.0.0.1:50052"]

    await coordinator.teardown()
    assert coordinator.resolved_rpc_endpoints == []
    await host_node.transport.stop()
    await worker_transport.stop()


# ---------------------------------------------------------------------------
# Test 20 — a config hosted by another device is refused, not mis-run locally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_refuses_config_hosted_elsewhere() -> None:
    host_node, worker_transport, worker_did, _, _ = await _make_host_worker_pair()

    # Host role belongs to a third device: the GGUF is on its disk, not ours.
    config = _distributed_config("did:key:somebody-else", worker_did)
    coordinator = InferenceCoordinator(node=host_node, config=config, setup_timeout=0.5)

    assert await coordinator.setup() is False
    assert coordinator.resolved_rpc_endpoints == []

    await host_node.transport.stop()
    await worker_transport.stop()


# ---------------------------------------------------------------------------
# Test 21 — end to end: a peer's models become configurations on this device
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_models_become_configs_over_a_live_link() -> None:
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        # Only B has the model file on disk.
        models_dir = Path(tmp_b) / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        (models_dir / "tiny-peer-model.gguf").write_bytes(b"GGUF" + b"\x00" * 2048)

        node_a = await _started_node(tmp_a, "box-a")
        node_b = await _started_node(tmp_b, "box-b")
        did_b = node_b.household.device_did or ""
        try:
            assert any(
                m.name == "tiny-peer-model"
                for m in (node_b._local_inference_capability.available_models)
            ), "B did not discover its own GGUF"
            # A knows nothing about it yet.
            assert not [
                c for c in node_a._inference_registry.get_configs()
                if c.model_name == "tiny-peer-model"
            ]

            await node_a._connect_and_announce(
                PeerInfo(
                    device_did=did_b,
                    name="box-b",
                    host="127.0.0.1",
                    port=node_b.transport.port,
                    household_tag="",
                )
            )

            assert await _wait_for(
                lambda: any(
                    c.model_name == "tiny-peer-model"
                    for c in node_a._inference_registry.get_configs()
                )
            ), "peer's model never produced a configuration on A"

            config = next(
                c for c in node_a._inference_registry.get_configs()
                if c.model_name == "tiny-peer-model"
            )
            # The model lives on B, so B is the host — A cannot drive it.
            host_role = next(r for r in config.devices if r.role == "host")
            assert host_role.device_did == did_b
            coordinator = InferenceCoordinator(node=node_a, config=config, setup_timeout=0.5)
            assert await coordinator.setup() is False
        finally:
            await node_a.stop()
            await node_b.stop()


# ---------------------------------------------------------------------------
# Test 22 — probes survive the Noise message ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_measure_peer_network_over_encrypted_link() -> None:
    """The old 64 KiB default exceeded the 65535-byte Noise transport limit, so
    every bandwidth probe raised inside encrypt() and silently returned 100."""
    from aries.inference.capability import (
        DEFAULT_BANDWIDTH_MBPS,
        DEFAULT_LATENCY_MS,
        MAX_PROBE_PAYLOAD,
        measure_peer_network,
    )

    assert MAX_PROBE_PAYLOAD < 65535 - 16, "probe must fit a Noise transport message"

    host_node, worker_transport, worker_did, _, _ = await _make_host_worker_pair()

    async def _echo(msg: AriesMessage, conn: PeerConnection) -> None:
        await conn.send(
            AriesMessage(
                type=MessageTypes.INFERENCE_PROBE_RESPONSE,
                sender_did=worker_did,
                thread_id=msg.id,
                body=dict(msg.body),
            )
        )

    worker_transport.on_message(MessageTypes.INFERENCE_PROBE, _echo)
    host_node.transport.on_message(
        MessageTypes.INFERENCE_PROBE_RESPONSE, _resolve_probe_response
    )

    latency_ms, bandwidth_mbps = await measure_peer_network(
        host_node.transport, worker_did, sender_did="", rounds=2
    )

    # A real round trip happened: loopback beats the 50 ms give-up default.
    assert 0.0 < latency_ms < DEFAULT_LATENCY_MS
    assert bandwidth_mbps > 0.0

    # An unreachable peer still yields something the estimator can use.
    assert await measure_peer_network(host_node.transport, "did:key:nobody") == (
        DEFAULT_LATENCY_MS,
        DEFAULT_BANDWIDTH_MBPS,
    )

    await host_node.transport.stop()
    await worker_transport.stop()


async def _resolve_probe_response(msg: AriesMessage, conn: PeerConnection) -> None:
    """Stand-in for AriesNode._handle_inference_probe_response."""
    from aries.inference.capability import _PENDING_PROBES

    fut = _PENDING_PROBES.pop(msg.thread_id or "", None)
    if fut is None or fut.done():
        return
    sent_ts = msg.body.get("ts")
    fut.set_result(
        (time.perf_counter() - float(sent_ts)) * 1000.0
        if isinstance(sent_ts, (int, float))
        else 0.0
    )


# ---------------------------------------------------------------------------
# Test 23 — a measured link lands in the capability the estimator reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_link_measurement_reaches_capability() -> None:
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        node_a = await _started_node(tmp_a, "box-a")
        node_b = await _started_node(tmp_b, "box-b")
        did_b = node_b.household.device_did or ""
        try:
            await node_a._connect_and_announce(
                PeerInfo(
                    device_did=did_b,
                    name="box-b",
                    host="127.0.0.1",
                    port=node_b.transport.port,
                    household_tag="",
                )
            )

            assert await _wait_for(
                lambda: did_b in node_a._local_inference_capability.peer_latency_ms
            ), "link was never measured"

            cap = node_a._local_inference_capability
            assert cap.peer_latency_ms[did_b] >= 0.0
            assert cap.peer_bandwidth_mbps[did_b] > 0.0

            # Disconnecting forgets the link so a reconnect re-measures.
            for conn in list(node_a.transport._connections.values()):
                await conn.close()
            assert await _wait_for(lambda: did_b not in cap.peer_latency_ms)
        finally:
            await node_a.stop()
            await node_b.stop()
