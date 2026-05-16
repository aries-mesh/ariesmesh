<p align="center">
  <img src="docs/assets/logo-dark.png" alt="Aries Mesh" width="600">
</p>

<p align="center">
  <strong>Open-source personal compute fabric — intelligent task routing, shared memory, and cryptographic identity across your device mesh.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Tests-46%20passing-brightgreen.svg" alt="Tests: 46 passing">
  <img src="https://img.shields.io/badge/Version-0.1.1-orange.svg" alt="Version: 0.1.1">
</p>

---

## What is Aries Mesh

Aries Mesh turns your personal devices — laptop, desktop, phone — into a single coordinated runtime for AI agents. It provides intelligent task routing (send each task to the best device based on privacy, cost, capability, and health), a shared memory fabric (every agent on every device reads and writes from the same CRDT-backed context store), and user-owned cryptographic identity (you hold the root key, not any vendor).

It sits beneath agent frameworks and model providers as the infrastructure layer. It doesn't care whether you use Claude, GPT, Gemini, Llama, or Qwen. It treats all of them as pluggable adapters and focuses on the orchestration: how devices discover each other, how tasks get routed, how context flows, and who controls the keys.

---

## What works today

- Household initialization with Shamir 2-of-3 root key splitting
- Device pairing via 6-word BIP39 codes over mDNS
- Agent registration for any LLM provider through litellm (Ollama, Anthropic, OpenAI, Google, any OpenAI-compatible endpoint)
- Privacy-first task routing — four-stage scheduler: Filter, Mandate, Score, Select
- User-defined mandates (tag-based, time-based, default) via YAML config
- CRDT-backed shared memory with three namespaces: context://, memory://, cache://
- Two-phase memory sync across devices (100ms debounce + 30s periodic)
- Signed Continuation envelopes for cross-device task hand-off
- Hash-linked Ed25519-signed receipt chains for audit trails
- UCAN 1.0 capability-based authorization with delegation chains and glob attenuation
- Argon2id + SecretBox encrypted key storage
- Full CLI: init, start, pair, register, invoke, handoff, status, agents, memory, household
- 46 tests passing — zero external API keys required

---

## Architecture

```
┌─────────────────────┐     ┌─────────────────────┐     ┌─────────────────────┐
│   MacBook Pro       │     │   Linux Desktop     │     │   Android Phone     │
│                     │     │                     │     │                     │
│  ┌───────────────┐  │     │  ┌───────────────┐  │     │  ┌───────────────┐  │
│  │ Shared Memory │  │     │  │ Shared Memory │  │     │  │ Shared Memory │  │
│  │ (CRDT Sync)   │  │     │  │ (CRDT Sync)   │  │     │  │ (CRDT Sync)   │  │
│  ├───────────────┤  │     │  ├───────────────┤  │     │  ├───────────────┤  │
│  │  Scheduler    │  │     │  │  Scheduler    │  │     │  │  Scheduler    │  │
│  ├───────────────┤  │     │  ├───────────────┤  │     │  ├───────────────┤  │
│  │  Transport +  │  │     │  │  Transport +  │  │     │  │  Transport +  │  │
│  │  Identity     │  │     │  │  Identity     │  │     │  │  Identity     │  │
│  ├───────────────┤  │     │  ├───────────────┤  │     │  ├───────────────┤  │
│  │ Node Runtime  │  │     │  │ Node Runtime  │  │     │  │ Node Runtime  │  │
│  │ + Adapters    │  │     │  │ + Adapters    │  │     │  │ + Adapters    │  │
│  └───────────────┘  │     │  └───────────────┘  │     │  └───────────────┘  │
└──────────┬──────────┘     └──────────┬──────────┘     └──────────┬──────────┘
           │          CBOR/TCP + mDNS  │                           │
           └───────────────────────────┴───────────────────────────┘
```

Every device runs the same daemon. The scheduler decides where each task goes. Memory syncs automatically.

| Layer | Responsibility | Key Modules |
|-------|---------------|-------------|
| Layer 3 — Shared Memory | CRDT-backed distributed state, three namespaces, two-phase sync | `memory/store.py`, `memory/sync.py` |
| Layer 2 — Scheduler | Capability-aware routing, privacy scoring, user mandates | `scheduler/router.py`, `scheduler/profile.py` |
| Layer 1 — Transport + Identity | DID-based identity, UCAN delegation, mDNS discovery, CBOR messaging | `identity/`, `transport/` |
| Layer 0 — Node Runtime | Per-device daemon, vendor adapters, hardware profiling | `node.py`, `adapters/` |

---

## Quickstart

### Install

