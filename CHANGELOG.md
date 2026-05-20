# Changelog

All notable changes to Aries Mesh are documented here.
This project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [v0.1.1] — Security hardening

Five trust-critical gaps surfaced by a v0.1 review, all fixed and pinned by adversarial tests.

- **Signed continuation envelopes.** `Continuation` now carries an Ed25519 signature whose payload is the canonical JSON of every field except the signature itself. Receivers verify before any auto-resume; tampered envelopes are rejected with a typed `ERROR` reply and a signed `handoff_received status=error` receipt — no ACK, no memory write.
- **Handoff requires an explicit target.** `AriesNode.handoff(target_device_did=...)` is now mandatory; the prior broadcast-on-`None` fallback is gone. A new `handoff_to_best_peer()` helper picks a target from ANNOUNCE-advertised peer capabilities when the caller wants the system to choose.
- **Real key encryption.** `save_key_encrypted` was a misnomer — it wrote plaintext. Renamed to `save_keypair`; when a passphrase is given, the Ed25519 secret is sealed with NaCl `SecretBox` (XSalsa20-Poly1305) under a key derived via `argon2id.kdf`. v1 plaintext files now self-document with a `_warning: "plaintext"` field.
- **Canonical resource grammar.** Memory keys and UCAN capability targets now share a single URI form `aries:<namespace>://<path>` (e.g. `aries:context://tasks/abc/history`). The store accepts the legacy `<namespace>://` form for one minor version with a `DeprecationWarning`. `Capability.is_attenuated_by` gains `/*` glob semantics — a parent `aries:context://tasks/abc/*` covers any child under that path but not `tasks/xyz`.
- **Auto-resume is the single canonical continuation receive behavior.** The historical PRD §18 described ACK-then-wait; `docs/ARCHITECTURE.md` and the `node.py` module docstring now state explicitly that `_handle_continuation` verifies → logs → ACKs → runs the local scheduler → invokes → mirrors the assistant reply. Manual `aries resume <task_id>` remains as a debugging escape hatch.

Plus five new adversarial tests in `tests/test_security.py`:
`test_continuation_tamper_detected`, `test_handoff_without_target_raises`,
`test_save_keypair_encrypted_roundtrip`, `test_revoked_device_cannot_chain_validate`,
`test_capability_wildcard_glob`. Total: **46 tests passing** (41 baseline + 5 new).

---

## [v0.1.0] — Initial prototype

The end-to-end build 

- **Identity layer.** Ed25519 `KeyPair` with GF(2^8) Shamir 2-of-3 secret sharing of the root key, did:key DID encoding, UCAN 1.0 capability tokens with delegation-chain validation, Household manifest with pairing primitives.
- **Transport layer.** Length-prefixed CBOR `AriesMessage` over plain TCP, `TransportServer` with per-message-type handler registry, async mDNS service `_aries._tcp.local.` for peer discovery (`zeroconf`).
- **Scheduler layer.** Four-stage routing pipeline (Filter → Mandate → Score → Select) with default weights privacy=3, capability=2, latency=1.5, cost=1, health=1; `DeviceProfiler` taking 10-second `psutil` snapshots; YAML mandate loader at `~/.aries/mandates.yaml`.
- **Memory layer.** Last-Writer-Wins CRDT register with Lamport clock and DID tie-break, dedup'd append-only logs, three TTL'd namespaces (`context://`, `memory://`, `cache://`), two-phase sync over the transport with 100ms debounce + 30s periodic loop.
- **Adapters.** `LiteLLMAdapter` covering Anthropic, OpenAI, Google, Ollama, and any OpenAI-compatible endpoint through `litellm`; `MockAdapter` for offline tests and demos.
- **Protocol.** `Continuation` hand-off envelope, `Receipt` hash-linked signed audit chain.
- **Daemon.** `AriesNode` ties all layers together with pairing (`start_pairing`/`accept_pairing_request`/`pair_with_invitation`) and continuation auto-resume.
- **CLI.** `aries init`, `start`, `pair`, `register`, `invoke`, `resume`, `agents`, `status`, `memory`, `household`, `mandate`.

41 tests passing across `test_identity.py`, `test_ucan.py`, `test_scheduler.py`, `test_memory.py`, `test_adapters.py`, and the two-node integration test in `test_two_node.py`.
