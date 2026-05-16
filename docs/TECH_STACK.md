# Aries Mesh — Tech Stack & Test Inventory

This document covers the libraries that power Aries Mesh and the 46 tests that currently pass on Windows 11 with Python 3.14.3 (41 v0.1 baseline + 5 added in the v0.1.1 hardening pass).

---

## 1. Tech stack

### Runtime

| Tool | Version (installed) | Why it's here |
|------|---------------------|---------------|
| **Python** | 3.14.3 | Spec requires ≥3.11 for `Self` types, `StrEnum`, modern `asyncio`. |
| **asyncio** | stdlib | Whole node is non-blocking: TCP transport, mDNS, memory sync loop, profiler all run as cooperative tasks. |
| **uvloop** | 0.x (Linux/macOS only) | Faster event loop. Guarded by `platform_system != 'Windows'` env marker. |

### Cryptography & identity

| Library | Used for |
|---------|----------|
| **PyNaCl** 1.6.2 (libsodium) | Ed25519 sign/verify, X25519 ECDH, **Argon2id KDF + XSalsa20-Poly1305 SecretBox** (v0.1.1, password-protected device key files). Backs `KeyPair`, UCAN signatures, Receipt signatures, signed `Continuation` envelopes, `save_keypair(...,passphrase=...)`. |
| **GF(2^8) Shamir SSS** (in-tree) | 2-of-3 secret sharing of the root key using AES polynomial `0x11B` with `0x03` as primitive generator. |
| **BIP39 word subset** (first 256 English words, in-tree) | Human-readable key fingerprints and 6-word pairing invitation codes. |
| **did:key** encoding (in-tree base58btc) | W3C-compliant Ed25519 DIDs of the form `did:key:z6Mk…`. |
| **UCAN 1.0** (in-tree, JWT/EdDSA) | Capability tokens with delegation chains: root → device → agent. Validates linkage, expiry, revocation, capability attenuation. |

### Transport & discovery

| Library | Used for |
|---------|----------|
| **cbor2** 6.1.1 | Wire-format encoding of every `AriesMessage` (length-prefixed CBOR). |
| **zeroconf** 0.148.0 (async API) | mDNS service `_aries._tcp.local.` with TXT records for device DID, household tag, capabilities. |
| **asyncio TCP streams** | `TransportServer` + `PeerConnection` — 4-byte length-prefixed CBOR over plain TCP loopback / LAN. |
| **websockets** 16.0 | Reserved for future fallback transport. |

### Scheduling & profiling

| Library | Used for |
|---------|----------|
| **psutil** 7.2.2 | CPU %, RAM, battery, thermal, NIC speed snapshots every 10s (`DeviceProfiler`). |
| **PyYAML** 6.0.3 | Loads `~/.aries/mandates.yaml` into `Mandate` dataclasses for the scheduler. |

### Memory (CRDT) & sync

In-tree, no third-party CRDT lib:

- **Last-Writer-Wins Register** (`LWWEntry`) with Lamport clock and DID tie-break.
- **Append-only Log** (`AppendLog`) deduplicated by `(device, _seq)` tuple.
- **Two-phase sync** over the transport layer: `request → response → update`, 100 ms debounce + 30 s periodic.
- **BLAKE3** 1.0.8 (SHA-256 fallback) for content hashes inside entries, receipts, and (v0.1.1) the *full* `Continuation` envelope.
- **Canonical resource grammar** (v0.1.1): every memory key and UCAN capability resource uses `aries:<namespace>://<path>`. The store accepts the legacy `<namespace>://...` form too, with a one-shot `DeprecationWarning` per prefix — removed in v0.2.

### LLM adapters

| Library | Used for |
|---------|----------|
| **litellm** 1.49.7 | Universal client. One `LiteLLMAdapter` wraps Anthropic, OpenAI, Google, Ollama, custom OpenAI-compatible endpoints. |
| **httpx** 0.28.1, **openai** 2.36.0, **anthropic-style SDK shims** | Pulled in by litellm. Not used directly. |
| In-tree **MockAdapter** | Deterministic offline adapter for tests and `aries register --vendor mock`. |

### CLI / display

