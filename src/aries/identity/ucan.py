"""UCAN 1.0 implementation: JWT-based capability tokens with EdDSA signatures.

Spec reference: §6.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from .did import did_to_public_key


# ---------------------------------------------------------------------------
# base64url helpers
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ---------------------------------------------------------------------------
# Capability model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Capability:
    resource: str  # "with": DID, aries:// URI, or "*"
    ability: str   # "can": e.g. "aries/agent.invoke"

    def is_attenuated_by(self, parent: "Capability") -> bool:
        """True if `self` is no broader than `parent`.

        Resource matching rules (in order of precedence):
          1. `parent.resource == "*"`  → matches everything.
          2. Exact string equality.
          3. Glob: `parent.resource` ending in `/*` covers any child whose path
             begins with the parent prefix (without the `*`).
          4. Path-prefix: child resource starts with `parent.rstrip("/") + "/"`.
        """
        if self.ability != parent.ability:
            return False
        if parent.resource == "*":
            return True
        if self.resource == parent.resource:
            return True
        if parent.resource.endswith("/*"):
            prefix = parent.resource[:-2]  # drop the "/*"
            return self.resource == prefix or self.resource.startswith(prefix + "/")
        return self.resource.startswith(parent.resource.rstrip("/") + "/")

    def to_dict(self) -> dict[str, str]:
        return {"with": self.resource, "can": self.ability}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Capability":
        return cls(resource=d["with"], ability=d["can"])


# Seven canonical abilities
ABILITIES = {
    "aries/agent.invoke",
    "aries/context.read",
    "aries/context.write",
    "aries/handoff.send",
    "aries/handoff.accept",
    "aries/identity.delegate",
    "aries/household.member",
}


@dataclass(frozen=True)
class Caveat:
    conditions: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.conditions)


# ---------------------------------------------------------------------------
# UCANToken
# ---------------------------------------------------------------------------

@dataclass
class UCANToken:
    issuer: str
    audience: str
    capabilities: list[Capability]
    caveats: list[Caveat] = field(default_factory=list)
    proofs: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    not_before: float = field(default_factory=time.time)
    expiration: float = 0.0
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    _raw_token: Optional[str] = None
    _signature: Optional[bytes] = None

    @property
    def is_expired(self) -> bool:
        return self.expiration > 0 and time.time() > self.expiration

    @property
    def is_active(self) -> bool:
        return time.time() >= self.not_before and not self.is_expired

    @property
    def cid(self) -> str:
        if not self._raw_token:
            raise ValueError("Token not yet signed; CID undefined")
        return "ucan:" + hashlib.sha256(self._raw_token.encode("ascii")).hexdigest()[:32]

    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.audience,
            "nbf": self.not_before,
            "nnc": self.nonce,
            "cap": [c.to_dict() for c in self.capabilities],
        }
        if self.expiration:
            payload["exp"] = self.expiration
        if self.caveats:
            payload["cav"] = [c.to_dict() for c in self.caveats]
        if self.proofs:
            payload["prf"] = list(self.proofs)
        if self.facts:
            payload["fct"] = dict(self.facts)
        return payload

    def sign(self, signing_key: SigningKey) -> str:
        header = {"alg": "EdDSA", "typ": "JWT", "ucv": "1.0"}
        header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        payload_b64 = _b64url(json.dumps(self._payload(), separators=(",", ":")).encode("utf-8"))
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        sig = signing_key.sign(signing_input).signature
        token = f"{header_b64}.{payload_b64}.{_b64url(sig)}"
        self._raw_token = token
        self._signature = sig
        return token

    @classmethod
    def decode(cls, token: str) -> "UCANToken":
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("UCAN JWT must have 3 parts")
        header = json.loads(_b64url_decode(parts[0]))
        if header.get("ucv") != "1.0":
            raise ValueError(f"Unsupported UCAN version: {header.get('ucv')!r}")
        payload = json.loads(_b64url_decode(parts[1]))
        sig = _b64url_decode(parts[2])

        ucan = cls(
            issuer=payload["iss"],
            audience=payload["aud"],
            capabilities=[Capability.from_dict(c) for c in payload.get("cap", [])],
            caveats=[Caveat(c) for c in payload.get("cav", [])],
            proofs=list(payload.get("prf", [])),
            facts=dict(payload.get("fct", {})),
            not_before=float(payload.get("nbf", time.time())),
            expiration=float(payload.get("exp", 0.0)),
            nonce=payload.get("nnc", ""),
        )
        ucan._raw_token = token
        ucan._signature = sig
        return ucan

    @classmethod
    def verify(cls, token: str) -> "UCANToken":
        ucan = cls.decode(token)
        parts = token.split(".")
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        try:
            VerifyKey(did_to_public_key(ucan.issuer)).verify(signing_input, ucan._signature or b"")
        except BadSignatureError:
            raise ValueError("UCAN signature verification failed")
        return ucan


# ---------------------------------------------------------------------------
# UCANStore — CID-indexed token cache + chain validator
# ---------------------------------------------------------------------------

class UCANStore:
    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def store(self, token_str: str) -> str:
        ucan = UCANToken.decode(token_str)
        cid = ucan.cid
        self._tokens[cid] = token_str
        return cid

    def get(self, cid: str) -> Optional[str]:
        return self._tokens.get(cid)

    def has(self, cid: str) -> bool:
        return cid in self._tokens

    def all_tokens(self) -> dict[str, str]:
        return dict(self._tokens)

    def validate_chain(
        self,
        token_str: str,
        expected_root_did: str,
        required_capability: Optional[Capability] = None,
        revocation_list: Optional[list[str]] = None,
    ) -> bool:
        """Recursively walk the proof chain back to `expected_root_did`.

        Raises ValueError on any failure (signature, expiry, revocation, linkage).
        """
        revocation_list = revocation_list or []
        return self._validate_chain(token_str, expected_root_did, required_capability, revocation_list)

    def _validate_chain(
        self,
        token_str: str,
        expected_root_did: str,
        required_capability: Optional[Capability],
        revocation_list: list[str],
        depth: int = 0,
    ) -> bool:
        if depth > 16:
            raise ValueError("UCAN chain too deep (>16)")
        ucan = UCANToken.verify(token_str)

        if not ucan.is_active:
            if ucan.is_expired:
                raise ValueError(f"UCAN expired at {ucan.expiration}")
            raise ValueError(f"UCAN not yet active (nbf={ucan.not_before})")

        if ucan.issuer in revocation_list:
            raise ValueError(f"Issuer {ucan.issuer} is revoked")
        if ucan.audience in revocation_list:
            raise ValueError(f"Audience {ucan.audience} is revoked")

        if required_capability is not None:
            if not any(required_capability.is_attenuated_by(c) for c in ucan.capabilities):
                raise ValueError(
                    f"Required capability {required_capability.ability} on {required_capability.resource} "
                    "not satisfied by this token"
                )

        if not ucan.proofs:
            if ucan.issuer != expected_root_did:
                raise ValueError(
                    f"Chain terminus issuer {ucan.issuer} != expected root {expected_root_did}"
                )
            return True

        for proof_cid in ucan.proofs:
            proof_str = self._tokens.get(proof_cid)
            if proof_str is None:
                raise ValueError(f"Proof {proof_cid} not found in store")
            proof = UCANToken.decode(proof_str)
            if proof.audience != ucan.issuer:
                raise ValueError(
                    f"Proof audience {proof.audience} != token issuer {ucan.issuer}"
                )
            for cap in ucan.capabilities:
                if not any(cap.is_attenuated_by(pc) for pc in proof.capabilities):
                    raise ValueError(
                        f"Capability {cap.ability}:{cap.resource} not attenuated by parent {proof_cid}"
                    )
            self._validate_chain(proof_str, expected_root_did, None, revocation_list, depth + 1)

        return True


# ---------------------------------------------------------------------------
# Builder functions
# ---------------------------------------------------------------------------

def build_household_membership(
    user_root_key: SigningKey,
    user_root_did: str,
    device_did: str,
) -> str:
    """Issue a no-expiration UCAN from root to a device with full household capabilities."""
    caps = [
        Capability(resource="*", ability=ability)
        for ability in (
            "aries/household.member",
            "aries/identity.delegate",
            "aries/context.read",
            "aries/context.write",
            "aries/agent.invoke",
            "aries/handoff.send",
            "aries/handoff.accept",
        )
    ]
    token = UCANToken(
        issuer=user_root_did,
        audience=device_did,
        capabilities=caps,
        facts={"household": user_root_did, "role": "device"},
        expiration=0.0,
    )
    return token.sign(user_root_key)


def build_agent_token(
    device_key: SigningKey,
    device_did: str,
    agent_did: str,
    capabilities: list[Capability],
    ttl_seconds: int = 86400,
    parent_proof_cid: Optional[str] = None,
    caveats: Optional[list[Caveat]] = None,
) -> str:
    """Issue a time-scoped UCAN from device to agent. parent_proof_cid links to membership UCAN."""
    now = time.time()
    proofs = [parent_proof_cid] if parent_proof_cid else []
    token = UCANToken(
        issuer=device_did,
        audience=agent_did,
        capabilities=list(capabilities),
        caveats=list(caveats) if caveats else [],
        proofs=proofs,
        not_before=now,
        expiration=now + ttl_seconds,
    )
    return token.sign(device_key)
