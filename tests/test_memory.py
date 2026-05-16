"""Unit tests for the memory store: LWW, AppendLog, TTL, sync diff, persistence."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from aries.memory.store import (
    AppendLog,
    LWWEntry,
    MemoryStore,
    Namespace,
    from_key,
)


def test_set_get_roundtrip() -> None:
    store = MemoryStore(device_did="did:key:zdev")
    store.set("context://tasks/a", {"hello": "world"})
    assert store.get("context://tasks/a") == {"hello": "world"}


def test_ttl_expiry() -> None:
    store = MemoryStore(device_did="did:key:zdev")
    store.set("cache://short", "x", ttl=0.05)
    time.sleep(0.1)
    assert store.get("cache://short") is None


def test_namespace_parsing() -> None:
    assert from_key("context://x") == (Namespace.CONTEXT, "x")
    assert from_key("memory://x/y") == (Namespace.MEMORY, "x/y")
    assert from_key("cache://x") == (Namespace.CACHE, "x")
    with pytest.raises(ValueError):
        from_key("no-prefix")


def test_lww_higher_timestamp_supersedes() -> None:
    a = LWWEntry(value="old", timestamp=1.0, device_did="did:key:zA")
    b = LWWEntry(value="new", timestamp=2.0, device_did="did:key:zA")
    assert b.supersedes(a)
    assert not a.supersedes(b)


def test_lww_tie_breaks_by_did() -> None:
    a = LWWEntry(value="x", timestamp=5.0, device_did="did:key:zA")
    b = LWWEntry(value="y", timestamp=5.0, device_did="did:key:zB")
    assert b.supersedes(a)


def test_append_log_dedup_by_device_seq() -> None:
    log = AppendLog()
    log.append({"msg": "hi"}, device_did="did:key:zA")
    log.append({"msg": "there"}, device_did="did:key:zA")
    remote = [
        {"msg": "hi", "_device": "did:key:zA", "_seq": 0, "_ts": 1.0},
        {"msg": "world", "_device": "did:key:zB", "_seq": 0, "_ts": 1.5},
    ]
    log.merge(remote)
    # Only the new "world" entry from B is added; the duplicate from A is skipped
    assert len(log.entries) == 3
    devices = sorted({e["_device"] for e in log.entries})
    assert devices == ["did:key:zA", "did:key:zB"]


def test_sync_state_compute_apply_roundtrip() -> None:
    a = MemoryStore(device_did="did:key:zA")
    b = MemoryStore(device_did="did:key:zB")
    a.set("context://k1", "from-a")
    a.set("context://k2", "shared")
    b.set("context://k2", "from-b")
    b.set("memory://only-b", "b-only")
    diff_for_b = a.compute_diff(b.get_sync_state())
    b.apply_diff(diff_for_b)
    diff_for_a = b.compute_diff(a.get_sync_state())
    a.apply_diff(diff_for_a)
    # b's k2 is later (higher lamport since b incremented after seeing a)
    # ensure both stores converge on the same value for k1 and the LWW winner for k2
    assert a.get("context://k1") == "from-a"
    assert b.get("context://k1") == "from-a"
    assert a.get("memory://only-b") == "b-only"
    assert a.get("context://k2") == b.get("context://k2")


def test_persistence_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as d:
        store = MemoryStore(device_did="did:key:zA", persist_dir=Path(d))
        store.set("memory://persisted", "yes")
        store.log_append("memory://events", {"e": 1})

        revived = MemoryStore(device_did="did:key:zA", persist_dir=Path(d))
        assert revived.get("memory://persisted") == "yes"
        assert revived.log_read("memory://events")[0]["e"] == 1
