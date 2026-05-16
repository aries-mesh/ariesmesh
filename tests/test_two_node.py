"""Two-node integration test: pairing, CRDT memory sync, handoff with auto-resume.

Uses loopback TCP between two AriesNodes in temp dirs. Skips mDNS by wiring
the transports together directly (this test focuses on protocol correctness,
not mDNS — which is exercised in the manual WSL2 smoke described in the plan).
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from aries.adapters.base import Message
from aries.adapters.mock_adapter import MockAdapter
from aries.continuation import HandoffReason
from aries.node import AriesNode
from aries.scheduler.router import Locality


async def _wire(a: AriesNode, b: AriesNode) -> None:
    """Connect node A to node B over loopback and exchange identity ANNOUNCEs."""
    assert a.transport is not None and b.transport is not None
    assert a.household is not None and b.household is not None

    from aries.transport.peer import PeerInfo, AriesMessage, MessageTypes

    # A connects to B
    peer_b = PeerInfo(
        device_did=b.household.device_did or "",
        name="b",
        host="127.0.0.1",
        port=b.transport.port,
        household_tag=b.household.household_tag,
    )
    conn_a_to_b = await a.transport.connect_to_peer(peer_b)
    await conn_a_to_b.send(
        AriesMessage(
            type=MessageTypes.ANNOUNCE,
            sender_did=a.household.device_did or "",
            body={"name": "a", "agents": []},
        )
    )

    # B connects back to A
    peer_a = PeerInfo(
        device_did=a.household.device_did or "",
        name="a",
        host="127.0.0.1",
        port=a.transport.port,
        household_tag=a.household.household_tag,
    )
    conn_b_to_a = await b.transport.connect_to_peer(peer_a)
    await conn_b_to_a.send(
        AriesMessage(
            type=MessageTypes.ANNOUNCE,
            sender_did=b.household.device_did or "",
            body={"name": "b", "agents": []},
        )
    )

    await asyncio.sleep(0.1)


async def _pair_via_household_clone(a: AriesNode, b: AriesNode) -> None:
    """Shortcut for tests: instead of going through the over-the-wire pairing
    handshake (which requires mDNS), have B reuse A's household manifest as if
    a membership UCAN had already been issued.

    This sidesteps the discovery layer while still exercising:
      - both nodes loading the same root_did / household_tag
      - the membership UCAN chain (A's manifest contains it)
      - transport, memory, sync, scheduler running on both
    """
    # Initialize B as a joiner, then have A issue a membership UCAN directly
    # via accept_pairing_request (bypassing mDNS).
    assert a.household is not None

    offer = a.household.start_pairing()
    device_did, _ = b.household.initialize_joiner("node-b", "linux")  # type: ignore[union-attr]
    membership_jwt = a.household.accept_pairing_request(
        candidate_device_did=device_did,
        candidate_device_name="node-b",
        candidate_platform="linux",
        presented_code=offer.code,
    )
    b.household.complete_joining(membership_jwt, "node-b", "linux")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_pairing_and_memory_sync() -> None:
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        node_a = AriesNode(data_dir=tmp_a)
        node_b = AriesNode(data_dir=tmp_b)

        await node_a.initialize("node-a", "linux")
        # initialize_joiner doesn't write a manifest; we need b.household later
        from aries.identity.household import Household
        node_b.household = Household(data_dir=tmp_b)

        await _pair_via_household_clone(node_a, node_b)

        await node_a.start(enable_discovery=False, enable_profiler=False)
        await node_b.start(enable_discovery=False, enable_profiler=False)

        await _wire(node_a, node_b)

        # both nodes have the same root DID
        assert node_a.household.user_root_did == node_b.household.user_root_did
        assert node_a.household.household_tag == node_b.household.household_tag

        # write on A, expect to appear on B after sync debounce
        assert node_a.memory is not None and node_b.memory is not None
        node_a.memory.set("context://test/value", "hello-from-a")

        # wait up to 2s for value to propagate
        for _ in range(20):
            await asyncio.sleep(0.1)
            if node_b.memory.get("context://test/value") == "hello-from-a":
                break
        assert node_b.memory.get("context://test/value") == "hello-from-a"

        await node_a.stop()
        await node_b.stop()


@pytest.mark.asyncio
async def test_handoff_auto_resume() -> None:
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        node_a = AriesNode(data_dir=tmp_a)
        node_b = AriesNode(data_dir=tmp_b)

        await node_a.initialize("node-a", "linux")
        from aries.identity.household import Household
        node_b.household = Household(data_dir=tmp_b)
        await _pair_via_household_clone(node_a, node_b)

        await node_a.start(enable_discovery=False, enable_profiler=False)
        await node_b.start(enable_discovery=False, enable_profiler=False)
        await _wire(node_a, node_b)

        # B is the "private" device that can run a mock locally
        node_b.register_agent(MockAdapter(canned_response="[B answered]"))

        # A has no agents — it must hand off
        task_id = "task_handoff_test"
        assert node_a.memory is not None
        # Pre-populate history: a user message
        node_a.memory.log_append(
            f"context://tasks/{task_id}/history",
            Message(role="user", content="please help (handoff)").to_dict(),
        )

        # We verify B locally produced a response in its own memory after auto-resume.
        # Fix 2: handoff requires an explicit target now — pass B's device DID.
        cont = await node_a.handoff(
            task_id=task_id,
            reason=HandoffReason(code="capability_need", description="A has no agents"),
            target_device_did=node_b.household.device_did,  # type: ignore[union-attr,arg-type]
            required_capabilities=["text.qa"],
            target_locality="any",
        )
        assert cont.task_id == task_id

        # Wait for B to auto-resume and write a response to its memory
        for _ in range(30):
            await asyncio.sleep(0.1)
            history = node_b.memory.log_read(f"context://tasks/{task_id}/history")  # type: ignore[union-attr]
            if any(m.get("role") == "assistant" and "[B answered]" in m.get("content", "") for m in history):
                break
        history = node_b.memory.log_read(f"context://tasks/{task_id}/history")  # type: ignore[union-attr]
        assert any(m.get("role") == "assistant" and "[B answered]" in m.get("content", "") for m in history), (
            f"B did not auto-resume; history={history}"
        )

        await node_a.stop()
        await node_b.stop()
