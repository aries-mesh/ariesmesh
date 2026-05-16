"""CRDT-backed distributed key-value store with three namespaces.

Spec reference: §12.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import cbor2

from ..util import content_hash


# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------

class Namespace(str, Enum):
    CONTEXT = "context"
    MEMORY = "memory"
    CACHE = "cache"


# Canonical (v0.1.1+) form: aries:<namespace>://<path>
_CANONICAL_PREFIXES = {
    "aries:context://": Namespace.CONTEXT,
    "aries:memory://": Namespace.MEMORY,
    "aries:cache://": Namespace.CACHE,
}

# Legacy (v0.1.0) form: <namespace>://<path> — still accepted, normalized
# internally, and a DeprecationWarning is emitted once per prefix per process.
_LEGACY_PREFIXES = {
    "context://": Namespace.CONTEXT,
    "memory://": Namespace.MEMORY,
    "cache://": Namespace.CACHE,
}

_warned_legacy: set[str] = set()


def canonical_key(ns: Namespace, path: str) -> str:
    """Build a canonical resource key string."""
    return f"aries:{ns.value}://{path}"


def normalize_key(key: str) -> str:
    """Return the canonical form of a key (no-op if already canonical)."""
    for prefix in _CANONICAL_PREFIXES:
        if key.startswith(prefix):
            return key
    for prefix, ns in _LEGACY_PREFIXES.items():
        if key.startswith(prefix):
            return canonical_key(ns, key[len(prefix):])
    return key


def from_key(key: str) -> tuple[Namespace, str]:
    for prefix, ns in _CANONICAL_PREFIXES.items():
        if key.startswith(prefix):
            return ns, key[len(prefix):]
    for prefix, ns in _LEGACY_PREFIXES.items():
        if key.startswith(prefix):
            if prefix not in _warned_legacy:
                import warnings
                warnings.warn(
                    f"Legacy resource prefix {prefix!r} is deprecated; "
                    f"use 'aries:{prefix}' instead. Will be removed in v0.2.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                _warned_legacy.add(prefix)
            return ns, key[len(prefix):]
    raise ValueError(f"Key {key!r} missing namespace prefix")


DEFAULT_TTLS: dict[Namespace, Optional[float]] = {
    Namespace.CONTEXT: 86400.0,
    Namespace.MEMORY: None,
    Namespace.CACHE: 3600.0,
}


# ---------------------------------------------------------------------------
# LWWEntry (Last-Writer-Wins Register)
# ---------------------------------------------------------------------------

@dataclass
class LWWEntry:
    value: Any
    timestamp: float
    device_did: str
    wall_clock: float = field(default_factory=time.time)
    content_hash: str = ""
    ttl: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = content_hash(self.value)

    @property
    def is_expired(self) -> bool:
        return self.ttl is not None and time.time() > self.wall_clock + self.ttl

    def supersedes(self, other: "LWWEntry") -> bool:
        if self.timestamp != other.timestamp:
            return self.timestamp > other.timestamp
        return self.device_did > other.device_did

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "timestamp": self.timestamp,
            "device_did": self.device_did,
            "wall_clock": self.wall_clock,
            "content_hash": self.content_hash,
            "ttl": self.ttl,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LWWEntry":
        return cls(
            value=d["value"],
            timestamp=d["timestamp"],
            device_did=d["device_did"],
            wall_clock=d.get("wall_clock", time.time()),
            content_hash=d.get("content_hash", ""),
            ttl=d.get("ttl"),
        )

    def to_cbor(self) -> bytes:
        return cbor2.dumps(self.to_dict())

    @classmethod
    def from_cbor(cls, data: bytes) -> "LWWEntry":
        return cls.from_dict(cbor2.loads(data))


# ---------------------------------------------------------------------------
# AppendLog
# ---------------------------------------------------------------------------

@dataclass
class AppendLog:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def append(self, entry: dict[str, Any], device_did: str) -> int:
        idx = len(self.entries)
        record = dict(entry)
        record["_seq"] = idx
        record["_device"] = device_did
        record["_ts"] = time.time()
        self.entries.append(record)
        return idx

    def merge(self, remote_entries: list[dict[str, Any]]) -> None:
        seen: set[tuple[str, int]] = {(e.get("_device", ""), int(e.get("_seq", -1))) for e in self.entries}
        for entry in remote_entries:
            key = (entry.get("_device", ""), int(entry.get("_seq", -1)))
            if key in seen:
                continue
            seen.add(key)
            self.entries.append(dict(entry))
        self.entries.sort(key=lambda e: float(e.get("_ts", 0.0)))

    def since(self, index: int) -> list[dict[str, Any]]:
        if index < 0:
            return list(self.entries)
        return list(self.entries[index:])

    def __len__(self) -> int:
        return len(self.entries)


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

ChangeCallback = Callable[[str, Any], None]


class MemoryStore:
    def __init__(self, device_did: str, persist_dir: Optional[Path] = None) -> None:
        self.device_did = device_did
        self.persist_dir = Path(persist_dir).expanduser() if persist_dir else None
        if self.persist_dir is not None:
            self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._lamport_clock: float = 0.0
        self._registers: dict[str, LWWEntry] = {}
        self._logs: dict[str, AppendLog] = {}
        self._on_change: list[ChangeCallback] = []
        if self.persist_dir is not None:
            self._load()

    # ----- clock -----

    def _tick(self) -> float:
        self._lamport_clock += 1
        return self._lamport_clock

    def _update_clock(self, remote_ts: float) -> None:
        self._lamport_clock = max(self._lamport_clock, remote_ts) + 1

    @property
    def clock(self) -> float:
        return self._lamport_clock

    # ----- callbacks -----

    def on_change(self, cb: ChangeCallback) -> None:
        self._on_change.append(cb)

    def _notify(self, key: str, value: Any) -> None:
        for cb in self._on_change:
            try:
                cb(key, value)
            except Exception:
                pass

    # ----- register API -----

    def get(self, key: str) -> Optional[Any]:
        key = normalize_key(key)
        entry = self._registers.get(key)
        if entry is None:
            return None
        if entry.is_expired:
            self._registers.pop(key, None)
            return None
        return entry.value

    def get_entry(self, key: str) -> Optional[LWWEntry]:
        key = normalize_key(key)
        entry = self._registers.get(key)
        if entry is None:
            return None
        if entry.is_expired:
            self._registers.pop(key, None)
            return None
        return entry

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> LWWEntry:
        key = normalize_key(key)
        ns, _ = from_key(key)
        if ttl is None:
            ttl = DEFAULT_TTLS[ns]
        ts = self._tick()
        entry = LWWEntry(value=value, timestamp=ts, device_did=self.device_did, ttl=ttl)
        existing = self._registers.get(key)
        if existing is not None and not entry.supersedes(existing):
            return existing
        self._registers[key] = entry
        self._notify(key, value)
        self._save()
        return entry

    def delete(self, key: str) -> None:
        self.set(key, None)

    def keys(self, prefix: str = "") -> list[str]:
        prefix = normalize_key(prefix) if prefix else ""
        out: list[str] = []
        for k, entry in self._registers.items():
            if not k.startswith(prefix):
                continue
            if entry.is_expired or entry.value is None:
                continue
            out.append(k)
        return out

    def list_namespace(self, ns: Namespace) -> list[str]:
        return self.keys(canonical_key(ns, ""))

    # ----- append log API -----

    def log_append(self, key: str, entry: dict[str, Any]) -> int:
        key = normalize_key(key)
        from_key(key)  # validate namespace prefix
        log = self._logs.setdefault(key, AppendLog())
        idx = log.append(entry, self.device_did)
        self._notify(key, entry)
        self._save()
        return idx

    def log_read(self, key: str, since: int = 0) -> list[dict[str, Any]]:
        key = normalize_key(key)
        log = self._logs.get(key)
        if log is None:
            return []
        return log.since(since)

    def log_len(self, key: str) -> int:
        return len(self._logs.get(normalize_key(key), AppendLog()))

    # ----- merge API -----

    def merge_entry(self, key: str, remote_entry: LWWEntry) -> bool:
        key = normalize_key(key)
        self._update_clock(remote_entry.timestamp)
        existing = self._registers.get(key)
        if existing is not None and not remote_entry.supersedes(existing):
            return False
        self._registers[key] = remote_entry
        self._notify(key, remote_entry.value)
        self._save()
        return True

    def merge_log(self, key: str, remote_entries: list[dict[str, Any]]) -> None:
        key = normalize_key(key)
        log = self._logs.setdefault(key, AppendLog())
        log.merge(remote_entries)
        if remote_entries:
            self._notify(key, remote_entries[-1])
        self._save()

    # ----- sync state -----

    def get_sync_state(self) -> dict[str, Any]:
        return {
            "registers": {
                k: {"ts": e.timestamp, "hash": e.content_hash, "device": e.device_did}
                for k, e in self._registers.items()
                if not e.is_expired
            },
            "logs": {k: len(log) for k, log in self._logs.items()},
            "clock": self._lamport_clock,
        }

    def compute_diff(self, remote_state: dict[str, Any]) -> dict[str, Any]:
        remote_regs = remote_state.get("registers", {})
        remote_logs = remote_state.get("logs", {})

        register_diff: dict[str, dict[str, Any]] = {}
        for key, entry in self._registers.items():
            if entry.is_expired:
                continue
            rem = remote_regs.get(key)
            include = False
            if rem is None:
                include = True
            elif entry.timestamp > rem.get("ts", 0):
                include = True
            elif entry.timestamp == rem.get("ts", 0) and entry.content_hash != rem.get("hash"):
                include = True
            if include:
                register_diff[key] = entry.to_dict()

        log_diff: dict[str, list[dict[str, Any]]] = {}
        for key, log in self._logs.items():
            local_len = len(log)
            remote_len = int(remote_logs.get(key, 0))
            if local_len > remote_len:
                log_diff[key] = log.since(remote_len)

        return {"registers": register_diff, "logs": log_diff}

    def apply_diff(self, diff: dict[str, Any]) -> None:
        for key, raw in diff.get("registers", {}).items():
            self.merge_entry(key, LWWEntry.from_dict(raw))
        for key, entries in diff.get("logs", {}).items():
            self.merge_log(key, entries)

    # ----- persistence -----

    @property
    def _persist_file(self) -> Optional[Path]:
        return self.persist_dir / "memory.json" if self.persist_dir else None

    def _save(self) -> None:
        path = self._persist_file
        if path is None:
            return
        payload = {
            "clock": self._lamport_clock,
            "registers": {k: e.to_dict() for k, e in self._registers.items()},
            "logs": {k: log.entries for k, log in self._logs.items()},
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def _load(self) -> None:
        path = self._persist_file
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self._lamport_clock = float(data.get("clock", 0.0))
        for k, raw in data.get("registers", {}).items():
            try:
                self._registers[k] = LWWEntry.from_dict(raw)
            except (KeyError, TypeError):
                continue
        for k, entries in data.get("logs", {}).items():
            log = AppendLog()
            log.entries = list(entries)
            self._logs[k] = log
