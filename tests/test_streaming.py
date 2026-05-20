"""Tests for streaming token output (Feature 3 / Phase 4)."""
from __future__ import annotations

import asyncio
import tempfile
from typing import AsyncIterator

import pytest
from click.testing import CliRunner

from aries.adapters.base import BaseAdapter, InvokeRequest, InvokeResponse, Message
from aries.adapters.mock_adapter import MockAdapter
from aries.cli.main import cli
from aries.node import AriesNode
from aries.transport.peer import AriesMessage, MessageTypes


# ---------------------------------------------------------------------------
# Test 1 — invoke_stream yields tokens and persists conversation + receipt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_stream_yields_tokens() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        node = AriesNode(data_dir=tmp)
        await node.initialize("solo", "linux")
        await node.start(enable_discovery=False, enable_profiler=False, enable_api=False)
        try:
            adapter = MockAdapter(canned_response="one two three four")
            node.register_agent(adapter)

            tokens: list[str] = []
            async for tok in node.invoke_stream(
                messages=[Message(role="user", content="hello")]
            ):
                tokens.append(tok)
            assert len(tokens) >= 4  # MockAdapter splits on whitespace
            joined = "".join(tokens)
            assert "one" in joined and "two" in joined and "three" in joined and "four" in joined

            # Conversation should be in memory under a task_id under context://tasks/
            history_keys = [
                k for k in node.memory.keys("aries:context://tasks/")  # type: ignore[union-attr]
                if k.endswith("/response")
            ]
            assert history_keys, "Response should be persisted under context://tasks/<id>/response"

            # A receipt should have been chained
            chains = list(node._receipt_chains.values())
            assert any(len(c.receipts) >= 1 for c in chains)
        finally:
            await node.stop()


# ---------------------------------------------------------------------------
# Test 2 — adapter without streaming support falls back to a single chunk
# ---------------------------------------------------------------------------


class _NonStreamingAdapter(BaseAdapter):
    """Adapter that raises NotImplementedError on invoke_stream."""

    vendor = "mock"

    def __init__(self) -> None:
        self.model = "no-stream-1"

    async def invoke(self, request: InvokeRequest) -> InvokeResponse:
        return InvokeResponse(
            content="full response in one chunk",
            model=self.model,
            finish_reason="stop",
            usage={"prompt_tokens": 1, "completion_tokens": 5},
            latency_ms=0.1,
        )

    async def invoke_stream(self, request: InvokeRequest) -> AsyncIterator[str]:
        raise NotImplementedError("This adapter does not support streaming")
        yield ""  # unreachable; makes it a proper async generator

    async def health_check(self) -> bool:
        return True

    def capabilities(self) -> dict:
        return {
            "vendor": self.vendor,
            "model": self.model,
            "capabilities": ["text.qa"],
            "context_window": 4096,
            "locality": "local",
            "cost_class": "free",
        }


@pytest.mark.asyncio
async def test_invoke_stream_falls_back_to_batch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        node = AriesNode(data_dir=tmp)
        await node.initialize("solo", "linux")
        await node.start(enable_discovery=False, enable_profiler=False, enable_api=False)
        try:
            adapter = _NonStreamingAdapter()
            node.register_agent(adapter)

            chunks: list[str] = []
            async for tok in node.invoke_stream(
                messages=[Message(role="user", content="hi")]
            ):
                chunks.append(tok)
            assert chunks == ["full response in one chunk"]
        finally:
            await node.stop()


# ---------------------------------------------------------------------------
# Test 3 — STREAM_CHUNK message round-trips over a real PeerConnection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_chunk_message_over_transport() -> None:
    from aries.identity.keys import KeyPair
    from aries.transport.peer import PeerConnection, PeerInfo, TransportServer

    kp_server = KeyPair.generate()
    kp_client = KeyPair.generate()

    server = TransportServer(device_keypair=kp_server)
    await server.start()

    received: list[AriesMessage] = []

    async def _on_chunk(msg: AriesMessage, conn: PeerConnection) -> None:
        received.append(msg)

    server.on_message(MessageTypes.STREAM_CHUNK, _on_chunk)

    client_transport = TransportServer(device_keypair=kp_client)
    peer = PeerInfo(
        device_did="server",
        name="server",
        host="127.0.0.1",
        port=server.port,
        household_tag="",
    )
    conn = await client_transport.connect_to_peer(peer)

    msg = AriesMessage(
        type=MessageTypes.STREAM_CHUNK,
        sender_did="client",
        body={"task_id": "task_xyz", "token": "hi", "index": 7, "done": False},
    )
    await conn.send(msg)

    for _ in range(40):
        await asyncio.sleep(0.05)
        if received:
            break
    assert len(received) == 1
    assert received[0].body["task_id"] == "task_xyz"
    assert received[0].body["token"] == "hi"
    assert received[0].body["index"] == 7
    assert received[0].body["done"] is False

    await server.stop()


# ---------------------------------------------------------------------------
# Test 4 — CLI `aries invoke -m ... --stream` runs end-to-end via CliRunner
# ---------------------------------------------------------------------------


def test_invoke_cli_stream_flag() -> None:
    """Smoke test: the CLI invoke command does not crash and produces output.

    We use a temp data dir, initialize a household, and register a MockAdapter
    in the SAME process so the daemon process (which the CLI spawns inline)
    can re-attach the adapter for the agent record.
    """
    with tempfile.TemporaryDirectory() as tmp:
        runner = CliRunner()

        result = runner.invoke(cli, ["--data-dir", tmp, "init", "--name", "cli-test"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(
            cli,
            ["--data-dir", tmp, "register", "--vendor", "mock", "--model", "demo-1"],
        )
        assert result.exit_code == 0, result.output

        result = runner.invoke(
            cli,
            ["--data-dir", tmp, "invoke", "-m", "hello cli", "--stream"],
        )
        assert result.exit_code == 0, result.output
        # MockAdapter's canned response includes "[mock] hello from the mock adapter"
        # — at least one of those tokens should appear in the output.
        assert "mock" in result.output.lower()
        assert "Traceback" not in result.output
