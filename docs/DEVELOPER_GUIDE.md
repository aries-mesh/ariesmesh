# Aries Mesh — Developer Guide

This guide covers everything you need to go from a fresh clone to a running two-node mesh on your local machine, understand how the layers fit together, and add new functionality confidently.

---

## 1. Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.11+ | `python --version`. 3.14 tested. Uses `Self`, `StrEnum`, `asyncio.TaskGroup`. |
| libsodium (via PyNaCl) | Installed automatically by `pip install pynacl`. On Windows, PyNaCl ships a bundled `.dll`. |
| Ollama (optional) | For local LLM testing without a cloud key. `ollama pull qwen2.5:7b`. |

---

## 2. Dev environment setup

```powershell
git clone <repo-url>
cd aries-mesh

python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate          # Linux / macOS

pip install -e ".[dev]"
pytest -v                            # expect 46 passing
```

The editable install wires up the `aries` CLI entry point immediately — no rebuild needed after editing source files.

---

## 3. Repository layout

```
aries-mesh/
├── src/aries/
│   ├── identity/
│   │   ├── keys.py          Ed25519, Shamir SSS, save_keypair/load_keypair
│   │   ├── did.py           did:key encode/decode (base58btc multicodec)
│   │   ├── ucan.py          UCAN 1.0 capability tokens + delegation chains
│   │   └── household.py     Household manifest, device init, agent registration, pairing
│   ├── transport/
│   │   ├── peer.py          AriesMessage (CBOR + 4-byte length prefix), TransportServer, PeerConnection
│   │   └── discovery.py     mDNS via zeroconf (_aries._tcp.local.)
│   ├── scheduler/
│   │   ├── router.py        Four-stage pipeline: Filter → Mandate → Score → Select
│   │   └── profile.py       DeviceProfiler (psutil snapshots every 10 s)
│   ├── memory/
│   │   ├── store.py         LWW CRDT + AppendLog + canonical resource grammar
│   │   └── sync.py          Two-phase memory sync (debounce 100 ms, periodic 30 s)
│   ├── adapters/
│   │   ├── base.py          BaseAdapter ABC, Message, InvokeRequest, InvokeResponse
│   │   ├── litellm_adapter.py  LiteLLMAdapter (Anthropic, OpenAI, Google, Ollama, any OpenAI-compat)
│   │   └── mock_adapter.py  MockAdapter — deterministic offline responses
│   ├── continuation.py      Signed hand-off envelope (Ed25519 full-field hash)
│   ├── receipt.py           Hash-linked signed audit chain
│   ├── node.py              AriesNode daemon
│   ├── util.py              canonical_json, helpers
│   ├── _wordlist.py         BIP39 first-256 words
│   └── cli/main.py          `aries` Click entry point
├── tests/
│   ├── test_identity.py
│   ├── test_ucan.py
│   ├── test_scheduler.py
│   ├── test_memory.py
│   ├── test_adapters.py
│   ├── test_two_node.py     Two-node integration (loopback, enable_discovery=False)
│   └── test_security.py     Adversarial / hardening tests (v0.1.1)
├── docs/
│   ├── ARCHITECTURE.md      Authoritative design doc (overrides PRD on conflicts)
│   ├── ENGINEERING_SPEC.md  Original PRD (preserved as historical brief)
│   ├── TECH_STACK.md        Library inventory + full test list
│   └── research/            Research notes and surveys
├── pyproject.toml
├── CONTRIBUTING.md
├── CHANGELOG.md
└── SECURITY.md
```

---

## 4. Key abstractions

### 4.1 AriesNode

`node.py` is the per-device runtime. It holds references to all layers and exposes the high-level operations:

```python
node = AriesNode(data_dir=Path("~/.aries/my-laptop"))
await node.start()                              # binds TCP, starts mDNS, spawns memory sync
await node.invoke(messages=[...], capability="aries/agent.invoke")
await node.handoff(target_device_did="did:key:z6Mk...", reason="low battery")
await node.handoff_to_best_peer(reason="low battery", required_capabilities=["aries/agent.invoke"])
await node.stop()
```

`node.start()` accepts `enable_discovery=False` and `enable_profiler=False` to suppress mDNS and psutil — useful in tests.

### 4.2 AriesMessage

Every message on the wire is an `AriesMessage` (CBOR-encoded):

