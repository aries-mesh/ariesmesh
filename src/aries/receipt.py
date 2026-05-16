"""Signed execution receipts forming hash-linked audit chains.

Spec reference: §17.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .identity.did import did_to_public_key
from .identity.keys import KeyPair, verify_detached
from .util import canonical_json


VALID_ACTIONS = {"invoke", "handoff_sent", "handoff_received", "completed"}
VALID_STATUSES = {"success", "error", "partial"}


@dataclass
class Receipt:
    task_id: str
    device_did: str
    agent_did: str
    action: str           # invoke | handoff_sent | handoff_received | completed
    status: str = "success"
    model_used: str = ""
    tokens_used: int = 0
    latency_ms: float = 0.0
    input_hash: str = ""
    output_hash: str = ""
    summary: str = ""
    continuation_id: Optional[str] = None
    previous_receipt_id: Optional[str] = None
    previous_receipt_hash: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    completed_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: "rcpt_" + uuid.uuid4().hex[:12])
    signature: Optional[str] = None
    signed_by: Optional[str] = None

    # ---------- hashing / signing ----------

    def _signable_content(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("signature", None)
        d.pop("signed_by", None)
        return d

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(canonical_json(self._signable_content())).hexdigest()

    def sign(self, keypair: KeyPair, signer_did: str) -> "Receipt":
        self.signed_by = signer_did
        payload = canonical_json(self._signable_content())
        self.signature = keypair.sign(payload).hex()
        return self

    def verify(self) -> bool:
        if not self.signature or not self.signed_by:
            return False
        try:
            pub = did_to_public_key(self.signed_by)
        except ValueError:
            return False
        return verify_detached(pub, canonical_json(self._signable_content()), bytes.fromhex(self.signature))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Receipt":
        return cls(**d)


class ReceiptChain:
    def __init__(self) -> None:
        self.receipts: list[Receipt] = []

    def add(
        self,
        receipt: Receipt,
        keypair: Optional[KeyPair] = None,
        signer_did: Optional[str] = None,
    ) -> Receipt:
        """Link receipt to the previous one and (optionally) sign atomically.

        If `keypair`/`signer_did` are provided, the receipt is signed AFTER its
        previous_receipt_* fields are set, so the signature covers the linkage.
        Otherwise the caller must sign before calling and the chain links by
        copying the prior receipt's id/hash WITHOUT re-signing — verify_chain
        will reject this. Prefer the signed form.
        """
        if self.receipts:
            prev = self.receipts[-1]
            receipt.previous_receipt_id = prev.id
            receipt.previous_receipt_hash = prev.content_hash
        if keypair is not None and signer_did is not None:
            receipt.sign(keypair, signer_did)
        self.receipts.append(receipt)
        return receipt

    def __len__(self) -> int:
        return len(self.receipts)

    def __iter__(self):
        return iter(self.receipts)

    def verify_chain(self) -> bool:
        prev: Optional[Receipt] = None
        for r in self.receipts:
            if not r.verify():
                return False
            if prev is not None:
                if r.previous_receipt_id != prev.id:
                    return False
                if r.previous_receipt_hash != prev.content_hash:
                    return False
            prev = r
        return True
