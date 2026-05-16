# Aries Mesh — Architecture

The historical engineering PRD lives at [../../Aries_Mesh_Engineering_Spec.md](../../Aries_Mesh_Engineering_Spec.md). Where this document and the PRD disagree, **this document is the source of truth** (the PRD is preserved as the original brief; v0.1.1 hardening produced a few deliberate deviations, called out below).

## Layers (bottom-up)

| Layer | Modules | Purpose |
|-------|---------|---------|
| 0 — Identity | `identity/{keys,did,ucan,household}.py` | Ed25519 keys, Shamir 2-of-3, did:key, UCAN delegation, household state |
| 1 — Transport | `transport/{peer,discovery}.py` | TCP message envelope, mDNS peer discovery |
| 2 — Scheduler | `scheduler/{router,profile}.py` | Hardware profiling, 4-stage routing (Filter -> Mandate -> Score -> Select) |
| 3 — Memory | `memory/{store,sync}.py` | LWW CRDT + AppendLog, two-phase sync |
| Adapters | `adapters/{base,litellm,mock}_adapter.py` | LLM vendor abstraction |
| Protocol | `continuation.py`, `receipt.py` | Signed hand-off envelope + signed audit chain |
| Daemon | `node.py` | Per-device runtime tying all layers |
| CLI | `cli/main.py` | `aries` command-line entry point |

---

## Resource grammar (canonical)

All resources — memory keys, UCAN capability targets, audit-log references — share a single URI form:

```
aries:<namespace>://<path>
```

Where `<namespace>` is one of `context`, `memory`, `cache`. Examples:

| Resource | Meaning |
|----------|---------|
| `aries:context://tasks/abc/history` | Per-task conversation log; TTL 24 h |
| `aries:memory://prefs/theme` | Persistent user/agent memory; no TTL |
| `aries:cache://embeddings/sha256-…` | Best-effort cache; TTL 1 h |

UCAN capability resources use the same form and support `*` and `/*` for wildcarding:

- `Capability("*", "aries/agent.invoke")` — invoke any agent.
- `Capability("aries:context://*", "aries/context.read")` — read anything in the context namespace.
- `Capability("aries:context://tasks/abc/*", "aries/context.read")` — read anything under task `abc`.

`Capability.is_attenuated_by` (`src/aries/identity/ucan.py`) enforces these rules — a child resource is attenuated by a parent when the parent's path is a strict prefix (with `/`) or the parent ends in `/*` and the child sits below it.

**Legacy form.** v0.1.0 used `<namespace>://...` without the `aries:` prefix. `memory.from_key` still accepts the legacy form for one minor version and emits a `DeprecationWarning`. It is normalized internally to canonical, so a key written under one form reads back under the other.

---

## Continuation envelope (Fix 1 — v0.1.1)

A `Continuation` (`src/aries/continuation.py`) is the hand-off payload between devices. **Every field except the signature itself is covered by the envelope hash.** The source device signs the canonical-JSON serialization of `_signable_content()` with its Ed25519 device key; the receiver verifies with `did_to_public_key(signed_by)` before doing anything else.

If `cont.verify()` returns false:
- The continuation is rejected silently from the application path.
- A signed `Receipt(action="handoff_received", status="error", summary="rejected: bad signature")` is appended to the local chain.
- An `ERROR` AriesMessage is sent back to the source with `continuation_id` and a short error string.
- **No ACK is sent. No auto-resume runs. No memory log is written.**

This closes the original v0.1 gap where mutating `metadata`, `ucan_chain`, `required_capabilities`, `locality_preference`, `max_cost_class`, or `reason` would not have changed the (narrow) content hash.

---

## Handoff transport contract (Fix 2 — v0.1.1)

`AriesNode.handoff(target_device_did, …)` **requires an explicit target**. Without it, the call raises `ValueError`. If the target is not a currently-connected peer, it raises `ConnectionError`. There is no silent broadcast.

For the "let the system pick a peer" flow, use `handoff_to_best_peer()`. It iterates connected peers and selects the first whose ANNOUNCE-advertised capabilities cover the requested ones; if none match, it raises. v0.1.1 keeps this minimal — peer capability tracking comes from the existing `_handle_announce` body, not a new protocol round-trip.

---

## Continuation receive — canonical behavior (Fix 4 — v0.1.1)

