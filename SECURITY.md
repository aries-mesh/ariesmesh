# Security Policy

## Reporting a vulnerability

**Do not open a public GitHub issue for security reports.**

Email **bb1231033@iitd.ac.in** cc **indranilbhadra.iitd@gmail.com** with the subject line `[aries-mesh security] <short description>`. You will receive a response within 72 hours acknowledging receipt. If you have not heard back after 5 business days, follow up in the same thread.

Please include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce or a proof-of-concept (can be a private Gist linked in the email).
- The affected version (run `aries --version` or check `CHANGELOG.md`).
- Your preferred credit line for the advisory (name, handle, or "anonymous").

We will coordinate a disclosure date with you. We aim to ship a fix within 14 days of a confirmed report; critical issues within 7 days.

---

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.1 (current) | Yes — receives security fixes. |
| 0.1.0 | No — upgrade to 0.1.1. |

---

## Scope

### In scope

- **Identity layer** — Ed25519 key management, Argon2id+SecretBox key-at-rest encryption, Shamir 2-of-3 secret sharing, did:key encoding.
- **UCAN tokens** — delegation-chain validation, capability attenuation, revocation propagation.
- **Continuation integrity** — Ed25519 envelope signing and verification; fields covered by the signature.
- **Transport** — length-prefixed CBOR over TCP; a peer impersonating another device; replay attacks.
- **Scheduler** — mandate bypass; locality enforcement circumvention.
- **Memory** — CRDT merge manipulation; unauthorized cross-device memory reads.
- **Pairing** — 6-word code interception or brute force; mDNS spoofing.

### Out of scope (known limitations — see below)

- Vulnerabilities in third-party dependencies (report upstream; we will update our pinned version promptly).
- Issues requiring local root access or physical device access — Aries Mesh is not designed to protect against a local privileged attacker.
- Denial-of-service attacks on the local LAN transport — v0.1 has no rate limiting.

---

## Known limitations (v0.1.1)

These are acknowledged weaknesses that are documented and tracked for v0.2:

| Area | Limitation | Mitigation until v0.2 |
|------|------------|----------------------|
| **Pairing** | The 6-word BIP39 code provides ~44 bits of entropy — sufficient against remote attackers but not against a local attacker who can read the terminal or spoof mDNS at the right moment. No PAKE, no key-confirmation round-trip. | Pair only on a trusted LAN. Do not display the code on a shared screen. |
| **Key at rest (default)** | `aries init` writes the device key as plaintext (version-1 file with `_warning: "plaintext"`). Protection is the OS user-profile ACL (`0600` POSIX / user profile dir Windows). | Pass `--passphrase` at init time to get Argon2id+SecretBox encryption. The three Shamir shares are also plaintext by default. |
| **Transport encryption** | TCP transport is unauthenticated and unencrypted at the socket level. Message authenticity relies on the Ed25519 envelope signature on `Continuation` and `Receipt`. Other message types are not signed. | Run only on a private LAN or VPN. WireGuard overlay is the recommended interim approach. |
| **Conversation-log causality** | `AppendLog.merge` uses timestamp-sorted union, which can reorder entries under concurrent writes from two devices with clock skew. | Deterministic but not causally ordered. Vector-clock ordering is planned for v0.2. |

---

## Cryptographic primitives summary

| Purpose | Algorithm | Implementation |
|---------|-----------|----------------|
| Device signing | Ed25519 | PyNaCl / libsodium |
| Key agreement | X25519 ECDH | PyNaCl / libsodium |
| Key-at-rest (passphrase) | Argon2id KDF + XSalsa20-Poly1305 | PyNaCl `argon2id.kdf` + `SecretBox` |
| Root-key backup | GF(2^8) Shamir 2-of-3 | In-tree, AES polynomial `0x11B`, generator `0x03` |
| Content hashing | BLAKE3 (SHA-256 fallback) | `blake3` library |
| Capability tokens | UCAN 1.0 / EdDSA | In-tree JWT implementation |
