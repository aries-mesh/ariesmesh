# Aries Mesh — Engineering Specification for Implementation Agents

**Version:** 0.1.0 | **Target:** ~4,700 LOC Python | **Build time estimate:** 3–5 agent sessions

> This document contains every detail a coding agent needs to build Aries Mesh from scratch: directory structure, every file with its exact classes/methods/fields, all algorithms with pseudocode, wire formats byte-by-byte, data flows between modules, and dependency configuration. No external context is needed.

---

## Table of Contents

1. [Project Setup & Dependencies](#1-project-setup--dependencies)
2. [Directory Structure & Module Map](#2-directory-structure--module-map)
3. [FILE: `pyproject.toml`](#3-file-pyprojecttoml)
4. [LAYER 0 — Identity: `src/aries/identity/keys.py`](#4-layer-0--identity-srcariesidentitykeyspy)
5. [LAYER 0 — Identity: `src/aries/identity/did.py`](#5-layer-0--identity-srcariesidentitydidpy)
6. [LAYER 0 — Identity: `src/aries/identity/ucan.py`](#6-layer-0--identity-srcariesidentityucanpy)
7. [LAYER 0 — Identity: `src/aries/identity/household.py`](#7-layer-0--identity-srcariesidentityhouseholdpy)
8. [LAYER 1 — Transport: `src/aries/transport/peer.py`](#8-layer-1--transport-srcariestransportpeerpy)
9. [LAYER 1 — Transport: `src/aries/transport/discovery.py`](#9-layer-1--transport-srcariestransportdiscoverypy)
10. [LAYER 2 — Scheduler: `src/aries/scheduler/router.py`](#10-layer-2--scheduler-srcariesschedulerrouterpy)
11. [LAYER 2 — Scheduler: `src/aries/scheduler/profile.py`](#11-layer-2--scheduler-srcariesschedulerprofilepy)
12. [LAYER 3 — Memory: `src/aries/memory/store.py`](#12-layer-3--memory-srcariesmemorypystorepy)
13. [LAYER 3 — Memory: `src/aries/memory/sync.py`](#13-layer-3--memory-srcariesmemorysynspy)
14. [ADAPTERS: `src/aries/adapters/base.py`](#14-adapters-srcariesadaptersbasepy)
15. [ADAPTERS: `src/aries/adapters/litellm_adapter.py`](#15-adapters-srcariesadapterslitellm_adapterpy)
16. [PROTOCOL: `src/aries/continuation.py`](#16-protocol-srcariescontinuationpy)
17. [PROTOCOL: `src/aries/receipt.py`](#17-protocol-srcariesreceiptpy)
18. [DAEMON: `src/aries/node.py`](#18-daemon-srcariesnodepy)
19. [CLI: `src/aries/cli/main.py`](#19-cli-srcariesclipy)
20. [Cross-Module Data Flow Diagrams](#20-cross-module-data-flow-diagrams)
21. [Constants & Magic Numbers Reference](#21-constants--magic-numbers-reference)
22. [Error Handling Contracts](#22-error-handling-contracts)
23. [Testing Strategy](#23-testing-strategy)

---

## 1. Project Setup & Dependencies

### Runtime requirements
- Python >= 3.11 (for `Self` type, `StrEnum`, improved `asyncio`)
- OS: macOS, Linux, Windows (WSL2 recommended). Android/iOS are future targets.

### Core dependencies (all required)

| Package | Version | Purpose |
|---------|---------|---------|
| `pynacl` | >= 1.5.0 | Ed25519 signing, X25519 ECDH (libsodium bindings) |
| `cbor2` | >= 5.6.0 | CBOR binary encoding/decoding for wire protocol |
| `zeroconf` | >= 0.131.0 | mDNS (DNS-SD) service advertisement and browsing |
| `click` | >= 8.1.0 | CLI framework |
| `httpx` | >= 0.27.0 | Async HTTP client for vendor API calls |
| `litellm` | >= 1.40.0 | Universal LLM adapter (100+ providers) |
| `pydantic` | >= 2.7.0 | Data model validation |
| `rich` | >= 13.7.0 | Pretty CLI output (tables, panels, trees) |
| `uvloop` | >= 0.19.0 | Fast event loop (Unix only, skip on Windows) |
| `psutil` | >= 5.9.0 | Cross-platform hardware profiling |
| `websockets` | >= 12.0 | WebSocket transport fallback |
| `blake3` | >= 0.4.0 | Fast content hashing (falls back to SHA-256 if unavailable) |

### Dev dependencies
- `pytest` >= 8.0, `pytest-asyncio` >= 0.23, `ruff` >= 0.4.0

### Install
```bash
pip install -e ".[dev]"
```

---

## 2. Directory Structure & Module Map

```
aries-mesh/
├── pyproject.toml
├── README.md
├── docs/
│   └── ARCHITECTURE.md
├── src/
│   └── aries/
│       ├── __init__.py                          # Empty
│       ├── node.py                              # [506 lines] Main daemon
│       ├── continuation.py                      # [228 lines] Hand-off envelope
│       ├── receipt.py                           # [200 lines] Signed audit trail
│       ├── identity/
│       │   ├── __init__.py                      # Empty
│       │   ├── keys.py                          # [271 lines] Ed25519 + Shamir SSS
│       │   ├── did.py                           # [107 lines] did:key encoding
│       │   ├── ucan.py                          # [388 lines] UCAN tokens + chain validation
│       │   └── household.py                     # [328 lines] Household state management
│       ├── transport/
│       │   ├── __init__.py                      # Empty
│       │   ├── peer.py                          # [269 lines] TCP connections + messages
│       │   └── discovery.py                     # [178 lines] mDNS advertisement/browsing
│       ├── scheduler/
│       │   ├── __init__.py                      # Empty
│       │   ├── router.py                        # [316 lines] Task routing + scoring
│       │   └── profile.py                       # [150 lines] Hardware profiler
│       ├── memory/
│       │   ├── __init__.py                      # Empty
│       │   ├── store.py                         # [425 lines] CRDT key-value store
│       │   └── sync.py                          # [187 lines] Two-phase sync protocol
│       ├── adapters/
│       │   ├── __init__.py                      # Empty
│       │   ├── base.py                          # [92 lines]  Abstract adapter interface
│       │   └── litellm_adapter.py               # [273 lines] Universal LLM adapter
│       └── cli/
│           ├── __init__.py                      # Empty
│           └── main.py                          # [346 lines] CLI commands
└── tests/
    ├── test_identity.py
    ├── test_ucan.py
    ├── test_scheduler.py
    ├── test_memory.py
    └── test_adapters.py
```

### Module dependency graph (import order — build bottom-up)

```
keys.py                          ← no internal deps (only pynacl)
did.py                           ← no internal deps
ucan.py                          ← imports did.py
household.py                     ← imports keys.py, did.py, ucan.py
peer.py                          ← no internal deps (only cbor2)
discovery.py                     ← imports peer.py (PeerInfo only)
router.py                        ← imports household.py (AgentRecord only)
profile.py                       ← imports router.py (DeviceHealth only)
store.py                         ← no internal deps (only cbor2, blake3)
sync.py                          ← imports store.py, peer.py
base.py (adapters)               ← no internal deps
litellm_adapter.py               ← imports base.py
continuation.py                  ← imports base.py (Message only)
receipt.py                       ← imports keys.py, did.py
node.py                          ← imports EVERYTHING
cli/main.py                      ← imports node.py, household.py, etc.
```

**Build order:** keys → did → ucan → household → peer → discovery → router → profile → store → sync → adapters/base → adapters/litellm → continuation → receipt → node → cli

---

## 3. FILE: `pyproject.toml`

```toml
[project]
name = "aries-mesh"
version = "0.1.0"
description = "A personal compute fabric for your device mesh"
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.11"
dependencies = [
    "pynacl>=1.5.0",
    "cbor2>=5.6.0",
    "zeroconf>=0.131.0",
    "click>=8.1.0",
    "httpx>=0.27.0",
    "litellm>=1.40.0",
    "pydantic>=2.7.0",
    "rich>=13.7.0",
    "uvloop>=0.19.0;platform_system!='Windows'",
    "psutil>=5.9.0",
    "websockets>=12.0",
    "blake3>=0.4.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "ruff>=0.4.0"]
semantic = ["sentence-transformers>=3.0.0", "numpy>=1.26.0"]

[project.scripts]
aries = "aries.cli.main:cli"

[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.backends._legacy:_Backend"

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
line-length = 100
target-version = "py311"
```

---

## 4. LAYER 0 — Identity: `src/aries/identity/keys.py`

### Purpose
Ed25519 key generation, signing/verification, Shamir 2-of-3 secret sharing, key storage.

### Imports
```python
from nacl.signing import SigningKey, VerifyKey
from nacl.public import PrivateKey as X25519PrivateKey, PublicKey as X25519PublicKey, Box
from nacl.encoding import RawEncoder
from nacl.exceptions import BadSignatureError
```

### Class: `KeyPair` (frozen dataclass)

| Field | Type | Description |
|-------|------|-------------|
| `signing_key` | `SigningKey` | NaCl Ed25519 signing key (init param) |
| `verify_key` | `VerifyKey` | Derived in `__post_init__` from `signing_key.verify_key` |

| Method | Signature | Logic |
|--------|-----------|-------|
| `generate()` | `classmethod → KeyPair` | `SigningKey.generate()` |
| `from_seed(seed: bytes)` | `classmethod → KeyPair` | Assert `len(seed) == 32`, then `SigningKey(seed)` |
| `from_bytes(raw: bytes)` | `classmethod → KeyPair` | `SigningKey(raw)` |
| `public_bytes` | `property → bytes` | `bytes(self.verify_key)` — raw 32-byte public key |
| `secret_bytes` | `property → bytes` | `bytes(self.signing_key)` — raw 32-byte secret |
| `sign(message)` | `bytes → bytes` | `self.signing_key.sign(message).signature` — 64-byte detached signature |
| `verify(message, signature)` | `bytes, bytes → bool` | `self.verify_key.verify(message, signature)`, catches `BadSignatureError` → False |
| `to_x25519_private()` | `→ X25519PrivateKey` | `self.signing_key.to_curve25519_private_key()` |
| `to_x25519_public()` | `→ X25519PublicKey` | `self.verify_key.to_curve25519_public_key()` |

### Function: `verify_detached(public_bytes, message, signature) → bool`
Standalone verification from raw 32-byte public key. Creates `VerifyKey(public_bytes)`, calls `.verify()`.

### Shamir Secret Sharing — GF(256) Implementation

**GF(256) arithmetic** (irreducible polynomial `0x11b`):
- Module-level: Initialize `_GF256_EXP[512]` and `_GF256_LOG[256]` lookup tables in `_init_gf256()`. Called at import time.
- `_gf256_mul(a, b)`: If either is 0 return 0, else `_GF256_EXP[_GF256_LOG[a] + _GF256_LOG[b]]`
- `_gf256_inv(a)`: `_GF256_EXP[255 - _GF256_LOG[a]]`

**`shamir_split(secret: bytes, n=3, k=2) → list[bytes]`**

```
for each byte in secret:
    coeffs = [byte_val, random(0..255)]  # k coefficients, coeffs[0] = secret byte
    for each share i (x = i+1):
        y = evaluate polynomial at x in GF(256) using Horner's method
        append y to share[i]
Each share = [x_coordinate_byte] + [y_values...]  (len = len(secret) + 1)
```

**`shamir_reconstruct(shares: list[bytes]) → bytes`**

```
xs = [share[0] for share in shares]  # x-coordinates
for each byte position:
    Lagrange interpolation at x=0 in GF(256):
    for each share i:
        basis = product(xj / (xi XOR xj)) for j != i, all in GF(256)
        val XOR= gf256_mul(yi, basis)
    result[byte_pos] = val
```

### Key Storage Functions

**`save_key_encrypted(key, path, passphrase=None)`**: Writes JSON `{"version": 1, "algorithm": "Ed25519", "secret_key_hex": hex(key.secret_bytes)}`. Sets file permission `0o600`.

**`load_key(path, passphrase=None) → KeyPair`**: Reads JSON, calls `KeyPair.from_bytes(bytes.fromhex(...))`.

### Function: `fingerprint(public_bytes, words=6) → str`
SHA-256 hash public key, convert to big integer, extract `words` indices mod 256 from a hardcoded 256-word list. Words are space-separated.

**Wordlist**: 256 curated words (alpha, anchor, apple, arrow, ... seed, shore, snow, song). Full list is in source.

---

## 5. LAYER 0 — Identity: `src/aries/identity/did.py`

### Purpose
Encode/decode Ed25519 public keys as `did:key:z6Mk...` URIs per the W3C did:key method.

### Constants
- `_ED25519_MULTICODEC = bytes([0xED, 0x01])` — varint prefix for Ed25519 in multicodec
- `_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"` — Bitcoin base58

### Base58 Implementation

**`_b58encode(data: bytes) → str`**: Convert bytes to big integer, repeatedly divmod by 58, index into alphabet. Preserve leading zero bytes as `'1'` characters.

**`_b58decode(s: str) → bytes`**: Reverse of encode. Count leading `'1'` chars, convert remaining to integer, prepend zero bytes.

### Core Functions

**`public_key_to_did(public_bytes: bytes) → str`**
```
assert len(public_bytes) == 32
multicodec_key = b'\xed\x01' + public_bytes     # 34 bytes
encoded = 'z' + base58btc(multicodec_key)        # 'z' = multibase prefix for base58btc
return f"did:key:{encoded}"
```
Result format: `did:key:z6Mk...` (the `6Mk` comes from base58-encoding `0xED01`)

**`did_to_public_key(did: str) → bytes`**
```
assert did.startswith("did:key:z")
encoded = did[len("did:key:z"):]
decoded = base58btc_decode(encoded)              # 34 bytes
assert decoded[:2] == b'\xed\x01'
return decoded[2:]                                # 32-byte public key
```

**`did_from_keypair(keypair) → str`**: Convenience wrapper.

**`did_short(did, chars=8) → str`**: `did[:16] + "..." + did[-chars:]` for display.

---

## 6. LAYER 0 — Identity: `src/aries/identity/ucan.py`

### Purpose
UCAN 1.0 implementation: JWT-based capability tokens with EdDSA signatures, delegation chains, and chain validation.

### Helper Functions
- `_b64url(data: bytes) → str`: Base64url encode, strip `=` padding
- `_b64url_decode(s: str) → bytes`: Restore padding, base64url decode

### Dataclass: `Capability` (frozen)

| Field | Type | Description |
|-------|------|-------------|
| `resource` | `str` | The `with` field — a DID or `aries://` URI or `*` |
| `ability` | `str` | The `can` field — e.g. `aries/agent.invoke` |

**`is_attenuated_by(parent: Capability) → bool`**: True if `self.ability == parent.ability` AND (`self.resource == parent.resource` OR `self.resource.startswith(parent.resource + "/")`)

### Seven defined abilities:
1. `aries/agent.invoke`
2. `aries/context.read`
3. `aries/context.write`
4. `aries/handoff.send`
5. `aries/handoff.accept`
6. `aries/identity.delegate`
7. `aries/household.member`

### Dataclass: `Caveat` (frozen)
Single field `conditions: dict[str, Any]`. Opaque constraints interpreted by verifier.

### Dataclass: `UCANToken`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `issuer` | `str` | required | `did:key` of issuer |
| `audience` | `str` | required | `did:key` of audience |
| `capabilities` | `list[Capability]` | required | Granted capabilities |
| `caveats` | `list[Caveat]` | `[]` | Additional constraints |
| `proofs` | `list[str]` | `[]` | CIDs of parent UCANs |
| `facts` | `dict[str, Any]` | `{}` | Arbitrary claims |
| `not_before` | `float` | `time.time()` | Activation timestamp |
| `expiration` | `float` | `0.0` | Expiry (0 = never) |
| `nonce` | `str` | `uuid4().hex[:16]` | Replay protection |
| `_raw_token` | `Optional[str]` | `None` | Set after signing |
| `_signature` | `Optional[bytes]` | `None` | Set after signing |

| Property/Method | Logic |
|-----------------|-------|
| `is_expired` | `expiration > 0 and time.time() > expiration` |
| `is_active` | `time.time() >= not_before and not is_expired` |
| `cid` | `"ucan:" + SHA256(raw_token)[:32]` |

**JWT structure:**
- Header: `{"alg": "EdDSA", "typ": "JWT", "ucv": "1.0"}`
- Payload: `{iss, aud, nbf, nnc, cap, [exp], [cav], [prf], [fct]}`
- Signature: Ed25519 over `base64url(header).base64url(payload)`

**`sign(signing_key: SigningKey) → str`**: Builds JWT string `header.payload.signature`. All JSON uses `separators=(",", ":")` (compact).

**`decode(token: str) → UCANToken`** (classmethod): Splits on `.`, base64url-decodes, parses JSON. No signature verification.

**`verify(token: str) → UCANToken`** (classmethod): Calls `decode()`, then extracts issuer's public key via `did_to_public_key(ucan.issuer)`, creates `VerifyKey`, verifies signature.

### Class: `UCANStore`

Internal dict: `_tokens: dict[str, str]` mapping CID → raw JWT string.

**`store(token_str) → str`**: Decode to get CID, store, return CID.

**`validate_chain(token_str, expected_root_did, required_capability=None, revocation_list=None) → bool`**

```
1. Verify signature on token
2. Check is_active (not expired, past nbf)
3. Check issuer and audience not in revocation_list
4. If required_capability: check at least one token capability satisfies it via is_attenuated_by
5. If no proofs: assert issuer == expected_root_did (chain terminus)
6. For each proof CID:
   a. Lookup proof token in store
   b. Assert proof.audience == token.issuer (delegation linkage)
   c. Assert token capabilities are attenuated by proof capabilities
   d. Recursively validate_chain on the proof token
```

### Builder Functions

**`build_household_membership(user_root_key, user_root_did, device_did) → str`**
Issues UCAN from root to device with ALL capabilities, no expiration:
- `aries/household.member`, `aries/identity.delegate`, `aries/context.read`, `aries/context.write`, `aries/agent.invoke`, `aries/handoff.send`, `aries/handoff.accept`
- Facts: `{"household": user_root_did, "role": "device"}`

**`build_agent_token(device_key, device_did, agent_did, capabilities, ttl_seconds=86400, parent_proof_cid=None, caveats=None) → str`**
Issues time-scoped UCAN from device to agent. Includes proof reference to parent (membership UCAN CID).

---

## 7. LAYER 0 — Identity: `src/aries/identity/household.py`

### Purpose
Manages the household: first device initialization, device pairing, agent registration, revocation, persistence.

### Dataclass: `DeviceRecord`

| Field | Type | Description |
|-------|------|-------------|
| `device_did` | `str` | `did:key:z6Mk...` |
| `name` | `str` | Human-readable (e.g., "macbook-pro") |
| `platform` | `str` | `"macos"`, `"linux"`, `"windows"`, `"android"`, `"ios"` |
| `paired_at` | `float` | Unix timestamp |
| `membership_ucan` | `str` | Raw JWT string |
| `is_self` | `bool` | True for this device |

### Dataclass: `AgentRecord`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `agent_did` | `str` | | Ephemeral `did:key` |
| `name` | `str` | | Human name, e.g. "ollama/qwen3:32b" |
| `vendor` | `str` | | "ollama", "anthropic", "openai", "google", "custom" |
| `model` | `Optional[str]` | `None` | Model identifier |
| `capabilities` | `list[str]` | `[]` | e.g. ["text.qa", "code.generate"] |
| `context_window` | `int` | `0` | Token capacity |
| `locality` | `str` | `"local"` | "local" or "cloud-routed" |
| `cost_class` | `str` | `"free"` | "free", "metered", "paid" |
| `registered_at` | `float` | `time.time()` | |
| `ucan_token` | `Optional[str]` | `None` | Agent's UCAN JWT |
| `pid` | `Optional[int]` | `None` | OS process ID |

### Dataclass: `RevocationEntry`
Fields: `revoked_did`, `revoked_at`, `signed_by`, `reason`.

### Class: `Household`

**Constructor**: Takes `data_dir: Path`. Creates directory. Initializes empty dicts for devices, agents, revocation_list. Holds `_user_root_key`, `_device_key`, `user_root_did`, `device_did`, `_membership_ucan`.

**`is_initialized` property**: Checks if `household.json` exists.

**`initialize(device_name, platform) → dict`**:
```
1. Generate user_root_key = KeyPair.generate()
2. user_root_did = public_key_to_did(root_key.public_bytes)
3. Generate device_key = KeyPair.generate()
4. device_did = public_key_to_did(device_key.public_bytes)
5. Shamir-split root key into 3 shares, write to root_share_{1,2,3}.bin
6. Save device key to device_key.json (0600 permissions)
7. Issue membership UCAN: build_household_membership(root_signing_key, root_did, device_did)
8. Store UCAN in UCANStore
9. Create DeviceRecord(is_self=True), add to self.devices
10. Persist to household.json
11. Return {user_root_did, device_did, household_tag, device_name}
```

**`load()`**: Read `household.json`, restore `user_root_did`, `device_did`, load `device_key.json`, reconstruct `devices` and `agents` dicts, reload UCANs into store.

**`register_agent(name, vendor, model, capabilities, context_window, locality, cost_class, pid) → AgentRecord`**:
```
1. Generate ephemeral agent_key = KeyPair.generate()
2. agent_did = public_key_to_did(agent_key.public_bytes)
3. Build UCAN capabilities: agent.invoke, context.read, context.write (all with resource="*" or "aries:context://*")
4. Get membership UCAN CID for proof chain
5. Issue agent token: build_agent_token(device_signing_key, device_did, agent_did, caps, parent_proof_cid=membership_cid)
6. Create AgentRecord, add to self.agents
7. Persist
```

**`revoke(did, reason)`**: Add `RevocationEntry`, remove from agents/devices, persist.

**`_household_tag() → str`**: `SHA256(user_root_did)[:16]` — privacy-preserving mDNS identifier.

**`_save()`**: Write `household.json` with version, root_did, device_did, devices, agents, revocation_list.

### Persistence format (`~/.aries/household/household.json`)
```json
{
  "version": 1,
  "user_root_did": "did:key:z6Mk...",
  "device_did": "did:key:z6Mk...",
  "household_tag": "a3f7b2c901d4e8f6",
  "devices": { "did:key:...": { DeviceRecord fields } },
  "agents": { "did:key:...": { AgentRecord fields } },
  "revocation_list": [ { RevocationEntry fields } ]
}
```

---

## 8. LAYER 1 — Transport: `src/aries/transport/peer.py`

### Purpose
Message envelope, TCP peer connections, transport server.

### Dataclass: `AriesMessage`

| Field | Type | Default | Wire key |
|-------|------|---------|----------|
| `type` | `str` | required | `@type` |
| `id` | `str` | `uuid4().hex` | `@id` |
| `sender_did` | `str` | `""` | `sender` |
| `thread_id` | `Optional[str]` | `None` | `@thread` |
| `body` | `dict` | `{}` | `body` |
| `timestamp` | `float` | `time.time()` | `ts` |
| `seq` | `int` | `0` | `seq` |

**`to_cbor() → bytes`**: `cbor2.dumps({"@type": ..., "@id": ..., ...})`

**`from_cbor(data) → AriesMessage`**: `cbor2.loads(data)`, construct from dict.

**`to_bytes() → bytes`**: `struct.pack("!I", len(payload)) + payload` — 4-byte big-endian length prefix + CBOR payload.

### Class: `MessageTypes` — String constants

```
ANNOUNCE            = "aries/v0.1/discovery/announce"
QUERY               = "aries/v0.1/discovery/query"
RESULT              = "aries/v0.1/discovery/result"
INVOKE              = "aries/v0.1/agent/invoke"
INVOKE_RESULT       = "aries/v0.1/agent/result"
ERROR               = "aries/v0.1/agent/error"
CONTINUATION        = "aries/v0.1/handoff/continuation"
ACK                 = "aries/v0.1/handoff/ack"
RECEIPT             = "aries/v0.1/handoff/receipt"
REVOCATION_APPEND   = "aries/v0.1/revocation/append"
REVOCATION_SYNC     = "aries/v0.1/revocation/sync"
MEMORY_SYNC         = "aries/v0.1/memory/sync"
MEMORY_UPDATE       = "aries/v0.1/memory/update"
HEARTBEAT           = "aries/v0.1/health/heartbeat"
PROFILE_UPDATE      = "aries/v0.1/health/profile"
```

### Dataclass: `PeerInfo`
Fields: `device_did`, `name`, `host`, `port`, `household_tag`, `capabilities: list[str]`, `last_seen: float`, `latency_ms: Optional[float]`.

### Type alias
`MessageHandler = Callable[[AriesMessage, PeerConnection], Coroutine[Any, Any, None]]`

### Class: `PeerConnection`

Wraps `asyncio.StreamReader/StreamWriter` for a single peer.

| Field | Type |
|-------|------|
| `peer` | `PeerInfo` |
| `reader` | `asyncio.StreamReader` |
| `writer` | `asyncio.StreamWriter` |
| `_seq` | `int` (per-connection counter) |
| `_connected` | `bool` |

**`send(msg)`**: Increment `_seq`, set `msg.seq`, call `msg.to_bytes()`, write to stream, drain.

**`recv() → Optional[AriesMessage]`**:
```
header = readexactly(4)
length = unpack("!I", header)
if length > 10MB: reject
payload = readexactly(length)
return AriesMessage.from_cbor(payload)
```
Returns `None` on `IncompleteReadError` or `ConnectionError`.

**`close()`**: Set `_connected = False`, close writer.

### Class: `TransportServer`

| Field | Type |
|-------|------|
| `host` | `str` (default `"0.0.0.0"`) |
| `port` | `int` (default `0` = auto-assign) |
| `_server` | `asyncio.Server` |
| `_handlers` | `dict[str, MessageHandler]` |
| `_connections` | `dict[str, PeerConnection]` (did → conn) |

**`on_message(msg_type, handler)`**: Register handler.

**`start() → int`**: `asyncio.start_server(self._handle_connection, host, port)`. Returns actual port.

**`connect_to_peer(peer: PeerInfo) → PeerConnection`**: `asyncio.open_connection(peer.host, peer.port)`, wraps in `PeerConnection`, starts `_receive_loop` as background task.

**`get_peer(device_did) → Optional[PeerConnection]`**: Lookup by DID.

**`broadcast(msg)`**: Send to all connected peers, catch exceptions per-peer.

**`_handle_connection(reader, writer)`**: Create `PeerConnection` with `device_did="unknown"`, start `_receive_loop`.

**`_receive_loop(conn)`**:
```
while connected:
    msg = conn.recv()
    if None: break
    if msg.sender_did and conn.peer.device_did == "unknown":
        conn.peer.device_did = msg.sender_did
        register connection by DID
    dispatch to registered handler for msg.type
```

---

## 9. LAYER 1 — Transport: `src/aries/transport/discovery.py`

### Purpose
mDNS service advertisement and peer discovery using `zeroconf`.

### Constants
- `SERVICE_TYPE = "_aries._tcp.local."`

### Class: `DiscoveryService`

**Constructor params:** `device_did`, `device_name`, `household_tag`, `port`, `capabilities=[]`

**Internal state:** `_zc: AsyncZeroconf`, `_browser: AsyncServiceBrowser`, `_service_info: ServiceInfo`, `peers: dict[str, PeerInfo]`, callbacks `_on_peer_found`, `_on_peer_lost`.

**`start()`**:
```
1. Create AsyncZeroconf()
2. Get local IP via _get_local_ip()
3. Build ServiceInfo:
   - type: "_aries._tcp.local."
   - name: f"aries-{device_name}._aries._tcp.local."
   - address: inet_aton(local_ip)
   - port: self.port
   - properties (TXT records, all bytes):
     did = device_did
     household = household_tag
     proto = "v0.1"
     cap = comma-separated capabilities
     name = device_name
4. Register service: async_register_service()
5. Start browser: AsyncServiceBrowser(zeroconf, SERVICE_TYPE, handler=_on_service_state_change)
```

**`_on_service_state_change(zeroconf, service_type, name, state_change)`**:
- `Added`: schedule `_resolve_service()`
- `Removed`: find peer by name in `self.peers`, remove, call `_on_peer_lost`

**`_resolve_service(zeroconf, service_type, name)`**:
```
1. Create AsyncServiceInfo, request with 3000ms timeout
2. Parse decoded_properties: did, household, name, cap
3. Filter: skip if household != self.household_tag (different household)
4. Filter: skip if did == self.device_did (self)
5. Get first parsed_scoped_address and port
6. Create PeerInfo, store in self.peers
7. Call _on_peer_found callback
```

**`_get_local_ip() → str`**: Opens UDP socket to 8.8.8.8:80 (doesn't send data), reads `getsockname()[0]`. Fallback: `"127.0.0.1"`.

**`stop()`**: Cancel browser, unregister service, close zeroconf.

---

## 10. LAYER 2 — Scheduler: `src/aries/scheduler/router.py`

### Purpose
Four-stage scheduling pipeline: Filter → Mandate → Score → Select.

### Enum: `Locality`
`LOCAL_ONLY = "local-only"`, `HOUSEHOLD = "household"`, `ANY = "any"`

### Dataclass: `TaskConstraints`

| Field | Type | Default |
|-------|------|---------|
| `capability` | `str` | required |
| `locality` | `Locality` | `HOUSEHOLD` |
| `vendor_preference` | `list[str]` | `[]` |
| `vendor_exclude` | `list[str]` | `[]` |
| `min_context_window` | `int` | `0` |
| `max_cost_class` | `str` | `"paid"` |
| `max_latency_ms` | `Optional[int]` | `None` |
| `tags` | `list[str]` | `[]` |

### Dataclass: `DeviceHealth`

| Field | Type | Default |
|-------|------|---------|
| `device_did` | `str` | required |
| `cpu_percent` | `float` | `0.0` |
| `ram_available_gb` | `float` | `0.0` |
| `ram_total_gb` | `float` | `0.0` |
| `gpu_utilization` | `float` | `0.0` |
| `vram_available_gb` | `float` | `0.0` |
| `battery_pct` | `Optional[float]` | `None` (None = desktop/plugged) |
| `charging` | `bool` | `True` |
| `thermal` | `str` | `"nominal"` |
| `network_type` | `str` | `"wifi"` |
| `bandwidth_mbps` | `float` | `0.0` |
| `last_updated` | `float` | `time.time()` |

**`health_score` property (0.0–1.0)**:
```
score = 1.0
if cpu > 80%: score *= 0.5  elif cpu > 50%: score *= 0.8
if battery < 10% and not charging: score *= 0.1
  elif battery < 30%: score *= 0.5
  elif battery < 50%: score *= 0.8
if thermal == "throttled": score *= 0.3  elif "warm": score *= 0.7
```

### Dataclass: `ScoringWeights`
`privacy=3.0`, `capability=2.0`, `latency=1.5`, `cost=1.0`, `health=1.0`

### Constants
```python
COST_RANK = {"free": 1.0, "metered": 0.5, "paid": 0.2}
LOCALITY_RANK = {"local": 1.0, "cloud-routed": 0.2}
```

### Dataclass: `Mandate`
Fields: `name`, `when_tags: list[str]`, `when_time: Optional[str]` (format "HH:MM-HH:MM"), `is_default: bool`, `enforce_locality: Optional[str]`, `enforce_cost_class: Optional[str]`, `enforce_max_tokens: Optional[int]`, `scoring_overrides: Optional[ScoringWeights]`.

### Class: `Scheduler`

**State:** `weights: ScoringWeights`, `mandates: list[Mandate]`, `_device_health: dict[str, DeviceHealth]`

**`select_agent(agents, constraints, device_did_map=None) → Optional[tuple[AgentRecord, float]]`**:
```
1. effective_constraints = _apply_mandates(constraints)
2. candidates = _filter(agents, effective_constraints)
3. For each candidate: score = _score(agent, constraints, health)
4. Sort descending by score
5. Return (top_agent, score) or None
```

**`_filter(agents, constraints) → list[AgentRecord]`**:
Checks each agent against: capability in agent.capabilities, locality constraint, vendor_preference, vendor_exclude, min_context_window, max_cost_class (using cost_order index comparison).

**`_score(agent, constraints, health) → float`**:
```
privacy = LOCALITY_RANK[agent.locality]           # local=1.0, cloud=0.2
capability = min(agent.context_window / 200000, 1.0)
cost = COST_RANK[agent.cost_class]                # free=1.0, paid=0.2
health_score = health.health_score if health else 0.7
latency = 1.0 if local else 0.5

total = w.privacy*privacy + w.capability*cap + w.latency*latency + w.cost*cost + w.health*health_score
return total / (w.privacy + w.capability + w.latency + w.cost + w.health)
```

**`_apply_mandates(constraints) → TaskConstraints`**: Deep-copy constraints, iterate mandates, apply overrides from matching mandates.

**`_mandate_applies(mandate, constraints) → bool`**: True if `is_default`, or any `when_tags` match `constraints.tags`, or current time is within `when_time` range.

---

## 11. LAYER 2 — Scheduler: `src/aries/scheduler/profile.py`

### Purpose
Hardware monitoring via `psutil`, periodic snapshots every 10s.

### Constants
- `PROFILE_INTERVAL_S = 10`

### Class: `DeviceProfiler`

**Constructor:** `device_did: str`. Internal: `_running`, `_task`, `_latest: DeviceHealth`, `_on_update: list`.

**`snapshot() → DeviceHealth`**:
```
cpu = psutil.cpu_percent(interval=0.1)
mem = psutil.virtual_memory()
battery = psutil.sensors_battery()  # None on desktops
thermal: try psutil.sensors_temperatures() — max temp > 90 = "throttled", > 75 = "warm"
network: iterate psutil.net_if_stats() — look for "eth" = ethernet, "wlan"/"wi-fi" = wifi
bandwidth: max NIC speed from net_if_stats
GPU: on macOS, use mem.available as vram (unified memory)
Return DeviceHealth with all fields
```

**`static_info() → dict`**: Platform, arch, CPU model/cores/threads, RAM, hostname, Python version, disk total/free. Uses `platform` and `shutil.disk_usage("/")`.

**`start()`**: Take immediate snapshot, start `_loop()` as asyncio task.

**`_loop()`**: Every `PROFILE_INTERVAL_S` seconds, take snapshot, notify callbacks.

---

## 12. LAYER 3 — Memory: `src/aries/memory/store.py`

### Purpose
CRDT-backed distributed key-value store with three namespaces.

### Enum: `Namespace(str, Enum)`
`CONTEXT = "context"`, `MEMORY = "memory"`, `CACHE = "cache"`

**`from_key(key: str) → tuple[Namespace, str]`**: Parse `"context://tasks/abc"` into `(Namespace.CONTEXT, "tasks/abc")`. Raises if no prefix matches.

### Dataclass: `LWWEntry` (Last-Writer-Wins Register)

| Field | Type | Description |
|-------|------|-------------|
| `value` | `Any` | The stored value |
| `timestamp` | `float` | Lamport timestamp (NOT wall clock) |
| `device_did` | `str` | Writer's DID (tie-breaker) |
| `wall_clock` | `float` | `time.time()` — for TTL expiry |
| `content_hash` | `str` | BLAKE3 or SHA-256 hash of JSON-serialized value, truncated to 16 hex chars |
| `ttl` | `Optional[float]` | Seconds until expiry (None = permanent) |

**`is_expired`**: `ttl is not None and time.time() > wall_clock + ttl`

**`supersedes(other) → bool`**: Higher timestamp wins. On tie, lexicographically larger `device_did` wins.

**Serialization:** `to_dict()`, `from_dict()`, `to_cbor()`, `from_cbor()`.

### Dataclass: `AppendLog`

Field: `entries: list[dict]`

**`append(entry, device_did) → int`**: Adds `_seq` (current length), `_device`, `_ts` to entry dict. Returns index.

**`merge(remote_entries)`**: Deduplicate by `(_device, _seq)` tuple. Sort all entries by `_ts`.

**`since(index) → list[dict]`**: `self.entries[index:]`

### Constants: `DEFAULT_TTLS`
```python
{Namespace.CONTEXT: 86400, Namespace.MEMORY: None, Namespace.CACHE: 3600}
```

### Class: `MemoryStore`

**Constructor:** `device_did: str, persist_dir: Optional[Path]`. Internal state:
- `_lamport_clock: float = 0.0`
- `_registers: dict[str, LWWEntry]` (full key → entry)
- `_logs: dict[str, AppendLog]` (full key → log)
- `_on_change: list[Callable]`

**Lamport clock logic:**
- `_tick() → float`: Increment by 1, return new value.
- `_update_clock(remote_ts)`: `max(local, remote) + 1`

**Read/Write API:**

| Method | Logic |
|--------|-------|
| `get(key) → Optional[Any]` | Lookup in `_registers`. Return `None` if missing or expired (delete expired). |
| `get_entry(key) → Optional[LWWEntry]` | Same but returns full entry with metadata. |
| `set(key, value, ttl=None) → LWWEntry` | Parse namespace from key, default TTL if not given. Tick clock. Create `LWWEntry`. Check `supersedes` against existing. Notify + persist. |
| `delete(key)` | `self.set(key, None)` — tombstone. |
| `keys(prefix) → list[str]` | Filter `_registers` by prefix, exclude expired and None values. |
| `list_namespace(ns) → list[str]` | `self.keys(f"{ns.value}://")` |

**Append log API:**

| Method | Logic |
|--------|-------|
| `log_append(key, entry) → int` | Create `AppendLog` if needed, append, notify, persist. |
| `log_read(key, since=0) → list[dict]` | `log.since(since)` |

**Merge API (for sync):**

| Method | Logic |
|--------|-------|
| `merge_entry(key, remote_entry)` | Update Lamport clock, apply if `supersedes` existing. |
| `merge_log(key, remote_entries)` | Create `AppendLog` if needed, call `log.merge()`. |

**Sync state:**

**`get_sync_state() → dict`**:
```json
{
  "registers": { "key": {"ts": float, "hash": str, "device": str} },
  "logs": { "key": int_entry_count },
  "clock": float
}
```

**`compute_diff(remote_state) → dict`**:
```
For each local register key:
  if remote missing: include in diff
  if local timestamp > remote timestamp: include
  if same timestamp but different hash: include
For each local log key:
  if local entry count > remote count: include entries[remote_count:]
Return {"registers": {...}, "logs": {...}}
```

**`apply_diff(diff)`**: Iterate registers → `merge_entry()`. Iterate logs → `merge_log()`.

**Persistence:** `_save()` writes `memory.json` (clock + registers + logs). `_load()` reads it back.

---

## 13. LAYER 3 — Memory: `src/aries/memory/sync.py`

### Purpose
Two-phase sync protocol over transport layer.

### Constants
- `SYNC_DEBOUNCE_MS = 100`
- `SYNC_INTERVAL_S = 30`

### Class: `MemorySyncService`

**Constructor:** `store: MemoryStore, transport: TransportServer, device_did: str`

On init, registers message handlers on transport:
- `MessageTypes.MEMORY_SYNC` → `_handle_sync_request`
- `MessageTypes.MEMORY_UPDATE` → `_handle_update`

Also registers `store.on_change(self._on_local_change)`.

**`sync_with_peer(peer_conn)`**: Send `MEMORY_SYNC` with `phase="request"` and local `get_sync_state()`.

**`_handle_sync_request(msg, conn)`**:
- Phase `"request"`: Compute diff from remote state, send `MEMORY_SYNC` response with `phase="response"`, our diff, and our state.
- Phase `"response"`: Apply received diff. Compute our diff from their state. If non-empty, send `MEMORY_UPDATE`.

**`_handle_update(msg, conn)`**: Apply diff from `msg.body["diff"]`.

**`_on_local_change(key, value)`**: Debounce 100ms, then call `_push_to_peers()` which broadcasts a `MEMORY_SYNC` request to all peers.

**`_periodic_sync()`**: Every 30s, broadcast `MEMORY_SYNC` request.

---

## 14. ADAPTERS: `src/aries/adapters/base.py`

### Purpose
Abstract base class for vendor adapters.

### Dataclass: `Message`
Fields: `role: str` ("user"/"assistant"/"system"), `content: str`, `name: Optional[str]`, `timestamp: float`.
Methods: `to_dict()`, `from_dict()`.

### Dataclass: `InvokeRequest`
Fields: `messages: list[Message]`, `system_prompt: Optional[str]`, `max_tokens: int = 4096`, `temperature: float = 0.7`, `stop_sequences: list[str]`, `stream: bool`, `metadata: dict`.

### Dataclass: `InvokeResponse`
Fields: `content: str`, `model: str`, `finish_reason: str`, `usage: dict` (prompt_tokens, completion_tokens), `latency_ms: float`, `metadata: dict`.
Property: `total_tokens → int`.

### ABC: `BaseAdapter`
Class vars: `vendor: str`, `model: str`.
Abstract methods: `invoke(request) → InvokeResponse`, `invoke_stream(request) → AsyncIterator[str]`, `health_check() → bool`, `capabilities() → dict`.

---

## 15. ADAPTERS: `src/aries/adapters/litellm_adapter.py`

### Purpose
Universal LLM adapter via `litellm`.

### Class: `LiteLLMAdapter(BaseAdapter)`

**Constructor:** `model, api_key, api_base, vendor, context_window, cost_class, locality, custom_capabilities`

`vendor` auto-inferred from model string if not provided:
- Starts with `"ollama/"` → `"ollama"`
- Contains `"claude"` or starts with `"anthropic/"` → `"anthropic"`
- Contains `"gpt"`, `"o1"`, `"o3"` → `"openai"`
- Contains `"gemini"` → `"google"`
- Starts with `"openai/"` → `"openai-compatible"`

**`invoke(request) → InvokeResponse`**: Build messages list (prepend system_prompt if present), call `litellm.acompletion(**kwargs)`, measure latency.

**`invoke_stream(request) → AsyncIterator[str]`**: Same but `stream=True`, yield `chunk.choices[0].delta.content`.

**`health_check() → bool`**: Send `"ping"` with `max_tokens=5`, return True if non-empty response.

**`capabilities() → dict`**: Returns `{vendor, model, capabilities, context_window, locality, cost_class}`.

### Convenience Constructors

| Function | Model default | Locality | Cost |
|----------|--------------|----------|------|
| `ollama_adapter(model="qwen3:32b", api_base="http://localhost:11434")` | ollama/{model} | local | free |
| `anthropic_adapter(model="claude-sonnet-4-20250514")` | as-is | cloud-routed | paid |
| `openai_adapter(model="gpt-4o")` | as-is | cloud-routed | paid |
| `google_adapter(model="gemini/gemini-2.5-flash")` | as-is | cloud-routed | metered |
| `custom_adapter(model, api_base)` | openai/{model} | local | free |

---

## 16. PROTOCOL: `src/aries/continuation.py`

### Purpose
Continuation envelope for cross-device task hand-off.

### Dataclass: `Resource`
Fields: `type` ("file"/"embedding"/"tool_output"/"memory_ref"), `uri`, `content: Optional[str]`, `hash: Optional[str]`, `mime_type: str`.

### Dataclass: `HandoffReason`
Fields: `code` (one of: "user_request", "privacy_upgrade", "capability_need", "battery_low", "cost_limit", "model_switch"), `description: str`, `user_initiated: bool`.

### Dataclass: `Continuation`

| Field | Type | Default |
|-------|------|---------|
| `id` | `str` | `"cont_" + uuid4().hex[:12]` |
| `task_id` | `str` | `""` |
| `thread_id` | `Optional[str]` | `None` |
| `source_device_did` | `str` | `""` |
| `source_agent_did` | `str` | `""` |
| `target_device_did` | `Optional[str]` | `None` (scheduler decides) |
| `target_agent_did` | `Optional[str]` | `None` |
| `system_prompt` | `Optional[str]` | `None` |
| `messages` | `list[Message]` | `[]` |
| `summary` | `Optional[str]` | `None` |
| `resources` | `list[Resource]` | `[]` |
| `memory_keys` | `list[str]` | `[]` |
| `metadata` | `dict` | `{}` |
| `ucan_chain` | `list[str]` | `[]` |
| `required_capabilities` | `list[str]` | `[]` |
| `locality_preference` | `str` | `"any"` |
| `max_cost_class` | `str` | `"paid"` |
| `reason` | `Optional[HandoffReason]` | `None` |
| `created_at` | `float` | `time.time()` |
| `expires_at` | `Optional[float]` | `None` |

**`content_hash`**: SHA-256 of JSON(id, messages, system_prompt, resources)[:16].

Serialization: `to_dict()`, `from_dict()`, `to_cbor()`, `from_cbor()`, `to_json()`.

### Builder: `build_continuation(...) → Continuation`
Convenience function that sets all fields and computes `expires_at = time.time() + ttl_seconds`.

---

## 17. PROTOCOL: `src/aries/receipt.py`

### Purpose
Signed execution receipts forming hash-linked audit chains.

### Dataclass: `Receipt`

| Field | Type |
|-------|------|
| `id` | `str` (`"rcpt_" + uuid4().hex[:12]`) |
| `task_id` | `str` |
| `continuation_id` | `Optional[str]` |
| `device_did` | `str` |
| `agent_did` | `str` |
| `action` | `str` ("invoke", "handoff_sent", "handoff_received", "completed") |
| `status` | `str` ("success", "error", "partial") |
| `model_used` | `str` |
| `tokens_used` | `int` |
| `latency_ms` | `float` |
| `input_hash` | `str` |
| `output_hash` | `str` |
| `summary` | `str` |
| `previous_receipt_id` | `Optional[str]` |
| `previous_receipt_hash` | `Optional[str]` |
| `started_at` | `float` |
| `completed_at` | `float` |
| `signature` | `Optional[str]` (hex-encoded Ed25519 sig) |
| `signed_by` | `Optional[str]` (DID) |

**`content_hash`**: SHA-256 of JSON(_signable_content()) — excludes signature fields.

**`sign(keypair, signer_did) → Receipt`**: Sign `_signable_content()` JSON bytes with `keypair.sign()`, store hex signature.

**`verify() → bool`**: Extract public key from `signed_by` DID via `did_to_public_key()`, call `verify_detached()`.

### Class: `ReceiptChain`

**`add(receipt) → Receipt`**: Link to previous: set `previous_receipt_id` and `previous_receipt_hash`. Append.

**`verify_chain() → bool`**: For each receipt: verify signature, verify hash link to predecessor.

---

## 18. DAEMON: `src/aries/node.py`

### Purpose
Main daemon that ties all layers together into one per-device runtime.

### Class: `AriesNode`

**Constructor:** `data_dir: str | Path = "~/.aries"`. Initializes all component references to `None`.

**Internal state:**
- `household: Household`, `transport: TransportServer`, `discovery: DiscoveryService`
- `scheduler: Scheduler`, `profiler: DeviceProfiler`
- `memory: MemoryStore`, `sync: MemorySyncService`
- `_adapters: dict[str, LiteLLMAdapter]` (agent_did → adapter)
- `_receipt_chains: dict[str, ReceiptChain]` (task_id → chain)

### Method: `initialize(device_name, platform) → dict`
Creates `Household`, calls `household.initialize()`.

### Method: `start()`
```
1. Load household from disk
2. Start TransportServer → get port
3. Register message handlers: INVOKE → _handle_invoke, CONTINUATION → _handle_continuation, HEARTBEAT → _handle_heartbeat, PROFILE_UPDATE → _handle_profile_update
4. Compute household_tag = SHA256(user_root_did)[:16]
5. Start DiscoveryService with tag and port
6. Register discovery callback: _on_peer_discovered
7. Start DeviceProfiler, register health callback: _on_health_update
8. Create Scheduler
9. Create MemoryStore with persist_dir
10. Create MemorySyncService, start it
```

### Method: `register_agent(adapter, name=None) → AgentRecord`
Gets capabilities from adapter, calls `household.register_agent()`, stores adapter by agent_did.

### Method: `invoke(messages, capability, system_prompt, agent_did, locality, tags) → InvokeResponse`
```
1. If agent_did specified: lookup adapter directly
   Else: list agents, build TaskConstraints, call scheduler.select_agent()
2. Generate task_id
3. Store request in memory: context://tasks/{id}/request
4. Create InvokeRequest, call adapter.invoke()
5. Store response in memory: context://tasks/{id}/response
6. Append all messages to log: context://tasks/{id}/history
7. Create Receipt(action="invoke"), add to chain
8. Store receipt in memory
9. Return response
```

### Method: `handoff(task_id, reason, target_device_did, target_locality, required_capabilities) → Continuation`
```
1. Read conversation history from memory log
2. Build Continuation envelope via build_continuation()
3. Create Receipt(action="handoff_sent")
4. Wrap in AriesMessage(type=CONTINUATION)
5. If target specified: send to that peer
   Else: broadcast to all peers
6. Return Continuation
```

### Message Handlers

**`_handle_invoke(msg, conn)`**: Extract messages/capability from body, call `self.invoke()`, send INVOKE_RESULT or ERROR back.

**`_handle_continuation(msg, conn)`**: Deserialize Continuation, store messages in local memory, create "handoff_received" receipt, send ACK.

**`_handle_heartbeat(msg, conn)`**: Update `conn.peer.last_seen`, update device health in scheduler.

**`_handle_profile_update(msg, conn)`**: Update device health in scheduler.

### Discovery Callback: `_on_peer_discovered(peer)`
Connects to peer via transport, sends ANNOUNCE with identity + agent list, triggers memory sync.

### Health Callback: `_on_health_update(health)`
Updates scheduler, broadcasts PROFILE_UPDATE to all peers.

---

## 19. CLI: `src/aries/cli/main.py`

### Purpose
Click-based CLI with Rich output. Entry point: `aries`.

### Commands

| Command | Description | Key logic |
|---------|-------------|-----------|
| `aries init --name NAME` | Initialize household | Creates `AriesNode`, calls `initialize()`, prints Panel with DIDs |
| `aries start` | Start daemon | Creates `AriesNode`, calls `start()`, runs `asyncio.sleep()` loop until Ctrl+C |
| `aries status` | Show node status | Loads Household, creates DeviceProfiler, prints Panel with health stats |
| `aries agents` | List agents | Loads Household, prints Rich Table |
| `aries register --vendor V --model M [--api-key K] [--api-base B] [--name N]` | Register agent | Creates appropriate adapter via convenience constructors, calls `household.register_agent()` |
| `aries memory KEY [--value V]` | Read/write memory | Creates MemoryStore with persist_dir, calls get/set/keys |
| `aries household` | Show household tree | Loads Household, prints Rich Tree of devices → agents |

**`run_async(coro)`**: Helper using `asyncio.run()`. Tries `uvloop.install()` first.

All commands use `@click.pass_context` with `ctx.obj["data_dir"]` for the data directory.

---

## 20. Cross-Module Data Flow Diagrams

### Flow 1: Household initialization
```
CLI init → AriesNode.initialize()
  → Household.initialize()
    → KeyPair.generate() × 2 (root + device)
    → shamir_split(root.secret_bytes, n=3, k=2) → write 3 share files
    → save_key_encrypted(device_key) → device_key.json
    → public_key_to_did(root) → user_root_did
    → public_key_to_did(device) → device_did
    → build_household_membership(root_signing_key, root_did, device_did) → JWT
    → UCANStore.store(JWT) → CID
    → Create DeviceRecord, save household.json
```

### Flow 2: Node startup
```
CLI start → AriesNode.start()
  → Household.load() → read household.json + device_key.json
  → TransportServer.start() → bind TCP port
  → Register 4 message handlers (invoke, continuation, heartbeat, profile)
  → DiscoveryService(did, name, tag, port).start()
    → AsyncZeroconf → register mDNS service
    → AsyncServiceBrowser → start browsing
  → DeviceProfiler.start() → first snapshot + periodic loop
  → Scheduler()
  → MemoryStore(did, persist_dir) → load memory.json if exists
  → MemorySyncService(store, transport, did).start() → periodic sync loop
```

### Flow 3: Peer discovery + connection
```
DiscoveryService._resolve_service()
  → Verify household_tag matches
  → Create PeerInfo
  → Call _on_peer_found callback → AriesNode._on_peer_discovered()
    → TransportServer.connect_to_peer(peer)
      → asyncio.open_connection(host, port)
      → Create PeerConnection
      → Start _receive_loop background task
    → Send AriesMessage(ANNOUNCE, body={name, agents})
    → MemorySyncService.sync_with_peer(conn)
      → Send MEMORY_SYNC with phase="request", local get_sync_state()
```

### Flow 4: Task invocation
```
AriesNode.invoke(messages, capability="text.qa")
  → Scheduler.select_agent(agents, TaskConstraints)
    → _apply_mandates() → check tag/time/default mandates
    → _filter() → capability, locality, vendor, cost checks
    → _score() → privacy*3 + capability*2 + latency*1.5 + cost*1 + health*1, normalize
    → Return (best_agent, score)
  → Get adapter from _adapters[agent.agent_did]
  → memory.set("context://tasks/{id}/request", {...})
  → adapter.invoke(InvokeRequest) → litellm.acompletion() → InvokeResponse
  → memory.set("context://tasks/{id}/response", {...})
  → memory.log_append("context://tasks/{id}/history", messages + response)
  → Receipt(action="invoke", ...).add to chain
  → Return InvokeResponse
```

### Flow 5: Task handoff (Mac → Linux)
```
AriesNode.handoff(task_id, reason=HandoffReason("privacy_upgrade"))
  → memory.log_read("context://tasks/{id}/history") → messages
  → build_continuation(task_id, source_did, messages, reason)
  → Receipt(action="handoff_sent")
  → AriesMessage(type=CONTINUATION, body=continuation.to_dict())
  → transport.broadcast(msg) or peer_conn.send(msg)

[Linux node receives]
AriesNode._handle_continuation(msg, conn)
  → Continuation.from_dict(msg.body)
  → For each message: memory.log_append("context://tasks/{id}/history", ...)
  → Receipt(action="handoff_received")
  → Send ACK back to Mac
  → [Task continues with local Ollama agent]
```

### Flow 6: Memory sync (two-phase)
```
Device A                              Device B
   |                                      |
   |--- MEMORY_SYNC (request) ---------->|
   |    {phase: "request",               |
   |     state: {registers, logs, clock}}|
   |                                      |
   |    B computes diff(A_state)          |
   |                                      |
   |<-- MEMORY_SYNC (response) ----------|
   |    {phase: "response",              |
   |     diff: {new/updated entries},    |
   |     state: B's sync state}          |
   |                                      |
   |    A applies B's diff               |
   |    A computes diff(B_state)          |
   |                                      |
   |--- MEMORY_UPDATE ------------------>|
   |    {diff: A's entries B is missing} |
   |                                      |
   |    B applies A's diff               |
   |                                      |
   [Both nodes now consistent]
```

---

## 21. Constants & Magic Numbers Reference

| Constant | Value | Location | Purpose |
|----------|-------|----------|---------|
| Ed25519 multicodec prefix | `0xED, 0x01` | `did.py` | did:key encoding |
| Multibase prefix | `'z'` | `did.py` | base58btc indicator |
| Shamir threshold | `k=2, n=3` | `keys.py` | 2-of-3 secret sharing |
| GF(256) polynomial | `0x11b` | `keys.py` | Irreducible for field arithmetic |
| UCAN version | `"1.0"` | `ucan.py` | JWT `ucv` header |
| JWT algorithm | `"EdDSA"` | `ucan.py` | JWT `alg` header |
| Agent UCAN TTL | 86400s (24h) | `ucan.py` | Default agent token lifetime |
| mDNS service type | `"_aries._tcp.local."` | `discovery.py` | DNS-SD service type |
| mDNS resolve timeout | 3000ms | `discovery.py` | Service resolution timeout |
| Max message size | 10 MB | `peer.py` | Wire protocol sanity limit |
| Length prefix | 4 bytes, big-endian uint32 | `peer.py` | Wire framing |
| Scoring weights | privacy=3, cap=2, latency=1.5, cost=1, health=1 | `router.py` | Default scheduler weights |
| Profile interval | 10s | `profile.py` | Hardware sampling rate |
| Sync debounce | 100ms | `sync.py` | Write-triggered sync delay |
| Sync interval | 30s | `sync.py` | Periodic heartbeat sync |
| Context TTL | 86400s (24h) | `store.py` | context:// namespace default |
| Memory TTL | None (permanent) | `store.py` | memory:// namespace default |
| Cache TTL | 3600s (1h) | `store.py` | cache:// namespace default |
| Content hash length | 16 hex chars | `store.py`, `receipt.py` | Truncated BLAKE3/SHA-256 |

---

## 22. Error Handling Contracts

| Module | Error condition | Behavior |
|--------|----------------|----------|
| `keys.py` | Seed not 32 bytes | `ValueError` |
| `did.py` | DID doesn't start with `did:key:z` | `ValueError` |
| `did.py` | Public key not 32 bytes after decode | `ValueError` |
| `ucan.py` | Invalid UCAN version | `ValueError` |
| `ucan.py` | Signature verification fails | `ValueError("UCAN signature verification failed")` |
| `ucan.py` | Chain validation: expired | `ValueError("UCAN expired at ...")` |
| `ucan.py` | Chain validation: issuer revoked | `ValueError("Issuer ... is revoked")` |
| `ucan.py` | Chain validation: capability not satisfied | `ValueError("Required capability ... not satisfied")` |
| `ucan.py` | Chain validation: proof not found | `ValueError("Proof ... not found in store")` |
| `ucan.py` | Chain validation: linkage broken | `ValueError("Proof audience ... != token issuer ...")` |
| `household.py` | Already initialized | `RuntimeError` |
| `household.py` | Not initialized | `RuntimeError` |
| `peer.py` | Not connected | `ConnectionError` |
| `peer.py` | Message too large (>10MB) | `ValueError` |
| `peer.py` | Connection lost | `recv()` returns `None` |
| `store.py` | Key missing namespace prefix | `ValueError` |
| `litellm_adapter.py` | litellm not installed | `ImportError` |
| `node.py` | Not initialized | `RuntimeError` |
| `node.py` | No agent matches capability | `RuntimeError` |
| `node.py` | Agent not registered | `ValueError` |
| `node.py` | Target device not connected | `ConnectionError` |

---

## 23. Testing Strategy

### Unit tests (per module)

**`test_identity.py`**:
- Generate KeyPair, verify public_bytes length == 32
- Sign and verify message
- Sign with one key, verify with different key → False
- Shamir split: split secret, reconstruct from any 2 of 3 shares
- Shamir: reconstruct with wrong shares → garbage (not crash)
- DID: round-trip encode/decode
- DID: short display
- Fingerprint: deterministic (same key → same words)

**`test_ucan.py`**:
- Build UCAN, sign, verify
- Decode without verification
- Expired UCAN: `is_expired == True`
- Capability attenuation: exact match, prefix match, different ability → False
- Chain validation: root-issued UCAN validates
- Chain validation: two-level chain (root → device → agent) validates
- Chain validation: revoked issuer → ValueError
- Chain validation: broken linkage → ValueError

**`test_scheduler.py`**:
- Filter: only agents with matching capability survive
- Filter: local-only excludes cloud-routed
- Filter: cost class filtering
- Score: local agent scores higher than cloud (privacy weight)
- Score: free scores higher than paid
- Select: returns highest-scoring agent
- Mandate: tag-based mandate overrides locality
- DeviceHealth: low battery reduces health_score

**`test_memory.py`**:
- Set/get round-trip
- TTL expiry: set with ttl=0.1, sleep 0.2, get → None
- Namespace parsing: context://, memory://, cache://
- LWW: higher timestamp supersedes
- LWW: same timestamp, higher DID supersedes
- AppendLog: append + merge deduplicates by (device, seq)
- Sync state + compute_diff + apply_diff round-trip
- Persistence: save, create new store from same dir, data intact

**`test_adapters.py`**:
- LiteLLMAdapter: vendor inference from model string
- Capabilities dict structure
- InvokeRequest/InvokeResponse serialization

### Integration test

**`test_two_node.py`** (requires pytest-asyncio):
```python
async def test_two_nodes_sync():
    node_a = AriesNode("/tmp/aries-test-a")
    node_b = AriesNode("/tmp/aries-test-b")
    
    await node_a.initialize("node-a", "linux")
    # ... pair node_b ...
    
    await node_a.start()
    await node_b.start()
    
    # Write on A, verify readable on B after sync
    node_a.memory.set("context://test/value", "hello")
    await asyncio.sleep(1)  # Allow sync
    assert node_b.memory.get("context://test/value") == "hello"
```

---

*End of engineering specification. Build bottom-up following the module dependency graph in Section 2. Every class, method, field, constant, and algorithm is documented above. No external documents are needed.*
