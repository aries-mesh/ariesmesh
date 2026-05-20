"""Tests for memory-store UCAN ACL enforcement (Feature 5 / Phase 4)."""
from __future__ import annotations

import time

import pytest

from aries.identity.did import public_key_to_did
from aries.identity.keys import KeyPair
from aries.identity.ucan import Capability, UCANToken
from aries.memory.store import MemoryStore


def _make_token(
    capabilities: list[Capability],
    *,
    issuer_kp: KeyPair | None = None,
    audience: str = "did:key:z6Mk-agent-audience",
) -> tuple[str, str]:
    """Build and sign a UCAN token; return (jwt_string, audience)."""
    kp = issuer_kp or KeyPair.generate()
    issuer_did = public_key_to_did(kp.public_bytes)
    token = UCANToken(
        issuer=issuer_did,
        audience=audience,
        capabilities=capabilities,
        expiration=time.time() + 3600,
    )
    jwt = token.sign(kp.signing_key)
    return jwt, audience


# ---------------------------------------------------------------------------
# Test 8 — valid UCAN write succeeds
# ---------------------------------------------------------------------------


def test_memory_set_with_valid_ucan_succeeds() -> None:
    store = MemoryStore(device_did="did:key:z6Mk-store")
    jwt, _ = _make_token(
        [Capability(resource="aries:context://*", ability="aries/context.write")]
    )

    store.set("context://tasks/abc/response", "ok", ucan_token=jwt)
    # Value should be readable (legacy or canonical key works).
    assert store.get("context://tasks/abc/response") == "ok"
    assert store.get("aries:context://tasks/abc/response") == "ok"


# ---------------------------------------------------------------------------
# Test 9 — UCAN scoped to one task path can't write to a sibling task
# ---------------------------------------------------------------------------


def test_memory_set_with_wrong_namespace_raises() -> None:
    store = MemoryStore(device_did="did:key:z6Mk-store")
    jwt, _ = _make_token(
        [
            Capability(
                resource="aries:context://tasks/abc/*",
                ability="aries/context.write",
            )
        ]
    )

    with pytest.raises(PermissionError) as exc_info:
        store.set("context://tasks/xyz/response", "leak", ucan_token=jwt)

    msg = str(exc_info.value)
    assert "aries:context://tasks/xyz/response" in msg
    assert "aries/context.write" in msg


# ---------------------------------------------------------------------------
# Test 10 — read-only UCAN cannot write
# ---------------------------------------------------------------------------


def test_memory_set_with_wrong_ability_raises() -> None:
    store = MemoryStore(device_did="did:key:z6Mk-store")
    jwt, _ = _make_token(
        [Capability(resource="aries:context://*", ability="aries/context.read")]
    )

    with pytest.raises(PermissionError):
        store.set("context://tasks/abc/response", "denied", ucan_token=jwt)


# ---------------------------------------------------------------------------
# Test 11 — writes without a UCAN remain unrestricted (backward compat)
# ---------------------------------------------------------------------------


def test_memory_set_without_ucan_succeeds() -> None:
    store = MemoryStore(device_did="did:key:z6Mk-store")
    # No ucan_token argument — internal node infrastructure path.
    store.set("context://tasks/abc/receipt", {"ok": True})
    assert store.get("context://tasks/abc/receipt") == {"ok": True}


# ---------------------------------------------------------------------------
# Test 12 — log_append respects scoped UCAN tokens
# ---------------------------------------------------------------------------


def test_log_append_with_scoped_ucan() -> None:
    store = MemoryStore(device_did="did:key:z6Mk-store")
    jwt, _ = _make_token(
        [
            Capability(
                resource="aries:context://tasks/abc/*",
                ability="aries/context.write",
            )
        ]
    )

    # In-scope append succeeds
    idx = store.log_append(
        "context://tasks/abc/history",
        {"role": "user", "content": "hello"},
        ucan_token=jwt,
    )
    assert idx == 0

    # Out-of-scope append raises
    with pytest.raises(PermissionError):
        store.log_append(
            "context://tasks/xyz/history",
            {"role": "user", "content": "leak"},
            ucan_token=jwt,
        )
