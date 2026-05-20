"""Adversarial tests — one per hardening fix (v0.1.1).

Pins behavior on the nasty paths the v0.1 happy-path coverage missed:
- Continuation tamper resistance (Fix 1)
- Handoff without explicit target raises (Fix 2)
- Encrypted key file requires the correct passphrase (Fix 3)
- Revoked device cannot chain-validate downstream agent tokens (issue #8 sample)
- Capability `/*` glob has correct prefix semantics (Fix 5)
"""
from __future__ import annotations

import tempfile

import pytest
from nacl.exceptions import CryptoError

from aries.adapters.base import Message
from aries.continuation import HandoffReason, build_continuation
from aries.identity.did import public_key_to_did
from aries.identity.keys import KeyPair, load_keypair, save_keypair
from aries.identity.ucan import (
    Capability,
    UCANStore,
    build_agent_token,
    build_household_membership,
)
from aries.node import AriesNode


# ---------------------------------------------------------------------------
# Fix 1: Continuation tamper resistance
# ---------------------------------------------------------------------------

def test_continuation_tamper_detected() -> None:
    """Signed continuation; mutate a non-content field; verify() must fail."""
    sender = KeyPair.generate()
    sender_did = public_key_to_did(sender.public_bytes)
    cont = build_continuation(
        task_id="t1",
        source_device_did=sender_did,
        source_agent_did=sender_did,
        messages=[Message(role="user", content="please run task")],
        reason=HandoffReason(code="privacy_upgrade", description="moving to local"),
        required_capabilities=["text.qa"],
        max_cost_class="free",
    )
    cont.sign(sender, sender_did)
    assert cont.verify() is True

    # Tamper: bump the cost ceiling from free -> paid AFTER signing
    cont.max_cost_class = "paid"
    assert cont.verify() is False, "mutation outside the narrow content_hash must invalidate the envelope"

    # Tamper a different field
    cont.max_cost_class = "free"  # restore
    assert cont.verify() is True
    cont.required_capabilities.append("code.generate")
    assert cont.verify() is False


# ---------------------------------------------------------------------------
# Fix 2: Handoff requires explicit target
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handoff_without_target_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        node = AriesNode(data_dir=tmp)
        await node.initialize("solo", "linux")
        await node.start(enable_discovery=False, enable_profiler=False, enable_api=False)
        try:
            with pytest.raises(ValueError, match="target_device_did"):
                # The type hint says str; pass empty to simulate the v0.1 default behavior
                await node.handoff(
                    task_id="t1",
                    reason=HandoffReason(code="user_request", description="no target"),
                    target_device_did="",  # type: ignore[arg-type]
                )
        finally:
            await node.stop()


# ---------------------------------------------------------------------------
# Fix 3: Real key encryption roundtrip
# ---------------------------------------------------------------------------

def test_save_keypair_encrypted_roundtrip(tmp_path) -> None:
    kp = KeyPair.generate()
    path = tmp_path / "device_key.json"
    save_keypair(kp, path, passphrase="correct horse battery staple")
    # Right passphrase: roundtrip
    revived = load_keypair(path, passphrase="correct horse battery staple")
    assert revived.public_bytes == kp.public_bytes
    assert revived.secret_bytes == kp.secret_bytes
    # Wrong passphrase: NaCl raises CryptoError
    with pytest.raises(CryptoError):
        load_keypair(path, passphrase="wrong")
    # Missing passphrase on a v2 file: explicit ValueError
    with pytest.raises(ValueError, match="passphrase"):
        load_keypair(path)


# ---------------------------------------------------------------------------
# Issue #8 sample: revoked device cannot chain-validate downstream tokens
# ---------------------------------------------------------------------------

def test_revoked_device_cannot_chain_validate() -> None:
    root = KeyPair.generate()
    device = KeyPair.generate()
    agent = KeyPair.generate()
    root_did = public_key_to_did(root.public_bytes)
    device_did = public_key_to_did(device.public_bytes)
    agent_did = public_key_to_did(agent.public_bytes)

    store = UCANStore()
    membership = build_household_membership(root.signing_key, root_did, device_did)
    membership_cid = store.store(membership)
    agent_token = build_agent_token(
        device.signing_key,
        device_did,
        agent_did,
        capabilities=[Capability("*", "aries/agent.invoke")],
        parent_proof_cid=membership_cid,
    )
    store.store(agent_token)

    # Sanity: chain validates today
    assert store.validate_chain(agent_token, expected_root_did=root_did) is True

    # Now revoke the device. The downstream agent token's signature is still good,
    # but the chain walks through the revoked device — validation must raise.
    with pytest.raises(ValueError, match="revoked"):
        store.validate_chain(
            agent_token,
            expected_root_did=root_did,
            revocation_list=[device_did],
        )


# ---------------------------------------------------------------------------
# Fix 5: Capability /* wildcard glob semantics
# ---------------------------------------------------------------------------

def test_capability_wildcard_glob() -> None:
    parent_abc = Capability("aries:context://tasks/abc/*", "aries/context.read")
    child_abc_history = Capability("aries:context://tasks/abc/history", "aries/context.read")
    child_abc_root = Capability("aries:context://tasks/abc", "aries/context.read")
    child_xyz_history = Capability("aries:context://tasks/xyz/history", "aries/context.read")
    child_abc_write = Capability("aries:context://tasks/abc/history", "aries/context.write")

    # Glob covers anything under abc/, and the bare abc resource itself
    assert child_abc_history.is_attenuated_by(parent_abc) is True
    assert child_abc_root.is_attenuated_by(parent_abc) is True
    # Different task path is NOT covered
    assert child_xyz_history.is_attenuated_by(parent_abc) is False
    # Different ability is NOT covered even with matching resource
    assert child_abc_write.is_attenuated_by(parent_abc) is False

    # The namespace-wide glob covers everything in that namespace
    ns_glob = Capability("aries:context://*", "aries/context.read")
    assert child_abc_history.is_attenuated_by(ns_glob) is True
    assert child_xyz_history.is_attenuated_by(ns_glob) is True