```
4-byte big-endian length | CBOR({type, sender, recipient, body, timestamp})
```

Message types are string constants in `transport/peer.py`: `ANNOUNCE`, `HANDSHAKE`, `ACK`, `ERROR`, `INVOKE`, `INVOKE_RESULT`, `HANDOFF`, `SYNC_REQUEST`, `SYNC_RESPONSE`, `SYNC_UPDATE`.

### 4.3 Continuation envelope

A `Continuation` is the hand-off payload. v0.1.1 signs every field:

```python
cont = build_continuation(task_id, messages, system_prompt, resources,
                          required_capabilities, locality_preference,
                          ucan_chain, metadata, max_cost_class, reason)
cont = cont.sign(keypair, signer_did)      # Ed25519 over canonical JSON of all fields
# ... send over transport ...
if not cont.verify():                      # receiver checks before anything else
    raise
```

### 4.4 Memory store

```python
store = MemoryStore()
store.set("aries:context://tasks/abc/history", value, device_did="did:key:z6Mk...")
entry = store.get("aries:context://tasks/abc/history")  # returns LWWEntry or None

# Legacy form still works (one DeprecationWarning per prefix per session):
store.set("context://tasks/abc/history", value, device_did=...)
```

Keys follow `aries:<namespace>://<path>` — see `docs/ARCHITECTURE.md` for the full grammar.

### 4.5 UCAN capability tokens

```python
from aries.identity.ucan import UCANStore, Capability, build_token

store = UCANStore()
root_token = build_token(issuer_did, audience_did,
                          capabilities=[Capability("*", "aries/agent.invoke")],
                          keypair=root_keypair, expiry=int(time.time()) + 3600)
store.add(root_token)
store.validate_chain(agent_token)   # walks issuer chain up to root, checks expiry + revocation
```

`Capability.is_attenuated_by(parent)` rules:
- `parent == "*"` → always true.
- `parent == self` → true.
- `parent.endswith("/*")` and child starts with `parent[:-2]` → true (glob).
- `child.startswith(parent.rstrip("/") + "/")` → true (strict prefix).

### 4.6 Scheduler pipeline

```python
router = SchedulerRouter()
router.load_mandates(Path("~/.aries/mandates.yaml"))   # optional
result = router.route(request, candidates)             # DeviceProfile list
# returns the winning DeviceProfile (or raises if none passes Filter)
```

Four stages in order:

1. **Filter** — drops candidates whose `capabilities` don't cover `request.required_capabilities`.
2. **Mandate** — applies YAML mandate rules (time windows, locality, cost class). Can hard-exclude candidates.
3. **Score** — weighted sum: `privacy × 3 + capability × 2 + latency × 1.5 + cost × 1 + health × 1`.
4. **Select** — returns the highest-scoring candidate.

---

## 5. Running a two-node mesh locally

You need two terminal sessions (or two WSL2 windows):

**Device A — initialize and start**

```powershell
$A = "$env:TEMP\aries-dev-a"
aries --data-dir $A init --name dev-a
aries --data-dir $A register --vendor mock --model demo-1
aries --data-dir $A start         # leave running
```

**Device B — initialize, pair, start**

```powershell
$B = "$env:TEMP\aries-dev-b"
aries --data-dir $B init --name dev-b

# On device A first:
aries --data-dir $A pair --invite    # prints 6-word code

# On device B:
aries --data-dir $B pair --code "word1 word2 word3 word4 word5 word6"
aries --data-dir $B register --vendor mock --model demo-1
aries --data-dir $B start
```

**Invoke and handoff**

```powershell
# Invoke on A:
aries --data-dir $A invoke -m "hello from A"

# Handoff to B (requires B's device DID — check aries --data-dir $B status):
aries --data-dir $A handoff --target <did:key:z6Mk...> --reason "testing"
```

---

## 6. Writing tests

### Unit tests

- Place in `tests/test_<module>.py`.
- Async tests work without any decorator — `pytest-asyncio` is in `auto` mode.
- Use `MockAdapter` for all LLM calls. Never hit a live endpoint in tests.
- Construct `MemoryStore`, `UCANStore`, `KeyPair` etc. directly — no filesystem.

### Integration tests

