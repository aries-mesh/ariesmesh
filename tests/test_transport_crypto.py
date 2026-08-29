"""Tests for Noise_XX encrypted transport (Feature 1 / v0.2).

All tests use loopback asyncio socket pairs so no real network is needed.
"""
from __future__ import annotations

import asyncio

import pytest

from aries.identity.did import public_key_to_did
from aries.identity.keys import KeyPair
from aries.transport.crypto import NoiseSession, did_matches_static
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
    server_did = public_key_to_did(kp_server.public_bytes)
    client_did = public_key_to_did(kp_client.public_bytes)

    server = TransportServer(device_keypair=kp_server)
    await server.start()

    received: list[AriesMessage] = []

    async def _echo(msg: AriesMessage, conn: PeerConnection) -> None:
        received.append(msg)
        reply = AriesMessage(
            type=MessageTypes.ACK,
            sender_did=server_did,
            body={"echo": msg.body.get("text", "")},
        )
        await conn.send(reply)

    server.on_message(MessageTypes.INVOKE, _echo)

    client_peer = PeerInfo(
        device_did=server_did,
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
        sender_did=client_did,
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
    assert received[0].sender_did == client_did

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


# ---------------------------------------------------------------------------
# Test 7: a DID is only valid if the handshake key backs it
# ---------------------------------------------------------------------------

def test_did_matches_static_binds_did_to_key() -> None:
    kp = KeyPair.generate()
    did = public_key_to_did(kp.public_bytes)

    assert did_matches_static(did, bytes(kp.to_x25519_public()))
    # Someone else's key does not satisfy this DID.
    assert not did_matches_static(did, bytes(KeyPair.generate().to_x25519_public()))
    # A malformed identifier must fail closed, not sail through.
    assert not did_matches_static("not-a-did", bytes(kp.to_x25519_public()))


# ---------------------------------------------------------------------------
# Test 8: connecting to a peer advertising a DID it cannot back is refused
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_rejects_advertised_did_mismatch() -> None:
    """mDNS says a DID lives at this address; the key exchanged says otherwise."""
    kp_server = KeyPair.generate()
    kp_client = KeyPair.generate()
    # A DID belonging to some third device — the server cannot hold its key.
    impersonated_did = public_key_to_did(KeyPair.generate().public_bytes)

    server = TransportServer(device_keypair=kp_server)
    await server.start()
    client_transport = TransportServer(device_keypair=kp_client)

    peer = PeerInfo(
        device_did=impersonated_did,
        name="imposter",
        host="127.0.0.1",
        port=server.port,
        household_tag="",
    )
    with pytest.raises(ConnectionError):
        await client_transport.connect_to_peer(peer)

    await server.stop()


# ---------------------------------------------------------------------------
# Test 9: an inbound peer cannot speak as one of its household siblings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbound_spoofed_sender_did_is_dropped() -> None:
    kp_server = KeyPair.generate()
    kp_client = KeyPair.generate()
    server_did = public_key_to_did(kp_server.public_bytes)
    # The client holds kp_client but will claim to be this other device.
    victim_did = public_key_to_did(KeyPair.generate().public_bytes)

    server = TransportServer(device_keypair=kp_server)
    await server.start()

    received: list[AriesMessage] = []

    async def _capture(msg: AriesMessage, conn: PeerConnection) -> None:
        received.append(msg)

    server.on_message(MessageTypes.INVOKE, _capture)

    client_transport = TransportServer(device_keypair=kp_client)
    conn = await client_transport.connect_to_peer(
        PeerInfo(
            device_did=server_did,
            name="server",
            host="127.0.0.1",
            port=server.port,
            household_tag="",
        )
    )

    await conn.send(
        AriesMessage(
            type=MessageTypes.INVOKE,
            sender_did=victim_did,
            body={"text": "I am totally the other laptop"},
        )
    )

    for _ in range(20):
        await asyncio.sleep(0.05)
        if received:
            break

    assert not received, "spoofed message reached a handler"
    assert victim_did not in server._connections, "spoofed DID was registered"

    await server.stop()
