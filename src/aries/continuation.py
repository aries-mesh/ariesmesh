"""Continuation envelope for cross-device task hand-off.

Spec reference: §16.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import cbor2

from .adapters.base import Message
from .identity.did import did_to_public_key
from .identity.keys import KeyPair, verify_detached
from .util import canonical_json, content_hash as _content_hash


# ---------------------------------------------------------------------------
# Resource / HandoffReason
# ---------------------------------------------------------------------------

@dataclass
class Resource:
    type: str  # "file" | "embedding" | "tool_output" | "memory_ref"
    uri: str
    content: Optional[str] = None
    hash: Optional[str] = None
    mime_type: str = "application/octet-stream"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Resource":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class HandoffReason:
    code: str   # user_request | privacy_upgrade | capability_need | battery_low | cost_limit | model_switch
    description: str = ""
    user_initiated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HandoffReason":
        return cls(**d)


# ---------------------------------------------------------------------------
# Continuation
# ---------------------------------------------------------------------------

@dataclass
class Continuation:
    id: str = field(default_factory=lambda: "cont_" + uuid.uuid4().hex[:12])
    task_id: str = ""
    thread_id: Optional[str] = None
    source_device_did: str = ""
    source_agent_did: str = ""
    target_device_did: Optional[str] = None
    target_agent_did: Optional[str] = None
    system_prompt: Optional[str] = None
    messages: list[Message] = field(default_factory=list)
    summary: Optional[str] = None
    resources: list[Resource] = field(default_factory=list)
    memory_keys: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    ucan_chain: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    locality_preference: str = "any"
    max_cost_class: str = "paid"
    reason: Optional[HandoffReason] = None
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    signature: Optional[str] = None   # hex-encoded Ed25519 over canonical_json(_signable_content())
    signed_by: Optional[str] = None   # did:key of the signer (source device)

    # ---------- signing ----------

    def _signable_content(self) -> dict[str, Any]:
        d = self.to_dict()
        d.pop("signature", None)
        d.pop("signed_by", None)
        return d

    @property
    def content_hash(self) -> str:
        """Hash that covers every field except the signature itself."""
        return _content_hash(self._signable_content())

    def sign(self, keypair: KeyPair, signer_did: str) -> "Continuation":
        self.signed_by = signer_did
        self.signature = keypair.sign(canonical_json(self._signable_content())).hex()
        return self

    def verify(self) -> bool:
        if not self.signature or not self.signed_by:
            return False
        try:
            pub = did_to_public_key(self.signed_by)
        except ValueError:
            return False
        return verify_detached(
            pub,
            canonical_json(self._signable_content()),
            bytes.fromhex(self.signature),
        )

    # ----- serialization -----

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "source_device_did": self.source_device_did,
            "source_agent_did": self.source_agent_did,
            "target_device_did": self.target_device_did,
            "target_agent_did": self.target_agent_did,
            "system_prompt": self.system_prompt,
            "messages": [m.to_dict() for m in self.messages],
            "summary": self.summary,
            "resources": [r.to_dict() for r in self.resources],
            "memory_keys": list(self.memory_keys),
            "metadata": dict(self.metadata),
            "ucan_chain": list(self.ucan_chain),
            "required_capabilities": list(self.required_capabilities),
            "locality_preference": self.locality_preference,
            "max_cost_class": self.max_cost_class,
            "reason": self.reason.to_dict() if self.reason else None,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "signature": self.signature,
            "signed_by": self.signed_by,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Continuation":
        return cls(
            id=d.get("id", "cont_" + uuid.uuid4().hex[:12]),
            task_id=d.get("task_id", ""),
            thread_id=d.get("thread_id"),
            source_device_did=d.get("source_device_did", ""),
            source_agent_did=d.get("source_agent_did", ""),
            target_device_did=d.get("target_device_did"),
            target_agent_did=d.get("target_agent_did"),
            system_prompt=d.get("system_prompt"),
            messages=[Message.from_dict(m) for m in d.get("messages", [])],
            summary=d.get("summary"),
            resources=[Resource.from_dict(r) for r in d.get("resources", [])],
            memory_keys=list(d.get("memory_keys", [])),
            metadata=dict(d.get("metadata", {})),
            ucan_chain=list(d.get("ucan_chain", [])),
            required_capabilities=list(d.get("required_capabilities", [])),
            locality_preference=d.get("locality_preference", "any"),
            max_cost_class=d.get("max_cost_class", "paid"),
            reason=HandoffReason.from_dict(d["reason"]) if d.get("reason") else None,
            created_at=d.get("created_at", time.time()),
            expires_at=d.get("expires_at"),
            signature=d.get("signature"),
            signed_by=d.get("signed_by"),
        )

    def to_cbor(self) -> bytes:
        return cbor2.dumps(self.to_dict())

    @classmethod
    def from_cbor(cls, data: bytes) -> "Continuation":
        return cls.from_dict(cbor2.loads(data))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_continuation(
    task_id: str,
    source_device_did: str,
    source_agent_did: str,
    messages: list[Message],
    *,
    reason: Optional[HandoffReason] = None,
    target_device_did: Optional[str] = None,
    required_capabilities: Optional[list[str]] = None,
    locality_preference: str = "any",
    max_cost_class: str = "paid",
    system_prompt: Optional[str] = None,
    resources: Optional[list[Resource]] = None,
    memory_keys: Optional[list[str]] = None,
    ucan_chain: Optional[list[str]] = None,
    ttl_seconds: float = 300.0,
    metadata: Optional[dict[str, Any]] = None,
) -> Continuation:
    return Continuation(
        task_id=task_id,
        source_device_did=source_device_did,
        source_agent_did=source_agent_did,
        target_device_did=target_device_did,
        system_prompt=system_prompt,
        messages=list(messages),
        resources=list(resources or []),
        memory_keys=list(memory_keys or []),
        ucan_chain=list(ucan_chain or []),
        required_capabilities=list(required_capabilities or []),
        locality_preference=locality_preference,
        max_cost_class=max_cost_class,
        reason=reason,
        metadata=dict(metadata or {}),
        expires_at=time.time() + ttl_seconds,
    )
