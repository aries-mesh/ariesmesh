# Aries Mesh — Threat Model

**Version:** 0.2.0
**Status:** This document covers the security architecture as implemented in v0.2. It is not a formal audit. We welcome security review — see [SECURITY.md](../SECURITY.md) for reporting guidelines.

---

## 1. Trust boundaries

Aries Mesh has three trust boundaries, ordered from outermost to innermost.

### Boundary 1 — Household

All devices within a household share a common root of trust: the user's Ed25519 root key, split 2-of-3 via Shamir's Secret Sharing over GF(256). Devices inside the boundary are authenticated through UCAN delegation chains rooted at this key. A device outside the boundary cannot join without (a) a 6-word pairing code transmitted out-of-band and (b) a valid UCAN membership token issued by an existing household member.

The household identifier exposed on the network (the `household_tag` in the mDNS TXT record) is a truncated SHA-256 hash, not the root DID. Non-members on the LAN can see that a household exists but cannot determine which user it belongs to.

### Boundary 2 — Device

Each device holds its own Ed25519 keypair, distinct from the user root key. Agents on a device receive ephemeral keys with time-scoped, capability-attenuated UCAN tokens (24-hour default TTL). A compromised agent cannot exceed the permissions granted in its UCAN — the memory store enforces the `aries/context.write` ACL on every write operation. A compromised device can be revoked by any other household member; the revocation propagates and instantly invalidates the device's UCAN chain, including all downstream agent tokens issued by it.

### Boundary 3 — Network

The network is treated as hostile. All inter-device communication is encrypted via the Noise Protocol Framework (Noise_XX pattern with X25519 ECDH, ChaCha20-Poly1305 AEAD, SHA-256 hashing). Forward secrecy is guaranteed by the ephemeral key pair each side generates per session — compromising the long-term static key cannot decrypt past sessions.

In-scope threats on this boundary include: WiFi eavesdroppers, ARP/DNS spoofing, rogue devices on the same LAN, hostile public WiFi access points, and passive packet capture. Out-of-scope: a global passive adversary with access to ISP backbones (Aries Mesh is a LAN-first protocol; cross-network routing is out of scope for v0.2).

---

## 2. Threat matrix

The full set of identified threats, their severity, the mitigation present in v0.2, and the residual risk that remains.

| Threat | Severity | Mitigation in v0.2 | Residual risk |
|---|---|---|---|
| **Eavesdropping on inter-node traffic** | High | Noise Protocol (X25519 ECDH + ChaCha20-Poly1305 AEAD) encrypts every message. Forward secrecy via ephemeral keys per session. | None — ciphertext only on the wire. |
| **Rogue device joining the household** | High | Requires a 6-word pairing code (physical proximity) and a UCAN membership token issued by an existing member. The mDNS household tag is a truncated SHA-256 hash and does not reveal the root DID to non-members. | Pairing code can be shoulder-surfed. A PAKE (SPAKE2 / OPAQUE) is planned for v0.3 to eliminate this. |
| **Man-in-the-middle during pairing** | Medium | The Noise_XX handshake authenticates both sides via static Ed25519 keys. An attacker without a valid household keypair cannot complete the handshake. | The initial pairing (before the first handshake) relies on the 6-word code for trust establishment. An attacker who intercepts the code at the time it is shown can impersonate the joiner. PAKE in v0.3 closes this window. |
| **Compromised device in the household** | High | Any device can add the compromised device's DID to the household revocation list. Revocations propagate immediately to all connected peers; the revoked device's UCAN chain is invalidated, including all downstream agent tokens. The blast radius is contained to that one device. | Between compromise and revocation, the attacker has the device's UCAN capabilities. Anomaly monitoring (alerts on unusual invoke patterns, etc.) is future work. |
| **Compromised agent on a trusted device** | Medium | Agent UCANs are time-scoped (default 24-hour TTL) and capability-attenuated. An agent with `aries:context://tasks/abc/*` write permission cannot access `aries:context://tasks/xyz/*` or `aries:memory://*`. The memory store enforces ACLs on `set()` and `log_append()`. | An agent granted a broad capability such as `aries:context://*` has broad access. Minimize agent scope at registration time. |
| **Activation tensor interception (distributed inference)** | High | Hidden state tensors between pipeline stages flow over the Noise-encrypted transport. An eavesdropper sees ciphertext only. | A compromised participating device has access to the hidden states present on that device. Hidden states are not human-readable but can theoretically be inverted to approximate inputs (academic research, not practical with current open techniques). Trust-weighted layer placement — pinning the embedding layer and LM head to the user's most-trusted device — is planned for v0.3. |
| **Memory poisoning via CRDT sync** | Medium | Writes from agents are UCAN-scoped. Writes from peer sync use LWW-Register semantics with Lamport clocks: a rogue writer needs a higher logical timestamp, which requires the Lamport clock to advance. CRDT merge is deterministic and auditable via the signed receipt chain. | A compromised household-member device can write to shared memory within its UCAN scope until it is revoked. Revocation stops further writes; pre-revocation CRDT state may need manual reconciliation. |
| **Key material theft** | Critical | Device keys are stored as Argon2id-derived + XSalsa20-Poly1305 SecretBox encrypted JSON when a passphrase is set. Root key shares use Shamir 2-of-3 — no single device holds the complete root. Key files and Shamir share files use `0600` permissions on POSIX systems and the user profile ACL on Windows. | If a passphrase is not set (v0.2 default for `aries init`), the key file is plaintext JSON with `_warning: "plaintext"` and file permissions are the only protection. If two of the three Shamir shares are stolen, the root key is reconstructible. Secure the third (backup) share. |
| **Replay attacks** | Low | Every `AriesMessage` carries a unique ID, a wall-clock timestamp, and a per-connection sequence number. The Noise Protocol's CipherState nonce counter prevents replay of encrypted messages within a session. UCAN tokens carry `nbf` (not-before) and `exp` (expiration) fields. | No explicit cross-session message deduplication beyond the Noise nonce ordering. Sufficient for v0.2; explicit application-level dedup is a v0.3 consideration. |
| **Denial of service (message flooding)** | Medium | No rate limiting in v0.2. A connected peer could flood the transport with messages. | Per-peer rate limits — especially on `INVOKE` (triggers LLM inference, expensive) and `MEMORY_UPDATE` (could pollute state) — are planned for v0.3. |
| **Unauthorized inference (resource theft)** | Medium | Distributed inference setup requires an `INFERENCE_SETUP` message, which only flows over authenticated, encrypted connections between household members. Non-household devices cannot trigger inference. | A household member could abuse shared compute resources within the household. Per-device compute quotas are future work. |
| **Supply chain / dependency compromise** | Low | Dependencies are pinned to version ranges in `pyproject.toml`. PyNaCl wraps libsodium (audited). The `noiseprotocol` package is pure Python implementing a formally-specified protocol. Built dashboard assets are committed and fingerprinted by Vite. | No vendored dependencies and no reproducible builds yet. A compromised `litellm` or `zeroconf` update could introduce vulnerabilities. Tighter pinning and dep audits are tracked for v0.3. |