| Library | Used for |
|---------|----------|
| **click** 8.3.3 | Subcommand router for `aries`. |
| **rich** 15.0.0 | Panels, tables, trees in the terminal output. |

### Dev tooling

| Library | Used for |
|---------|----------|
| **pytest** 9.0.3 | Test runner. Auto async mode via `pytest-asyncio` 1.3.0. |
| **ruff** 0.15.13 | Lint/format (config in `pyproject.toml`, line-length 100). |
| **setuptools** ≥68 + **wheel** | Editable install via `pip install -e ".[dev]"`. Build-backend `setuptools.build_meta`. |

### Module dependency graph (bottom-up)

```
util.py / _wordlist.py
keys.py        identity      Ed25519, Shamir, fingerprint, save_keypair (Argon2id+SecretBox when passphrased)
did.py         identity      did:key encode/decode
ucan.py        identity      JWT capability tokens + chains + /* glob attenuation
household.py   identity      first-device init, agent reg, pairing
peer.py        transport     CBOR wire, TCP server, connections
discovery.py   transport     mDNS over zeroconf
router.py      scheduler     Filter -> Mandate -> Score -> Select + YAML mandates
profile.py     scheduler     psutil snapshots
store.py       memory        LWW + AppendLog + Lamport + canonical aries: grammar
sync.py        memory        two-phase sync
adapters/base.py            BaseAdapter ABC + Message types
adapters/litellm_adapter.py LiteLLMAdapter + 5 conveniences
adapters/mock_adapter.py    MockAdapter
continuation.py              Signed hand-off envelope (full-field hash + Ed25519 envelope sig)
receipt.py                   Hash-linked signed audit chain
node.py                      AriesNode daemon: signs continuations on send, verifies on receive,
                             auto-resumes; handoff requires explicit target (no broadcast)
cli/main.py                  `aries` entry point
```

---

## 2. Architecture (one-paragraph version)

A per-device daemon (`AriesNode`) brings up: a TCP transport with CBOR framing; an mDNS service that advertises the device's DID + a SHA-256-truncated `household_tag`; a hardware profiler; a scheduler that ranks agents on privacy×3, capability×2, latency×1.5, cost×1, health×1; a CRDT memory store with three TTL'd namespaces under the canonical resource grammar `aries:<namespace>://<path>`; and one or more LLM adapters. Devices pair into a household using a 6-word BIP39 code; the inviter issues a no-expiry UCAN membership token to the joiner. Tasks invoked on one device can be handed off as a **signed** `Continuation` envelope (the signature covers every field except the signature itself); the receiver verifies first, then auto-resumes by running its local scheduler against the continuation's required capabilities and locality preference. Handoff requires an explicit target — no broadcast-by-default. Every invoke/handoff produces an Ed25519-signed `Receipt`, hash-linked into a per-task chain. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the canonical spec and [`../Aries_Mesh_Engineering_Spec.md`](../Aries_Mesh_Engineering_Spec.md) for the historical PRD.

### v0.1.1 hardening (this release)

The v0.1 baseline shipped with five known gaps; all are fixed here and pinned by tests:

| # | Fix | Where |
|---|-----|-------|
| 1 | `Continuation` hash + Ed25519 signature now cover every field | `src/aries/continuation.py`, `tests/test_security.py::test_continuation_tamper_detected` |
| 2 | `handoff()` requires explicit target; no broadcast fallback | `src/aries/node.py`, `tests/test_security.py::test_handoff_without_target_raises` |
| 3 | `save_keypair()` does real Argon2id + SecretBox encryption when a passphrase is given (legacy `save_key_encrypted` alias kept) | `src/aries/identity/keys.py`, `tests/test_security.py::test_save_keypair_encrypted_roundtrip` |
| 4 | Continuation receive is **auto-resume**, single canonical answer | `docs/ARCHITECTURE.md`, `src/aries/node.py` (deviates from PRD §18) |
| 5 | Canonical resource grammar `aries:<namespace>://<path>` with `/*` glob in UCAN capabilities; legacy `<namespace>://` still accepted with `DeprecationWarning` | `src/aries/memory/store.py`, `src/aries/identity/ucan.py`, `tests/test_security.py::test_capability_wildcard_glob` |

