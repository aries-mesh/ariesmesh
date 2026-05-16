# Distributed Inference on a Device Mesh — Survey

**Status:** Working draft — open questions marked `[TBD]`.
**Scope:** What it would take to run a single LLM inference request across multiple Aries Mesh nodes, and which techniques from the research literature are feasible on heterogeneous consumer hardware.

---

## 1. Motivation

Aries Mesh v0.1 treats each device as an independent inference node: the scheduler picks **one** device per request. This is correct for routing, but leaves capability on the table. A household of four devices (laptop, desktop, phone, Raspberry Pi) may collectively own enough VRAM and RAM to run a model that no single device can fit — or to serve it at a latency that no single device can match.

The goal of this research track is to understand the trade-off space well enough to decide:

1. Which parallelism strategies are worth implementing in v0.2 / v0.3.
2. What changes to the scheduler, transport, and memory layers those strategies require.
3. What the realistic performance floor is on a home LAN (1 Gbps Ethernet, Wi-Fi 6).

---

## 2. Parallelism strategies

### 2.1 Tensor parallelism (TP)

Split individual weight matrices across devices along the hidden dimension. Each device holds a shard; an all-reduce synchronization step merges partial activations at every transformer layer.

**Pros:** Works well for very large models (70B+); reduces per-device memory linearly with the number of shards.

**Cons:** Requires a high-bandwidth, low-latency interconnect. All-reduce latency dominates at small batch sizes. Consumer Ethernet (~1 Gbps = ~120 MB/s) is 10–100× slower than NVLink. Likely impractical across Wi-Fi.

**Relevant work:** Megatron-LM (Shoeybi et al. 2019), DeepSpeed ZeRO (Rajbhandari et al. 2020).

**Feasibility on Aries Mesh:** `[TBD]` — needs benchmarks. Initial hypothesis: viable only on wired Ethernet for small models (≤7B) with TP degree 2.

### 2.2 Pipeline parallelism (PP)

Assign consecutive transformer layers to consecutive devices. The token sequence passes through devices in a pipeline; each device processes its layer slice and forwards activations to the next.

**Pros:** Bandwidth requirement is proportional to activation size (one layer boundary), not all weights. More tolerant of latency than TP. Can be implemented with the existing TCP transport.

**Cons:** Pipeline bubble at sequence boundaries; increases latency for a single request. Most beneficial at throughput, not latency. Requires static layer assignment at startup.

**Relevant work:** GPipe (Huang et al. 2019), PipeDream (Narayanan et al. 2019), Petals (Borzunov et al. 2022) — the closest prior art for consumer/volunteer hardware.

**Feasibility on Aries Mesh:** `[TBD]` — Petals' architecture is a near-direct model. Primary open question: how to integrate layer-shard assignment into the Aries scheduler without breaking the vendor-agnostic adapter model.

### 2.3 Sequence parallelism (SP)

Distribute the input sequence (or prefill tokens) across devices, merging attention outputs via all-gather. Reduces memory pressure for long-context requests.

**Pros:** Well-suited for long documents on a mesh with moderate bandwidth.

**Cons:** Requires attention implementations that support distributed prefill (flash-attn-2 or equivalent). Significant implementation complexity.

**Feasibility on Aries Mesh:** `[TBD]` — low priority for v0.2; revisit after PP lands.

### 2.4 KV-cache sharing

Devices serving different tokens of the same conversation share a distributed KV cache, avoiding redundant prefill computation.

**Pros:** Significant speedup for multi-turn conversations with long history.

**Cons:** Cache coherence across devices with the LWW CRDT store is non-trivial (KV entries are large and time-sensitive). Requires TTL tuning.

**Relevant work:** DistKV (various); Infinite LLM (Lin et al. 2024).

**Feasibility on Aries Mesh:** `[TBD]` — the `cache://` namespace in `MemoryStore` already has a 1 h TTL and is synced via two-phase protocol. The missing piece is a content-addressed blob store for activation tensors.

### 2.5 Speculative decoding across devices

A small "draft" model (local, fast) proposes tokens; a large "verifier" model (remote, accurate) accepts or rejects them. The two models can live on different devices.

**Pros:** Reduces the number of round-trips to the large model; the draft model runs locally at near-zero latency.

