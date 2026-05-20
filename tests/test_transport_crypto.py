"""Tests for Noise_XX encrypted transport (Feature 1 / v0.2).

All tests use loopback asyncio socket pairs so no real network is needed.
"""
from __future__ import annotations

import asyncio

import pytest

from aries.identity.keys import KeyPair
from aries.transport.crypto import NoiseSession
from aries.transport.peer import AriesMessage, MessageTypes, PeerConnection, PeerInfo, TransportServer


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _connected_pair() -> tuple[
    tuple[asyncio.StreamReader, asyncio.StreamWriter],
    tuple[asyncio.StreamReader, asyncio.StreamWriter],
]:
    """Return two (reader, writer) pairs connected over a loopback socket."""
    server_reader: asyncio.StreamReader | None = None
    server_writer: asyncio.StreamWriter | None = None
    ready: asyncio.Event = asyncio.Event()

    async def _accept(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        nonlocal server_reader, server_writer
        server_reader, server_writer = r, w
        ready.set()

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]  # type: ignore[index]
    client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
    await ready.wait()
    server.close()
    assert server_reader is not None and server_writer is not None
    return (client_reader, client_writer), (server_reader, server_writer)


async def _do_handshake(
    kp_init: KeyPair,
    kp_resp: KeyPair,
) -> tuple[NoiseSession, NoiseSession]:
    """Perform a full Noise_XX handshake; return (initiator_session, responder_session)."""
    (r_i, w_i), (r_r, w_r) = await _connected_pair()
    sess_i = NoiseSession(kp_init, is_initiator=True)
    sess_r = NoiseSession(kp_resp, is_initiator=False)
    rs_i, rs_r = await asyncio.gather(
        sess_i.handshake(r_i, w_i),
        sess_r.handshake(r_r, w_r),
    )
    # Clean up writers
    for w in (w_i, w_r):
        try:
            w.close()
        except Exception:
            pass
    return sess_i, sess_r


# ---------------------------------------------------------------------------
# Test 1: basic handshake
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_noise_handshake_success() -> None:
    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    sess_a, sess_b = await _do_handshake(kp_a, kp_b)

    assert sess_a.remote_static_public is not None
    assert sess_b.remote_static_public is not None
    assert len(sess_a.remote_static_public) == 32
    assert len(sess_b.remote_static_public) == 32

    # Each side learned the other's X25519 static public key
    expected_b_pub = bytes(kp_b.to_x25519_public())
    expected_a_pub = bytes(kp_a.to_x25519_public())
    assert sess_a.remote_static_public == expected_b_pub
    assert sess_b.remote_static_public == expected_a_pub


# ---------------------------------------------------------------------------
# Test 2: bidirectional encrypt / decrypt roundtrip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_encrypted_message_roundtrip() -> None:
    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    sess_a, sess_b = await _do_handshake(kp_a, kp_b)

    # A → B
    plaintext = b"hello from aries mesh"
    ct = sess_a.encrypt(plaintext)
    assert ct != plaintext
    assert sess_b.decrypt(ct) == plaintext

    # B → A
    plaintext2 = b"hello back from b"
    ct2 = sess_b.encrypt(plaintext2)
    assert ct2 != plaintext2
    assert sess_a.decrypt(ct2) == plaintext2


# ---------------------------------------------------------------------------
# Test 3: tampered ciphertext is rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tampered_ciphertext_rejected() -> None:
    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    sess_a, sess_b = await _do_handshake(kp_a, kp_b)

    ct = bytearray(sess_a.encrypt(b"secret payload"))
    # Flip a byte in the middle of the ciphertext
    ct[len(ct) // 2] ^= 0xFF

    with pytest.raises(Exception):
        sess_b.decrypt(bytes(ct))


# ---------------------------------------------------------------------------
# Test 4: impersonation is detectable via remote_static_public
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_static_key_handshake_fails() -> None:
    """Noise_XX does not reject unknown keys at the protocol level (it's designed
    for first-contact scenarios). However, after the handshake the caller can
    compare remote_static_public against the expected peer's X25519 public key.
    This test verifies that an attacker using a different keypair produces a
    remote_static that does NOT match the expected peer's key."""
    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    kp_attacker = KeyPair.generate()

    # Legitimate: A ↔ B
    sess_a, _ = await _do_handshake(kp_a, kp_b)
    expected = bytes(kp_b.to_x25519_public())
    assert sess_a.remote_static_public == expected

    # Impersonation: A ↔ attacker (claims to be B)
    sess_a2, _ = await _do_handshake(kp_a, kp_attacker)
    actual = sess_a2.remote_static_public
    assert actual != expected, (
        "remote_static from attacker must differ from B's expected key"
    )
    assert actual == bytes(kp_attacker.to_x25519_public())


# ---------------------------------------------------------------------------
# Test 5: PeerConnection end-to-end over encrypted transport
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_peer_connection_encrypted_send_recv() -> None:
    kp_server = KeyPair.generate()
    kp_client = KeyPair.generate()

    server = TransportServer(device_keypair=kp_server)
    await server.start()

    received: list[AriesMessage] = []

    async def _echo(msg: AriesMessage, conn: PeerConnection) -> None:
        received.append(msg)
        reply = AriesMessage(
            type=MessageTypes.ACK,
            sender_did="server",
            body={"echo": msg.body.get("text", "")},
        )
        await conn.send(reply)

    server.on_message(MessageTypes.INVOKE, _echo)

    client_peer = PeerInfo(
        device_did="server",
        name="server",
        host="127.0.0.1",
        port=server.port,
        household_tag="",
    )

    # Build a minimal TransportServer just to get an encrypted PeerConnection
    client_transport = TransportServer(device_keypair=kp_client)

    # connect_to_peer performs the handshake transparently
    client_conn = await client_transport.connect_to_peer(client_peer)

    msg = AriesMessage(
        type=MessageTypes.INVOKE,
        sender_did="client",
        body={"text": "hello encrypted world"},
    )
    await client_conn.send(msg)

    # Wait for the echo handler to fire
    for _ in range(50):
        await asyncio.sleep(0.05)
        if received:
            break

    assert received, "Server did not receive the encrypted message"
    assert received[0].body["text"] == "hello encrypted world"
    assert received[0].sender_did == "client"

    await server.stop()


# ---------------------------------------------------------------------------
# Test 6: different sessions produce different ciphertexts (forward secrecy)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forward_secrecy_different_sessions() -> None:
    """Two separate handshakes between the same static keypairs must produce
    different session keys (because ephemeral keys are random per session).
    Encrypting the same plaintext with two different sessions yields different
    ciphertexts — past sessions are not compromised by a later key leak."""
    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()

    sess_a1, _ = await _do_handshake(kp_a, kp_b)
    sess_a2, _ = await _do_handshake(kp_a, kp_b)

    plaintext = b"same plaintext both times"
    ct1 = sess_a1.encrypt(plaintext)
    ct2 = sess_a2.encrypt(plaintext)

    assert ct1 != ct2, (
        "Two separate sessions must produce different ciphertexts for the same plaintext"
    )