---

## 3. Cryptographic primitives inventory

Every cryptographic operation in the codebase, with the exact algorithm and library backing it.

| Operation | Algorithm | Library | Location |
|---|---|---|---|
| Device / agent key generation | Ed25519 | PyNaCl (libsodium) | `src/aries/identity/keys.py` |
| Root key splitting | Shamir 2-of-3 over GF(256), AES polynomial 0x11B, generator 0x03 | In-tree (no external dep) | `src/aries/identity/keys.py` |
| DID encoding | did:key with multicodec prefix `0xED01` + base58btc | In-tree | `src/aries/identity/did.py` |
| UCAN signing / verification | Ed25519 (EdDSA) over JWT-formatted payload | In-tree + PyNaCl | `src/aries/identity/ucan.py` |
| Key file encryption (at rest) | Argon2id KDF (`moderate` ops/mem limits) + XSalsa20-Poly1305 SecretBox | PyNaCl | `src/aries/identity/keys.py` |
| Transport encryption | Noise_XX: X25519 ECDH + ChaCha20-Poly1305 AEAD + SHA-256 | `noiseprotocol` + PyNaCl | `src/aries/transport/crypto.py` |
| Content hashing | BLAKE3 (SHA-256 fallback) | `blake3` / `hashlib` | `src/aries/util.py`, `memory/store.py`, `receipt.py`, `continuation.py` |
| Receipt signing | Ed25519 detached signature over canonical-JSON body | PyNaCl | `src/aries/receipt.py` |
| Continuation signing | Ed25519 detached signature over the full-field hash of every envelope field except the signature itself | PyNaCl | `src/aries/continuation.py` |
| Pairing code | 6 words drawn from the first 256 entries of the BIP-39 English word list (~44 bits of entropy) | In-tree | `src/aries/identity/_wordlist.py` |

---

## 4. Known limitations in v0.2

These are deliberate scope decisions with documented future plans, not bugs.

1. **Pairing relies on physical proximity, not PAKE.** The 6-word code is transmitted out-of-band (spoken aloud or shown on a screen). A local observer who sees the code can join the household. Mitigation: pair in private. Fix: a password-authenticated key exchange (SPAKE2 or OPAQUE) is planned for v0.3.

2. **No per-peer rate limiting.** A malicious household member could flood the mesh with messages and degrade service for everyone. Fix: transport-layer rate limits per message type in v0.3.

3. **No defense against physical device access.** An attacker who can read the device's filesystem can extract the device key (if no passphrase is set) or accumulate Shamir shares. Mitigations: set a passphrase on key files at init time; store the backup Shamir share in a separate physical location (USB key in a drawer, paper, etc.).

4. **Transport encryption is Noise Protocol, not full DIDComm v2.** The current implementation authenticates and encrypts at the session level. DIDComm v2 provides per-message encryption with envelope-level authentication, which enables store-and-forward through untrusted relays. Fix: DIDComm v2 envelopes are planned for v0.3 alongside relay support.

5. **CRDT conflict resolution is LWW (last-writer-wins).** Conflicting writes are resolved by Lamport timestamp, with the lexicographically-larger device DID as a tie-breaker. This guarantees eventual consistency but does not preserve causal ordering of conversation turns. Fix: causality-aware merge (vector clocks per task) is on the roadmap for v0.4.

6. **Hidden state tensors are encrypted in transit but not at rest on participating devices.** During distributed inference, intermediate activations exist in process memory on each device. A local attacker with memory access could read them. Fix: trust-weighted layer placement (embedding + LM head pinned to the user's most-trusted device only) is planned for v0.3.

---

## 5. Reporting vulnerabilities

If you believe you have found a security issue, **do not** open a public GitHub issue. Instead, follow the procedure in [SECURITY.md](../SECURITY.md):

- Email **bb1231033@iitd.ac.in** cc **indranilbhadra.iitd@gmail.com** with the subject line `[aries-mesh security] <short description>`.
- Acknowledgement within 72 hours, target fix within 14 days (7 days for critical issues).
- Coordinated disclosure: we will agree on a public release date before any details are published.

We credit reporters in the advisory unless they request anonymity.
