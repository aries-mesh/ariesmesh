"""Unit tests for UCAN tokens, capability attenuation, and chain validation."""
from __future__ import annotations

import time

import pytest

from aries.identity.did import public_key_to_did
from aries.identity.keys import KeyPair
from aries.identity.ucan import (
    Capability,
    UCANStore,
    UCANToken,
    build_agent_token,
    build_household_membership,
)


def test_build_sign_and_verify_token() -> None:
    issuer_kp = KeyPair.generate()
    audience_kp = KeyPair.generate()
    issuer_did = public_key_to_did(issuer_kp.public_bytes)
    audience_did = public_key_to_did(audience_kp.public_bytes)
    token = UCANToken(
        issuer=issuer_did,
        audience=audience_did,
        capabilities=[Capability("*", "aries/agent.invoke")],
    )
    jwt = token.sign(issuer_kp.signing_key)
    verified = UCANToken.verify(jwt)
    assert verified.issuer == issuer_did


def test_decode_skips_signature() -> None:
    kp = KeyPair.generate()
    did = public_key_to_did(kp.public_bytes)
    token = UCANToken(issuer=did, audience=did, capabilities=[Capability("*", "aries/agent.invoke")])
    jwt = token.sign(kp.signing_key)
    decoded = UCANToken.decode(jwt)
    assert decoded.issuer == did


def test_expired_flag() -> None:
    kp = KeyPair.generate()
    did = public_key_to_did(kp.public_bytes)
    token = UCANToken(
        issuer=did,
        audience=did,
        capabilities=[Capability("*", "aries/agent.invoke")],
        expiration=time.time() - 60,
    )
    assert token.is_expired
    assert not token.is_active


def test_capability_attenuation_match() -> None:
    parent = Capability("aries:context", "aries/context.read")
    child = Capability("aries:context/tasks/abc", "aries/context.read")
    assert child.is_attenuated_by(parent)
    assert child.is_attenuated_by(Capability("*", "aries/context.read"))
    assert child.is_attenuated_by(child)


def test_capability_different_ability_rejected() -> None:
    parent = Capability("*", "aries/agent.invoke")
    child = Capability("*", "aries/context.write")
    assert not child.is_attenuated_by(parent)


def test_chain_validation_root_token() -> None:
    root = KeyPair.generate()
    device = KeyPair.generate()
    root_did = public_key_to_did(root.public_bytes)
    device_did = public_key_to_did(device.public_bytes)

    store = UCANStore()
    jwt = build_household_membership(root.signing_key, root_did, device_did)
    store.store(jwt)
    assert store.validate_chain(jwt, expected_root_did=root_did) is True


def test_chain_validation_two_level() -> None:
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

    assert store.validate_chain(agent_token, expected_root_did=root_did) is True
    assert store.validate_chain(
        agent_token,
        expected_root_did=root_did,
        required_capability=Capability("aries:agent://x", "aries/agent.invoke"),
    ) is True


def test_chain_validation_revoked_issuer_raises() -> None:
    root = KeyPair.generate()
    device = KeyPair.generate()
    root_did = public_key_to_did(root.public_bytes)
    device_did = public_key_to_did(device.public_bytes)
    store = UCANStore()
    jwt = build_household_membership(root.signing_key, root_did, device_did)
    store.store(jwt)
    with pytest.raises(ValueError, match="revoked"):
        store.validate_chain(jwt, expected_root_did=root_did, revocation_list=[root_did])


def test_chain_validation_broken_linkage_raises() -> None:
    root = KeyPair.generate()
    device = KeyPair.generate()
    agent = KeyPair.generate()
    rogue = KeyPair.generate()
    root_did = public_key_to_did(root.public_bytes)
    device_did = public_key_to_did(device.public_bytes)
    agent_did = public_key_to_did(agent.public_bytes)
    rogue_did = public_key_to_did(rogue.public_bytes)

    store = UCANStore()
    # membership goes to a DIFFERENT device DID than the one issuing the agent token
    membership = build_household_membership(root.signing_key, root_did, rogue_did)
    cid = store.store(membership)
    agent_token = build_agent_token(
        device.signing_key,
        device_did,
        agent_did,
        capabilities=[Capability("*", "aries/agent.invoke")],
        parent_proof_cid=cid,
    )
    store.store(agent_token)
    with pytest.raises(ValueError, match="audience"):
        store.validate_chain(agent_token, expected_root_did=root_did)
