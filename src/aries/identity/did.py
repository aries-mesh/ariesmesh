"""did:key encoding/decoding per W3C did:key method, Ed25519 only.

Spec reference: §5.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .keys import KeyPair


_ED25519_MULTICODEC = bytes([0xED, 0x01])
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big") if data else 0
    out = bytearray()
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(_B58_ALPHABET[rem])
    # preserve leading zero bytes as leading '1's
    for byte in data:
        if byte == 0:
            out.append(_B58_ALPHABET[0])
        else:
            break
    out.reverse()
    return out.decode("ascii")


def _b58decode(s: str) -> bytes:
    if not s:
        return b""
    n = 0
    for ch in s:
        idx = _B58_ALPHABET.find(ch.encode("ascii"))
        if idx < 0:
            raise ValueError(f"Invalid base58 character: {ch!r}")
        n = n * 58 + idx
    # restore leading zero bytes from leading '1's
    leading = 0
    for ch in s:
        if ch == "1":
            leading += 1
        else:
            break
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * leading + body


def public_key_to_did(public_bytes: bytes) -> str:
    if len(public_bytes) != 32:
        raise ValueError(f"Ed25519 public key must be 32 bytes, got {len(public_bytes)}")
    multicodec_key = _ED25519_MULTICODEC + public_bytes  # 34 bytes
    return "did:key:z" + _b58encode(multicodec_key)


def did_to_public_key(did: str) -> bytes:
    prefix = "did:key:z"
    if not did.startswith(prefix):
        raise ValueError(f"DID must start with {prefix!r}: {did!r}")
    encoded = did[len(prefix):]
    decoded = _b58decode(encoded)
    if decoded[:2] != _ED25519_MULTICODEC:
        raise ValueError(f"DID multicodec is not Ed25519 (0xED01): {decoded[:2].hex()}")
    pk = decoded[2:]
    if len(pk) != 32:
        raise ValueError(f"Decoded public key length {len(pk)} != 32")
    return pk


def did_from_keypair(keypair: "KeyPair") -> str:
    return public_key_to_did(keypair.public_bytes)


def did_short(did: str, chars: int = 8) -> str:
    return f"{did[:16]}...{did[-chars:]}"
