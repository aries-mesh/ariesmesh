"""Unit tests for the identity layer (keys, did, fingerprint, Shamir)."""
from __future__ import annotations

import secrets

from aries.identity.did import did_short, did_to_public_key, public_key_to_did
from aries.identity.keys import (
    KeyPair,
    fingerprint,
    shamir_reconstruct,
    shamir_split,
    verify_detached,
)


def test_keypair_public_bytes_length() -> None:
    kp = KeyPair.generate()
    assert len(kp.public_bytes) == 32
    assert len(kp.secret_bytes) == 32


def test_sign_verify_roundtrip() -> None:
    kp = KeyPair.generate()
    msg = b"hello aries"
    sig = kp.sign(msg)
    assert kp.verify(msg, sig)


def test_sign_with_wrong_key_fails() -> None:
    a = KeyPair.generate()
    b = KeyPair.generate()
    msg = b"hello aries"
    sig = a.sign(msg)
    assert b.verify(msg, sig) is False


def test_verify_detached_helper() -> None:
    kp = KeyPair.generate()
    msg = b"detached"
    sig = kp.sign(msg)
    assert verify_detached(kp.public_bytes, msg, sig)
    assert verify_detached(KeyPair.generate().public_bytes, msg, sig) is False


def test_shamir_2of3_reconstruction() -> None:
    secret = secrets.token_bytes(32)
    shares = shamir_split(secret, n=3, k=2)
    assert len(shares) == 3
    # any 2 of 3 should reconstruct
    assert shamir_reconstruct(shares[:2]) == secret
    assert shamir_reconstruct([shares[0], shares[2]]) == secret
    assert shamir_reconstruct([shares[1], shares[2]]) == secret


def test_shamir_all_three_reconstructs() -> None:
    secret = b"\x00\x01\x02test\xff"
    shares = shamir_split(secret, n=3, k=2)
    assert shamir_reconstruct(shares) == secret


def test_did_key_roundtrip() -> None:
    kp = KeyPair.generate()
    did = public_key_to_did(kp.public_bytes)
    assert did.startswith("did:key:z6Mk")
    decoded = did_to_public_key(did)
    assert decoded == kp.public_bytes


def test_did_short_display() -> None:
    kp = KeyPair.generate()
    did = public_key_to_did(kp.public_bytes)
    short = did_short(did)
    assert "..." in short


def test_fingerprint_deterministic() -> None:
    kp = KeyPair.generate()
    fp_a = fingerprint(kp.public_bytes)
    fp_b = fingerprint(kp.public_bytes)
    assert fp_a == fp_b
    assert len(fp_a.split()) == 6
    other = fingerprint(KeyPair.generate().public_bytes)
    assert other != fp_a