```bash
git clone https://github.com/aries-mesh/ariesmesh.git)
cd aries-mesh
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### Initialize and start

```bash
aries init --name my-laptop
aries register --vendor ollama --model qwen2.5:7b
aries register --vendor mock --model fallback
aries start
```

In a second terminal:

```bash
aries status
aries agents
```

### Pair a second device

On the first device:

```bash
aries pair --invite
# prints: Pairing code: alpha anchor apple arrow seed shore
```

On the second device:

```bash
aries init --name my-phone
aries pair --code "alpha anchor apple arrow seed shore"
```

### Hand off a conversation

```bash
# On device A — start a task
aries invoke -m "Explain the CAP theorem" --capability text.qa

# Hand it off to device B
aries handoff --task-id <task_id> --target <device_B_did> --reason user_request
```

The conversation continues on device B with full context. Shared memory keeps both devices in sync.

---

## How task routing works

When a task arrives, the scheduler runs a four-stage pipeline. First, Filter removes agents that lack the required capability. Second, Mandate applies your standing policies (tag-based, time-based, or default rules from `~/.aries/mandates.yaml`). Third, Score ranks remaining candidates across five weighted dimensions. Fourth, Select picks the winner.

| Dimension | Weight | What it means |
|-----------|--------|----------------|
| Privacy | 3.0 | Local models strongly preferred over cloud |
| Capability | 2.0 | Larger context windows, stronger models score higher |
| Latency | 1.5 | LAN and local agents favored |
| Cost | 1.0 | Free beats metered beats paid |
| Health | 1.0 | Battery, thermal, CPU load factor in |

Local models win by default. Override with mandates.

```yaml
# ~/.aries/mandates.yaml
- name: sensitive-stays-local
  when_tags: [medical, financial, personal]
  enforce_locality: local

- name: overnight-budget
  when_time: "00:00-06:00"
  enforce_cost_class: free
```

---

## Project structure

```
aries-mesh/
├── src/aries/
│   ├── node.py                    # Per-device daemon — ties all layers together
│   ├── continuation.py            # Signed hand-off envelope
│   ├── receipt.py                 # Hash-linked signed audit chain
│   ├── identity/
│   │   ├── keys.py                # Ed25519 keypairs, Shamir 2-of-3, Argon2id storage
│   │   ├── did.py                 # did:key encoding/decoding
│   │   ├── ucan.py                # UCAN 1.0 tokens, chain validation, glob attenuation
│   │   └── household.py           # Household state, pairing, agent registration
│   ├── transport/
│   │   ├── peer.py                # CBOR wire protocol, TCP connections
│   │   └── discovery.py           # mDNS service advertisement and browsing
│   ├── scheduler/
│   │   ├── router.py              # Four-stage routing pipeline, mandates
│   │   └── profile.py             # Hardware profiler (psutil)
│   ├── memory/
│   │   ├── store.py               # LWW-Register + AppendLog CRDTs, three namespaces
│   │   └── sync.py                # Two-phase sync protocol
│   ├── adapters/
│   │   ├── base.py                # Abstract adapter interface
│   │   ├── litellm_adapter.py     # Universal LLM adapter (100+ providers)
│   │   └── mock_adapter.py        # Deterministic offline adapter for testing
│   └── cli/
│       └── main.py                # CLI entry point
├── tests/                         # 46 tests (unit + integration + security)
├── docs/
│   ├── ARCHITECTURE.md
│   ├── ENGINEERING_SPEC.md
│   ├── DEVELOPER_GUIDE.md
│   ├── TECH_STACK.md
│   └── research/
│       └── distributed-inference-survey.md
└── pyproject.toml
```

---

## Tech stack

| Component | Choice | Purpose |
|-----------|--------|---------|
| Language | Python 3.11+ | Native async/await, modern type system |
| Cryptography | PyNaCl (libsodium) | Ed25519 signing, Argon2id KDF, SecretBox encryption |
| Wire format | cbor2 | Compact binary encoding, deterministic for signatures |
| Discovery | zeroconf | mDNS service advertisement and peer browsing |
| LLM adapters | litellm | Universal translation layer for 100+ providers |
| Profiling | psutil | Cross-platform CPU, RAM, battery, thermal monitoring |
| Content hashing | BLAKE3 | Fast cryptographic hashing with SHA-256 fallback |
| CLI | Click + Rich | Command framework with formatted terminal output |
| Mandates | PyYAML | Human-readable policy configuration |

---

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Engineering Specification](docs/ENGINEERING_SPEC.md)
- [Developer Guide](docs/DEVELOPER_GUIDE.md)
- [Tech Stack & Test Inventory](docs/TECH_STACK.md)
- [Distributed Inference Research Survey](docs/research/distributed-inference-survey.md)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. Run `pip install -e '.[dev]'` then `pytest` to verify all 46 tests pass. Open an issue before sending a PR.

---

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center"><sub>Three devices, one brain.</sub></p>
