"""Household state management: first-device init, agent registration, pairing, revocation.

Spec reference: §7 plus pairing extensions per plan.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .did import public_key_to_did
from .keys import KeyPair, load_keypair, save_keypair, shamir_split
from ._wordlist import BIP39_256
from .ucan import (
    Capability,
    UCANStore,
    UCANToken,
    build_agent_token,
    build_household_membership,
)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class DeviceRecord:
    device_did: str
    name: str
    platform: str
    paired_at: float
    membership_ucan: str
    is_self: bool = False


@dataclass
class AgentRecord:
    agent_did: str
    name: str
    vendor: str
    model: Optional[str] = None
    capabilities: list[str] = field(default_factory=list)
    context_window: int = 0
    locality: str = "local"
    cost_class: str = "free"
    registered_at: float = field(default_factory=time.time)
    ucan_token: Optional[str] = None
    pid: Optional[int] = None


@dataclass
class RevocationEntry:
    revoked_did: str
    revoked_at: float
    signed_by: str
    reason: str


# ---------------------------------------------------------------------------
# Pending pairing offer (in-memory only on the inviter)
# ---------------------------------------------------------------------------

@dataclass
class PairingOffer:
    code: str           # 6 BIP39 words separated by spaces
    code_hash: str      # SHA-256 hex; what we publish over the wire
    created_at: float
    expires_at: float


# ---------------------------------------------------------------------------
# Household
# ---------------------------------------------------------------------------

class Household:
    def __init__(self, data_dir: str | Path = "~/.aries") -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.household_dir = self.data_dir / "household"
        self.household_dir.mkdir(parents=True, exist_ok=True)

        self.devices: dict[str, DeviceRecord] = {}
        self.agents: dict[str, AgentRecord] = {}
        self.revocation_list: list[RevocationEntry] = []
        self.ucan_store = UCANStore()

        self._user_root_key: Optional[KeyPair] = None  # only present on the founding device
        self._device_key: Optional[KeyPair] = None
        self.user_root_did: Optional[str] = None
        self.device_did: Optional[str] = None
        self._membership_ucan: Optional[str] = None
        self._membership_cid: Optional[str] = None
        self._pending_offer: Optional[PairingOffer] = None

    @property
    def manifest_path(self) -> Path:
        return self.household_dir / "household.json"

    @property
    def device_key_path(self) -> Path:
        return self.household_dir / "device_key.json"

    @property
    def is_initialized(self) -> bool:
        return self.manifest_path.exists()

    @property
    def household_tag(self) -> str:
        if not self.user_root_did:
            raise RuntimeError("Household not initialized")
        return self._household_tag(self.user_root_did)

    @staticmethod
    def _household_tag(user_root_did: str) -> str:
        return hashlib.sha256(user_root_did.encode("utf-8")).hexdigest()[:16]

    # -----------------------------------------------------------------------
    # Initialization (first device)
    # -----------------------------------------------------------------------

    def initialize(self, device_name: str, platform: str) -> dict[str, str]:
        if self.is_initialized:
            raise RuntimeError(f"Household already initialized at {self.household_dir}")

        root = KeyPair.generate()
        device = KeyPair.generate()
        root_did = public_key_to_did(root.public_bytes)
        device_did = public_key_to_did(device.public_bytes)

        shares = shamir_split(root.secret_bytes, n=3, k=2)
        for i, share in enumerate(shares, start=1):
            (self.household_dir / f"root_share_{i}.bin").write_bytes(share)

        save_keypair(device, self.device_key_path)

        membership = build_household_membership(root.signing_key, root_did, device_did)
        cid = self.ucan_store.store(membership)

        self._user_root_key = root
        self._device_key = device
        self.user_root_did = root_did
        self.device_did = device_did
        self._membership_ucan = membership
        self._membership_cid = cid

        record = DeviceRecord(
            device_did=device_did,
            name=device_name,
            platform=platform,
            paired_at=time.time(),
            membership_ucan=membership,
            is_self=True,
        )
        self.devices[device_did] = record
        self._save()

        return {
            "user_root_did": root_did,
            "device_did": device_did,
            "household_tag": self.household_tag,
            "device_name": device_name,
        }

    def load(self) -> None:
        if not self.is_initialized:
            raise RuntimeError(f"Household not initialized at {self.household_dir}")

        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.user_root_did = data["user_root_did"]
        self.device_did = data["device_did"]

        self._device_key = load_keypair(self.device_key_path)

        self.devices = {
            did: DeviceRecord(**rec)
            for did, rec in data.get("devices", {}).items()
        }
        self.agents = {
            did: AgentRecord(**rec)
            for did, rec in data.get("agents", {}).items()
        }
        self.revocation_list = [RevocationEntry(**r) for r in data.get("revocation_list", [])]

        for record in self.devices.values():
            cid = self.ucan_store.store(record.membership_ucan)
            if record.is_self:
                self._membership_ucan = record.membership_ucan
                self._membership_cid = cid

        for agent in self.agents.values():
            if agent.ucan_token:
                self.ucan_store.store(agent.ucan_token)

    # -----------------------------------------------------------------------
    # Agent registration
    # -----------------------------------------------------------------------

    def register_agent(
        self,
        name: str,
        vendor: str,
        model: Optional[str] = None,
        capabilities: Optional[list[str]] = None,
        context_window: int = 0,
        locality: str = "local",
        cost_class: str = "free",
        pid: Optional[int] = None,
    ) -> AgentRecord:
        if not self.device_did or not self._device_key:
            raise RuntimeError("Household not loaded; cannot register agent")
        if not self._membership_cid:
            raise RuntimeError("Membership UCAN missing; reload household")

        agent_key = KeyPair.generate()
        agent_did = public_key_to_did(agent_key.public_bytes)

        caps = [
            Capability(resource="*", ability="aries/agent.invoke"),
            Capability(resource="aries:context://*", ability="aries/context.read"),
            Capability(resource="aries:context://*", ability="aries/context.write"),
        ]
        token = build_agent_token(
            device_key=self._device_key.signing_key,
            device_did=self.device_did,
            agent_did=agent_did,
            capabilities=caps,
            ttl_seconds=86400,
            parent_proof_cid=self._membership_cid,
        )
        self.ucan_store.store(token)

        record = AgentRecord(
            agent_did=agent_did,
            name=name,
            vendor=vendor,
            model=model,
            capabilities=list(capabilities or []),
            context_window=context_window,
            locality=locality,
            cost_class=cost_class,
            registered_at=time.time(),
            ucan_token=token,
            pid=pid,
        )
        self.agents[agent_did] = record
        self._save()
        return record

    def revoke(self, did: str, reason: str = "") -> None:
        if not self.user_root_did:
            raise RuntimeError("Household not loaded")
        entry = RevocationEntry(
            revoked_did=did,
            revoked_at=time.time(),
            signed_by=self.device_did or self.user_root_did,
            reason=reason,
        )
        self.revocation_list.append(entry)
        self.agents.pop(did, None)
        self.devices.pop(did, None)
        self._save()

    def revoked_dids(self) -> list[str]:
        return [r.revoked_did for r in self.revocation_list]

    # -----------------------------------------------------------------------
    # Pairing — invitation side
    # -----------------------------------------------------------------------

    def start_pairing(self, ttl_seconds: int = 300) -> PairingOffer:
        """Generate a one-time 6-word code. Returns the offer; caller advertises code_hash."""
        if not self.user_root_did or not self._device_key:
            raise RuntimeError("Household not loaded; cannot start pairing")
        words = [BIP39_256[secrets.randbelow(256)] for _ in range(6)]
        code = " ".join(words)
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        offer = PairingOffer(
            code=code,
            code_hash=code_hash,
            created_at=time.time(),
            expires_at=time.time() + ttl_seconds,
        )
        self._pending_offer = offer
        return offer

    def accept_pairing_request(
        self,
        candidate_device_did: str,
        candidate_device_name: str,
        candidate_platform: str,
        presented_code: str,
    ) -> str:
        """Inviter: validate code, issue membership UCAN to the joiner. Returns JWT."""
        if self._pending_offer is None:
            raise RuntimeError("No pending pairing offer")
        offer = self._pending_offer
        if time.time() > offer.expires_at:
            self._pending_offer = None
            raise RuntimeError("Pairing offer expired")
        if hashlib.sha256(presented_code.encode("utf-8")).hexdigest() != offer.code_hash:
            raise RuntimeError("Pairing code mismatch")
        if self._user_root_key is None:
            raise RuntimeError("Cannot issue membership: this device does not hold the root key")

        membership = build_household_membership(
            self._user_root_key.signing_key,
            self.user_root_did or "",
            candidate_device_did,
        )
        cid = self.ucan_store.store(membership)

        record = DeviceRecord(
            device_did=candidate_device_did,
            name=candidate_device_name,
            platform=candidate_platform,
            paired_at=time.time(),
            membership_ucan=membership,
            is_self=False,
        )
        self.devices[candidate_device_did] = record
        self._pending_offer = None
        _ = cid
        self._save()
        return membership

    # -----------------------------------------------------------------------
    # Pairing — joiner side
    # -----------------------------------------------------------------------

    def initialize_joiner(self, device_name: str, platform: str) -> tuple[str, str]:
        """Phase 1 of joining: generate this device's key, return (device_did, name).

        After the inviter returns a membership UCAN via `accept_pairing_request`,
        call `complete_joining(membership_jwt)` to persist.
        """
        if self.is_initialized:
            raise RuntimeError(f"Household already initialized at {self.household_dir}")
        device = KeyPair.generate()
        self._device_key = device
        self.device_did = public_key_to_did(device.public_bytes)
        save_keypair(device, self.device_key_path)
        return self.device_did, device_name

    def complete_joining(
        self,
        membership_jwt: str,
        device_name: str,
        platform: str,
    ) -> dict[str, str]:
        if self._device_key is None or self.device_did is None:
            raise RuntimeError("initialize_joiner must run first")

        # Decode without verifying signature for trust-on-first-use convenience;
        # the source device just proved possession of the pairing secret.
        decoded = UCANToken.decode(membership_jwt)
        if decoded.audience != self.device_did:
            raise RuntimeError(
                f"Membership audience {decoded.audience} != this device {self.device_did}"
            )

        self.user_root_did = decoded.issuer
        self._membership_ucan = membership_jwt
        self._membership_cid = self.ucan_store.store(membership_jwt)

        self.devices[self.device_did] = DeviceRecord(
            device_did=self.device_did,
            name=device_name,
            platform=platform,
            paired_at=time.time(),
            membership_ucan=membership_jwt,
            is_self=True,
        )
        self._save()
        return {
            "user_root_did": self.user_root_did,
            "device_did": self.device_did,
            "household_tag": self.household_tag,
        }

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    def _save(self) -> None:
        payload: dict[str, Any] = {
            "version": 1,
            "user_root_did": self.user_root_did,
            "device_did": self.device_did,
            "household_tag": self.household_tag if self.user_root_did else "",
            "devices": {did: asdict(rec) for did, rec in self.devices.items()},
            "agents": {did: asdict(rec) for did, rec in self.agents.items()},
            "revocation_list": [asdict(r) for r in self.revocation_list],
        }
        self.manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
