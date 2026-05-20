"""Tests for the Phase 5 web dashboard JSON + SSE API."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from aries.api.server import DashboardAPI
from aries.identity.household import AgentRecord
from aries.memory.store import MemoryStore
from aries.scheduler.router import DeviceHealth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_node(
    *,
    with_profiler: bool = False,
    with_peer: bool = False,
    with_agents: int = 0,
    with_memory: bool = False,
) -> SimpleNamespace:
    """Build a minimal duck-typed AriesNode for API tests."""
    device_did = "did:key:z6Mk-test-host-device"
    household = SimpleNamespace(
        device_did=device_did,
        user_root_did="did:key:z6Mk-test-root",
        devices={device_did: SimpleNamespace(name="test-host")},
        agents={},
    )

    if with_agents:
        for i in range(with_agents):
            agent_did = f"did:key:z6Mk-agent-{i:02d}"
            household.agents[agent_did] = AgentRecord(
                agent_did=agent_did,
                name=f"agent-{i}",
                vendor="mock",
                model=f"mock-{i}",
                capabilities=["text.qa"],
                context_window=4096,
                locality="local",
                cost_class="free",
            )

    profiler = None
    if with_profiler:
        snap = DeviceHealth(
            device_did=device_did,
            cpu_percent=42.0,
            ram_available_gb=4.0,
            ram_total_gb=16.0,
            battery_pct=78.0,
            charging=True,
            thermal="nominal",
            network_type="wifi",
            bandwidth_mbps=42.0,
        )
        profiler = SimpleNamespace(latest=snap)

    transport = SimpleNamespace(port=47291, _connections={})
    if with_peer:
        peer_info = SimpleNamespace(
            device_did="did:key:z6Mk-peer",
            name="linux-desktop",
            host="127.0.0.1",
            port=47292,
            latency_ms=48.0,
            last_seen=time.time(),
            household_tag="test",
            capabilities=[],
        )
        peer_conn = SimpleNamespace(peer=peer_info, is_connected=True)
        transport._connections["did:key:z6Mk-peer"] = peer_conn

    memory = None
    if with_memory:
        memory = MemoryStore(device_did=device_did)
        memory.set("aries:context://tasks/abc/response", "value-1")
        memory.set("aries:memory://prefs/theme", "dark")
        memory.log_append("aries:context://tasks/abc/history", {"role": "user", "content": "hi"})

    return SimpleNamespace(
        household=household,
        transport=transport,
        profiler=profiler,
        memory=memory,
        _inference_registry=None,
        _inference_coordinator=None,
        _receipt_chains={},
        _start_time=time.time() - 60.0,
    )


async def _start_api(node: SimpleNamespace) -> tuple[DashboardAPI, str]:
    """Start the API on an OS-assigned port; return (api, base_url)."""
    api = DashboardAPI(node, host="127.0.0.1", port=0)
    ok = await api.start()
    assert ok, "DashboardAPI failed to bind to an ephemeral port"
    return api, f"http://127.0.0.1:{api.port}"


# ---------------------------------------------------------------------------
# Test 1 — /api/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_status_endpoint() -> None:
    node = _make_mock_node()
    api, base = await _start_api(node)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/api/status")
        assert r.status_code == 200
        data = r.json()
        assert data["device_did"].startswith("did:key:")
        assert data["device_name"] == "test-host"
        assert data["protocol_version"] == "v0.2"
        assert data["encrypted_transport"] is True
        assert data["uptime_seconds"] >= 0
        assert "device_did_short" in data
    finally:
        await api.stop()


# ---------------------------------------------------------------------------
# Test 2 — /api/health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_health_endpoint() -> None:
    node = _make_mock_node(with_profiler=True)
    api, base = await _start_api(node)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["cpu_percent"] == 42.0
        assert data["ram_available_gb"] == 4.0
        assert data["thermal"] == "nominal"
        assert "health_score" in data
        assert 0.0 <= data["health_score"] <= 1.0
    finally:
        await api.stop()


# ---------------------------------------------------------------------------
# Test 3 — /api/peers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_peers_endpoint() -> None:
    node = _make_mock_node(with_peer=True)
    api, base = await _start_api(node)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/api/peers")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        peer = data["peers"][0]
        assert peer["name"] == "linux-desktop"
        assert peer["latency_ms"] == 48.0
        assert peer["connected"] is True
        assert peer["device_did_short"].startswith("did:key:")
    finally:
        await api.stop()


# ---------------------------------------------------------------------------
# Test 4 — /api/agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_agents_endpoint() -> None:
    node = _make_mock_node(with_agents=2)
    api, base = await _start_api(node)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/api/agents")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        names = {a["name"] for a in data["agents"]}
        assert names == {"agent-0", "agent-1"}
        first = data["agents"][0]
        assert first["vendor"] == "mock"
        assert first["locality"] == "local"
        assert "text.qa" in first["capabilities"]
    finally:
        await api.stop()


# ---------------------------------------------------------------------------
# Test 5 — /api/memory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_memory_endpoint() -> None:
    node = _make_mock_node(with_memory=True)
    api, base = await _start_api(node)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/api/memory")
        assert r.status_code == 200
        data = r.json()
        assert data["total_keys"] >= 2
        assert data["by_namespace"]["context"] >= 1
        assert data["by_namespace"]["memory"] >= 1
        assert data["total_logs"] >= 1
        assert data["log_entries"] >= 1
        assert isinstance(data["lamport_clock"], int)
    finally:
        await api.stop()


# ---------------------------------------------------------------------------
# Test 6 — /api/events SSE stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_events_sse_stream() -> None:
    node = _make_mock_node()
    api, base = await _start_api(node)
    try:
        received: list[dict] = []

        async def _consume() -> None:
            timeout = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("GET", f"{base}/api/events") as resp:
                    assert resp.status_code == 200
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            payload = json.loads(line[6:])
                            received.append(payload)
                            if len(received) >= 1:
                                return

        consumer = asyncio.create_task(_consume())
        # Wait until the SSE handler has actually registered its queue
        # (httpx's stream open + initial header read takes a moment in CI).
        for _ in range(50):
            await asyncio.sleep(0.05)
            if api._sse_clients:
                break
        assert api._sse_clients, "SSE client failed to register within timeout"
        api.push_event("test_event", "hello from API test")

        await asyncio.wait_for(consumer, timeout=5.0)
        assert len(received) >= 1
        assert received[0]["type"] == "test_event"
        assert received[0]["description"] == "hello from API test"
    finally:
        await api.stop()