A fifth security test (`test_revoked_device_cannot_chain_validate`) covers issue #8's revocation-propagation sample — revoking a device must break downstream agent chain validation even though the agent's signature itself is still good.

Deferred to v0.2: hardened pairing (PAKE + key confirmation), causality-aware conversation merge, full adversarial fuzz suite.

---

## 3. The 46 tests, by file

Run the whole suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
# 46 passed in 3.53s
```

### `tests/test_identity.py` — 9 tests

Covers `keys.py`, `did.py`, fingerprint, Shamir SSS.

1. **test_keypair_public_bytes_length** — Generated KeyPair exposes 32-byte public/secret bytes.
2. **test_sign_verify_roundtrip** — `kp.sign(msg)` → `kp.verify(msg, sig)` is `True`.
3. **test_sign_with_wrong_key_fails** — Verifying A's signature with B's key returns `False`.
4. **test_verify_detached_helper** — Module-level `verify_detached(pub, msg, sig)` matches keypair verification.
5. **test_shamir_2of3_reconstruction** — Splitting a 32-byte secret into 3 shares; any 2 of 3 reconstruct.
6. **test_shamir_all_three_reconstructs** — Using all 3 shares also reconstructs (over-determined case).
7. **test_did_key_roundtrip** — `public_key_to_did → did_to_public_key` is an identity. DID starts with `did:key:z6Mk`.
8. **test_did_short_display** — `did_short(did)` returns abbreviated form with `...`.
9. **test_fingerprint_deterministic** — Same public key → same 6-word fingerprint; different keys differ.

### `tests/test_ucan.py` — 9 tests

Covers `ucan.py` — token signing, decoding, capability attenuation, chain validation.

1. **test_build_sign_and_verify_token** — Sign a UCAN with issuer's key, `UCANToken.verify(jwt)` succeeds.
2. **test_decode_skips_signature** — `UCANToken.decode` parses payload without verifying signature.
3. **test_expired_flag** — A UCAN with `expiration` in the past has `is_expired=True` and `is_active=False`.
4. **test_capability_attenuation_match** — Child capability with a strict-prefix resource is attenuated by parent; `*` parent matches everything; identity matches itself.
5. **test_capability_different_ability_rejected** — Different abilities are not attenuated even with matching resource.
6. **test_chain_validation_root_token** — Self-issued membership UCAN validates against the root DID.
7. **test_chain_validation_two_level** — root → device (membership) → agent chain validates end-to-end, including a required-capability check.
8. **test_chain_validation_revoked_issuer_raises** — Validation raises `ValueError("…revoked…")` when issuer is in the revocation list.
9. **test_chain_validation_broken_linkage_raises** — Validation raises when proof's audience ≠ token's issuer.

### `tests/test_scheduler.py` — 8 tests

Covers `router.py` — filtering, scoring, mandates, YAML loader.

1. **test_filter_capability_match** — Filter keeps only agents whose `capabilities` include the requested capability.
2. **test_filter_local_only_excludes_cloud** — `Locality.LOCAL_ONLY` excludes `cloud-routed` agents.
3. **test_filter_cost_class_excludes_paid** — `max_cost_class="metered"` drops `paid` agents.
4. **test_score_local_beats_cloud_on_privacy** — Default weights (privacy=3) make a local agent outrank a cloud agent.
5. **test_score_free_beats_paid_when_local_equal** — Zeroing other weights, `cost_class="free"` wins over `paid`.
6. **test_mandate_tag_override** — A tag-triggered mandate that enforces `local` locality overrides a user-supplied `Locality.ANY`.
7. **test_devicehealth_low_battery_reduces_score** — Discharging battery below 10% pushes `health_score` to ≤0.2.
8. **test_yaml_mandates_roundtrip** — `load_mandates_from_yaml` reads two mandates from a YAML file and surfaces their fields.

### `tests/test_memory.py` — 8 tests

Covers `store.py` — LWW CRDT, AppendLog, TTL, sync diff, persistence.

1. **test_set_get_roundtrip** — `store.set(key, value)` is retrievable via `store.get(key)`.
2. **test_ttl_expiry** — Setting `cache://` value with `ttl=0.05` returns `None` after sleeping 0.1 s.
3. **test_namespace_parsing** — `from_key("context://x")` returns `(Namespace.CONTEXT, "x")`; missing prefix raises.
4. **test_lww_higher_timestamp_supersedes** — `LWWEntry.supersedes` honours timestamp ordering.
5. **test_lww_tie_breaks_by_did** — Equal timestamps break ties by lexicographic DID.
6. **test_append_log_dedup_by_device_seq** — Merging remote entries dedups on `(_device, _seq)` and sorts by `_ts`.
7. **test_sync_state_compute_apply_roundtrip** — Two stores converge after computing diffs against each other's sync state.
8. **test_persistence_roundtrip** — Writing to a `persist_dir` then constructing a new store from the same dir recovers registers and logs.