The canonical behavior on continuation receive is **auto-resume**:

1. Deserialize the `Continuation` from the inbound `AriesMessage` body.
2. Verify the envelope signature. On failure, take the rejection path above and stop.
3. Append the user messages to local memory at `_canonical_history_key(cont.task_id)`.
4. Sign a "handoff_received" receipt.
5. Send `ACK` back to the source.
6. Run `AriesNode.invoke(messages=cont.messages, capability=cont.required_capabilities[0], locality=cont.locality_preference, …)` against the local scheduler.
7. Mirror the assistant reply under the continuation's `task_id` (so the cross-device log stays coherent on this device).
8. Send `INVOKE_RESULT` back to the source.

**ACK-then-wait is not a supported mode.** Step 6 is unconditional. The original PRD §18 described an ACK-only flow; v0.1.1 supersedes it. `aries resume <task_id>` remains as a manual debugging escape hatch.

---

## Key storage (Fix 3 — v0.1.1)

`save_keypair(key, path, passphrase=None)` (`src/aries/identity/keys.py`) is the only key persistence entry point. The legacy name `save_key_encrypted` is preserved as an alias and will be removed in v0.2.

- **With a passphrase:** Argon2id KDF (16-byte random salt, `MODERATE` ops/mem limits) derives a 32-byte key; the Ed25519 secret is sealed with NaCl `SecretBox` (XSalsa20-Poly1305) and a fresh 24-byte nonce. File format version 2. `load_keypair(path)` without the passphrase raises `ValueError`; a wrong passphrase raises `nacl.exceptions.CryptoError`.
- **Without a passphrase:** plaintext hex with an explicit `_warning: "plaintext"` field in the JSON. File format version 1. This is the v0.1.1 first-device-init default because the three Shamir shares of the root key in the same directory are also plaintext; protection is the OS file ACL (`0600` on POSIX, user profile dir on Windows).

The original PRD called the function `save_key_encrypted` even though it wrote plaintext — that name was a security footgun and has been retired.

---

## Pairing threat model

Pairing trusts physical proximity: the inviter prints a 6-word code from the BIP39 short list; the joiner types it. Same household-tag filter applies on mDNS so peers cannot accidentally join the wrong household. **No defense against a local attacker who shoulder-surfs the code, joins the LAN, or spoofs an mDNS announcement at the right moment.** Acceptable for v0.1 inside a home network. v0.2 will harden this (see "Deferred" below).

---

## Deferred to v0.2

These are known gaps with concrete planned fixes:

- **Pairing — strong UX.** Replace the plain BIP39 code with an out-of-band PAKE (e.g. SPAKE2) plus an explicit key-confirmation step so neither side commits until both have proven knowledge. Add mDNS reply authentication so a hostile peer cannot impersonate the inviter.
- **Conversation-log causality.** Today `AppendLog.merge` does timestamp-sorted union, which is deterministic but can re-order events under network partitions. v0.2 moves to a per-task vector clock so each device's append order is preserved and merges respect causality.
- **Full adversarial test suite.** v0.1.1 adds five targeted tests in `tests/test_security.py`. v0.2 will add property/fuzz tests on the wire format, replay/expiry edge cases on UCAN tokens, and pairing-handshake abuse cases.

---

## Module dependency graph (build-bottom-up)

```
util / _wordlist
keys.py        identity      Ed25519, Shamir, fingerprint, save_keypair/load_keypair
did.py         identity      did:key encode/decode
ucan.py        identity      JWT capability tokens + chain validation + /* glob
household.py   identity      first-device init, agent reg, pairing
peer.py        transport     CBOR wire, TCP server, connections
discovery.py   transport     mDNS over zeroconf
router.py      scheduler     Filter -> Mandate -> Score -> Select + YAML loader
profile.py     scheduler     psutil snapshots
store.py       memory        LWW + AppendLog + Lamport + canonical resource grammar
sync.py        memory        two-phase sync
adapters/base.py            BaseAdapter ABC + Message types
adapters/litellm_adapter.py LiteLLMAdapter + 5 conveniences
adapters/mock_adapter.py    MockAdapter
continuation.py              Signed hand-off envelope
receipt.py                   Hash-linked signed audit chain
node.py                      AriesNode daemon (signs, verifies, auto-resumes)
cli/main.py                  `aries` entry point
```
