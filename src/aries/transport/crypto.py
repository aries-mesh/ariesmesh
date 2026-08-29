"""Noise_XX encrypted session for Aries transport.

Every byte exchanged between nodes passes through a NoiseSession. The XX
pattern means both sides transmit their static key, so neither side needs to
know the other's key in advance — but after the 3-message handshake both sides
have authenticated each other's static key with forward secrecy.

Static keys are the X25519 conversion of each device's Ed25519 signing key,
available via KeyPair.to_x25519_private() / to_x25519_public().
"""
from __future__ import annotations

import asyncio
import struct
from typing import Optional

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from nacl.signing import VerifyKey
from noise.connection import Keypair as NKeypair
from noise.connection import NoiseConnection

from ..identity.did import did_to_public_key
from ..identity.keys import KeyPair

_PROTOCOL = b"Noise_XX_25519_ChaChaPoly_SHA256"
_HANDSHAKE_BYTE = b"\x00"
_APP_BYTE = b"\x01"


class HandshakeError(Exception):
    """Raised when the Noise_XX handshake cannot be completed."""


def did_matches_static(did: str, remote_static: bytes) -> bool:
    """True if `remote_static` is the X25519 form of `did`'s Ed25519 key.

    The handshake authenticates a static key but says nothing about which DID
    owns it. This is the bridge: it converts the Ed25519 key embedded in a
    did:key the same way `KeyPair.to_x25519_public()` converts ours, then
    compares the raw bytes. A peer that cannot present the matching static key
    cannot claim that DID.
    """
    try:
        expected = bytes(VerifyKey(did_to_public_key(did)).to_curve25519_public_key())
    except Exception:
        return False
    return expected == remote_static


class NoiseSession:
    """Manages a single Noise_XX encrypted channel between two Aries nodes.

    Noise_XX pattern:
        → e                     initiator sends ephemeral public key
        ← e, ee, s, es          responder sends ephemeral + static; two DH ops
        → s, se                 initiator sends static; one more DH op

    After the three-message handshake both sides hold a pair of CipherState
    objects (one per direction) and have authenticated each other's static key.
    Ephemeral keys are discarded — forward secrecy is provided.
    """

    def __init__(self, device_keypair: KeyPair, is_initiator: bool) -> None:
        self._is_initiator = is_initiator
        self._remote_static: Optional[bytes] = None

        x25519_priv = bytes(device_keypair.to_x25519_private())
        conn = NoiseConnection.from_name(_PROTOCOL)
        if is_initiator:
            conn.set_as_initiator()
        else:
            conn.set_as_responder()
        conn.set_keypair_from_private_bytes(NKeypair.STATIC, x25519_priv)
        conn.start_handshake()
        self._conn = conn

    async def handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bytes:
        """Run the 3-message XX handshake over the open TCP streams.

        Returns the remote peer's X25519 static public key bytes (32 bytes).
        These are the curve25519 representation of the peer's Ed25519 device
        key, so they can be compared against did_to_public_key() output for
        identity verification.

        Raises HandshakeError if the handshake fails for any reason.
        """
        try:
            if self._is_initiator:
                # msg1: → e
                await _send_hs(writer, bytes(self._conn.write_message()))

                # msg2: ← e, ee, s, es  (responder's static key arrives here)
                self._conn.read_message(await _recv_hs(reader))

                # Capture remote static BEFORE write_message consumes handshake_state
                self._remote_static = _extract_rs(self._conn.noise_protocol.handshake_state)

                # msg3: → s, se
                await _send_hs(writer, bytes(self._conn.write_message()))

            else:
                # msg1: ← e
                self._conn.read_message(await _recv_hs(reader))

                # msg2: → e, ee, s, es
                await _send_hs(writer, bytes(self._conn.write_message()))

                # Grab the handshake_state reference BEFORE read_message triggers
                # split() and deletes it from noise_protocol. The HandshakeState
                # object itself survives (we hold a reference) and rs is set on it
                # during read_message processing.
                hs_ref = self._conn.noise_protocol.handshake_state

                # msg3: ← s, se  (initiator's static key arrives here; triggers split)
                self._conn.read_message(await _recv_hs(reader))

                self._remote_static = _extract_rs(hs_ref)

        except HandshakeError:
            raise
        except Exception as exc:
            raise HandshakeError(f"Noise handshake failed: {exc}") from exc

        if self._remote_static is None:
            raise HandshakeError("Handshake completed but remote static key was not captured")

        return self._remote_static

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt CBOR plaintext. Returns ciphertext (plaintext length + 16-byte tag)."""
        return bytes(self._conn.encrypt(plaintext))

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt ciphertext. Raises on authentication failure."""
        return bytes(self._conn.decrypt(ciphertext))

    @property
    def remote_static_public(self) -> Optional[bytes]:
        """The remote peer's X25519 static public key bytes, set after handshake."""
        return self._remote_static


# ---------------------------------------------------------------------------
# wire helpers
# ---------------------------------------------------------------------------

async def _send_hs(writer: asyncio.StreamWriter, payload: bytes) -> None:
    """Frame and send a Noise handshake message: [4-byte length][0x00][payload]."""
    frame = struct.pack("!I", len(payload) + 1) + _HANDSHAKE_BYTE + payload
    writer.write(frame)
    await writer.drain()


async def _recv_hs(reader: asyncio.StreamReader) -> bytes:
    """Read a framed Noise handshake message; return the raw Noise payload."""
    header = await reader.readexactly(4)
    length = struct.unpack("!I", header)[0]
    if length == 0:
        raise HandshakeError("Empty handshake frame")
    envelope = await reader.readexactly(length)
    if envelope[0:1] != _HANDSHAKE_BYTE:
        raise HandshakeError(
            f"Expected handshake frame (0x00), got 0x{envelope[0]:02x} — "
            "peer may be running a plaintext-only version of Aries Mesh"
        )
    return envelope[1:]


def _extract_rs(hs) -> Optional[bytes]:
    """Pull the remote static public key bytes out of a HandshakeState."""
    if hs is None or getattr(hs, "rs", None) is None:
        return None
    try:
        return hs.rs.public.public_bytes(Encoding.Raw, PublicFormat.Raw)
    except Exception:
        return None