**Cons:** Requires tight coupling of draft and verifier; token acceptance rate degrades if the models diverge.

**Feasibility on Aries Mesh:** `[TBD]` — requires the adapter layer to expose speculative decoding primitives. Currently `InvokeRequest` has no draft-token field.

---

## 3. Bandwidth and latency budget

`[TBD — needs measurement]`

Expected measurements needed:

- Round-trip latency: loopback, 1 Gbps Ethernet, Wi-Fi 6 same AP, Wi-Fi cross-AP.
- Sustained TCP throughput between two nodes under the existing CBOR transport.
- Activation tensor sizes per layer boundary for Qwen 2.5 7B, Llama 3.2 3B, Mistral 7B.
- Memory footprint per layer shard at int8 and fp16 precision.

Hypothesized budget for pipeline parallelism to be viable (latency < 2× single-device):

| Link | Max activation transfer per layer | Verdict |
|------|------------------------------------|---------|
| Loopback | ~300 MB/s | Viable |
| 1 Gbps Ethernet | ~100 MB/s | Possibly viable (7B fp16 ~14 MB/layer) |
| Wi-Fi 6 (same AP) | ~40–60 MB/s | Marginal |
| Wi-Fi cross-AP | ~10–20 MB/s | Unlikely viable |

---

## 4. Scheduler changes required

The current scheduler routes to one device. Distributed inference requires:

1. **Group selection** — instead of returning a single `DeviceProfile`, return an ordered list of devices that together satisfy the request.
2. **Layer assignment** — for PP, a secondary allocation step assigns layer ranges to each selected device.
3. **Capability advertisement** — devices must advertise layer-shard capability in their ANNOUNCE TXT record (e.g. `shards=llama3.2-3b:layers=0-15`).
4. **Health monitoring** — the profiler needs VRAM-in-use in addition to CPU/RAM.

`[TBD]` — propose specific changes to `SchedulerRouter.route()` return type and `DeviceProfile` schema.

---

## 5. Protocol changes required

- `Continuation` needs a `shard_map` field (device DID → layer range) for PP.
- `INVOKE` message needs a `shard_index` field so each device knows which layers to evaluate.
- A new `ACTIVATION_FORWARD` message type passes intermediate activations between pipeline stages.
- `INVOKE_RESULT` aggregation: a coordinator device collects partial results and assembles the final token stream.

`[TBD]` — draft message schema.

---

## 6. Prior art to study

| Project | Relevance |
|---------|-----------|
| **Petals** (Together AI, 2022) | Closest reference implementation. BitTorrent-style layer distribution on volunteer hardware. |
| **ExoLLM** (2024) | Consumer mesh inference; edge-focused. |
| **PowerInfer** (2024) | CPU/GPU co-execution; relevant for heterogeneous devices. |
| **DeepSpeed-Inference** | Reference for TP/PP implementation patterns. |
| **vLLM PagedAttention** | KV-cache management; relevant for cache-sharing design. |
| **Infinite LLM** (2024) | Distributed KV cache across a cluster. |

---

## 7. Open questions

1. Is pipeline parallelism across Wi-Fi latency-competitive with a single beefy device (e.g. MacBook Pro M3 Max)?
2. What is the minimum model size where PP across two wired devices beats one device?
3. Can the existing `MemoryStore` + `SyncManager` serve as the KV-cache transport, or does activation size require a separate blob channel?
4. How does speculative decoding interact with the Aries scheduler (the draft model lives locally; does it bypass the routing pipeline entirely)?
5. Is there a viable design where the adapter abstraction (`BaseAdapter`) remains vendor-agnostic while supporting distributed inference? Or does PP require adapter-aware layer routing?

---

## 8. Recommended first experiment

Before any code: benchmark TCP throughput between two Aries nodes using the existing transport (`AriesMessage` with a large binary body). Confirm the effective MB/s on Ethernet and Wi-Fi. If throughput is below ~50 MB/s for a 7B model layer boundary, pipeline parallelism across Wi-Fi is not worth pursuing.

```python
# Sketch: send N bytes as an AriesMessage body, measure round-trip
import asyncio, time
from aries.transport.peer import TransportServer, AriesMessage

# [TBD: flesh out benchmark script]
```