- Use `AriesNode(..., enable_discovery=False, enable_profiler=False)` for reliable loopback.
- Pick ephemeral ports: `port=0` in `TransportServer` (the OS assigns one), then read `server.port` after bind.
- Clean up with `await node.stop()` in a `finally` block.

### Security / adversarial tests

- Add to `tests/test_security.py`.
- Patterns: tamper a field after signing, replay an expired UCAN, attempt a handoff to an unconnected peer, load a revoked device DID into the chain.

---

## 7. Adding a new LLM adapter

1. Subclass `BaseAdapter` (`src/aries/adapters/base.py`).
2. Implement `async def invoke(self, request: InvokeRequest) -> InvokeResponse`.
3. Declare `vendor: str` as a class attribute matching the string passed to `aries register --vendor <name>`.
4. Register in `household.py::_adapter_for_vendor()` (the lookup dict near the bottom of the file).
5. Add at least one test in `tests/test_adapters.py` using your adapter in offline/mock mode.

Only add a new adapter when `LiteLLMAdapter` genuinely cannot cover the case — it already handles Anthropic, OpenAI, Google, Ollama, and any OpenAI-compatible endpoint.

---

## 8. Lint and type checks

```powershell
# Lint
.\.venv\Scripts\ruff.exe check src/ tests/

# Type check (mypy — not required but helpful)
.\.venv\Scripts\mypy.exe src/aries --ignore-missing-imports
```

Line length is 100. `from __future__ import annotations` is required at the top of any file using forward references. No `# type: ignore` without a one-line explanation on the same line.

---

## 9. CLI reference

| Command | Description |
|---------|-------------|
| `aries init --name <n>` | Create a new device identity in `~/.aries/<n>/`. |
| `aries start` | Start the daemon (TCP transport + mDNS + memory sync). Ctrl-C to stop. |
| `aries pair --invite` | Print a 6-word pairing invitation code. |
| `aries pair --code "<words>"` | Accept an invitation and pair with the inviting device. |
| `aries register --vendor <v> --model <m>` | Register an LLM agent. Vendors: `mock`, `ollama`, `anthropic`, `openai`, `litellm`. |
| `aries agents` | List registered agents. |
| `aries invoke -m "<msg>"` | Invoke the best available agent with a single message. |
| `aries resume <task_id>` | Manually resume a handed-off task (debugging escape hatch). |
| `aries handoff --target <did> --reason "<r>"` | Hand off the current task to a specific peer. |
| `aries status` | Print device DID, connected peers, registered agents, scheduler weights. |
| `aries memory get <key>` | Read a memory entry by canonical key. |
| `aries memory set <key> <value>` | Write a memory entry. |
| `aries household` | Print the household manifest (devices, pairing state). |
| `aries mandate list` | List active scheduler mandates. |

All commands accept `--data-dir <path>` to override the default `~/.aries/` data directory.

---

## 10. Configuration files

| Path | Format | Purpose |
|------|--------|---------|
| `~/.aries/<device>/device_key.json` | JSON v1 (plaintext) or v2 (Argon2id+SecretBox) | Ed25519 device keypair |
| `~/.aries/<device>/household.json` | JSON | Household manifest: members, agent registry, pairing invitations |
| `~/.aries/<device>/memory.json` | JSON | Persisted LWW and AppendLog entries |
| `~/.aries/<device>/root_share_*.bin` | Binary | Shamir shares of the root key (keep these safe) |
| `~/.aries/mandates.yaml` | YAML | Scheduler mandate overrides (optional) |

None of these files should ever be committed to version control — all are covered by `.gitignore`.

---

## 11. Common pitfalls

**`CryptoError` on `load_keypair`** — you saved the key with a passphrase and are loading without one, or vice versa. Check the `version` field in the JSON file.

**`ValueError: handoff requires explicit target_device_did`** — v0.1.1 removed the broadcast fallback. Pass the target's `did:key:...` or use `node.handoff_to_best_peer()`.

**mDNS not finding peers** — confirm both devices are on the same subnet and no firewall blocks UDP 5353. In tests, always use `enable_discovery=False` and connect peers manually.

**`DeprecationWarning: legacy resource key`** — you are using `context://...` instead of `aries:context://...`. Update the key. The legacy form will be removed in v0.2.

**`test_two_node.py` hangs** — ensure `enable_discovery=False` and `enable_profiler=False` are passed to `AriesNode`. The zeroconf and psutil loops block on Windows without these flags.