### `tests/test_adapters.py` — 5 tests

Covers `base.py`, `litellm_adapter.py`, `mock_adapter.py`.

1. **test_vendor_inference** — Model strings like `claude-…`, `gpt-…`, `gemini/…`, `ollama/…`, `openai/…` map to the right vendor.
2. **test_capabilities_shape** — `ollama_adapter().capabilities()` returns the expected dict with vendor, locality, cost_class.
3. **test_convenience_constructors** — `anthropic_adapter`, `openai_adapter`, `google_adapter` all infer the right vendor.
4. **test_request_response_serialization** — `Message.to_dict / from_dict` round-trips; `InvokeResponse.total_tokens` sums prompt + completion.
5. **test_mock_adapter_invoke** — `MockAdapter.invoke()` echoes the last user message in the canned response and reports `health_check() is True`.

### `tests/test_two_node.py` — 2 tests

Integration tests with two `AriesNode` instances on loopback TCP (mDNS skipped for determinism; covered by the manual WSL2 smoke).

1. **test_pairing_and_memory_sync** — Both nodes start, share a household via the pairing code flow, exchange ANNOUNCEs, and a `context://test/value` written on node A appears on node B within 2 s.
2. **test_handoff_auto_resume** — Node A has no agents; node B has a `MockAdapter`. A calls `handoff(target_device_did=B.device_did, …)`. B receives the signed `Continuation`, verifies, ACKs, runs its local scheduler, invokes the mock, and logs the assistant reply under the *continuation's* `task_id` in its own memory.

### `tests/test_security.py` — 5 tests (new in v0.1.1)

Adversarial tests pinning each hardening fix.

1. **test_continuation_tamper_detected** — Sign a `Continuation`, mutate `max_cost_class` (then `required_capabilities`), expect `verify()` to return `False` in both cases. Pins Fix 1.
2. **test_handoff_without_target_raises** — `AriesNode.handoff(target_device_did="")` raises `ValueError` mentioning `target_device_did`. No silent broadcast escape. Pins Fix 2.
3. **test_save_keypair_encrypted_roundtrip** — Save with passphrase; load with the right passphrase recovers the key; load with the wrong passphrase raises `nacl.exceptions.CryptoError`; load with no passphrase raises `ValueError`. Pins Fix 3.
4. **test_revoked_device_cannot_chain_validate** — Build a `root -> device -> agent` UCAN chain; chain validates clean; add `device_did` to the revocation list; the *same* agent token now fails chain validation with `ValueError("…revoked…")`. Covers issue #8's revocation-propagation sample.
5. **test_capability_wildcard_glob** — `Capability("aries:context://tasks/abc/*", read)` covers `aries:context://tasks/abc/history` and the bare `aries:context://tasks/abc`, but not `aries:context://tasks/xyz/history`, and not the same path with `write` ability. The namespace-wide glob covers everything in the namespace. Pins Fix 5.

---

## 4. How to reproduce

```powershell
# from C:\Users\indra\OneDrive\Desktop\ARIES\aries-mesh
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m pytest                            # 46 passed (41 baseline + 5 v0.1.1 security)
$env:PYTHONIOENCODING="utf-8"               # for rich output on Windows
aries --help
aries --data-dir $env:TEMP\aries-demo init --name demo
aries --data-dir $env:TEMP\aries-demo register --vendor mock --model demo-1
aries --data-dir $env:TEMP\aries-demo invoke -m "hello" --locality local-only
```

---

*Last refresh: 2026-05-15 (v0.1.1 hardening pass). All 46 tests green under Windows 11, Python 3.14.3, no Ollama or API key required.*
