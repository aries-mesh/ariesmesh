"""Main per-device daemon. Ties identity, transport, scheduler, memory, adapters together.

Spec reference: §18 (historical). v0.1.1 deviates from the PRD in three places:
  * Continuation envelopes are signed (Fix 1); `_handle_continuation` verifies
    and rejects on tamper before doing anything else.
  * `handoff()` requires an explicit `target_device_did` — no broadcast (Fix 2).
  * The canonical continuation receive behavior is **auto-resume**, not
    ACK-then-wait (Fix 4). `aries resume <task_id>` is the manual escape hatch.

All task-scoped memory uses the canonical resource grammar
`aries:context://tasks/<id>/<bucket>` (Fix 5); see `_canonical_*_key` helpers
at the bottom of this module.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from .adapters.base import BaseAdapter, InvokeRequest, InvokeResponse, Message
from .api.server import DashboardAPI
from .continuation import Continuation, HandoffReason, build_continuation
from .identity.household import AgentRecord, Household
from .identity.keys import KeyPair
from .inference.capability import _PENDING_PROBES, probe_inference_capability
from .inference.coordinator import InferenceCoordinator
from .inference.registry import DeviceCapability, InferenceConfig, InferenceRegistry
from .memory.store import MemoryStore
from .memory.sync import MemorySyncService
from .receipt import Receipt, ReceiptChain
from .scheduler.profile import DeviceProfiler
from .scheduler.router import (
    DeviceHealth,
    Locality,
    Mandate,
    Scheduler,
    TaskConstraints,
    load_mandates_from_yaml,
)
from .transport.discovery import ZEROCONF_AVAILABLE, DiscoveryService
from .transport.peer import AriesMessage, MessageTypes, PeerConnection, PeerInfo, TransportServer


class AriesNode:
    def __init__(self, data_dir: str | Path = "~/.aries") -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.household: Optional[Household] = None
        self.transport: Optional[TransportServer] = None
        self.discovery: Optional[DiscoveryService] = None
        self.scheduler: Optional[Scheduler] = None
        self.profiler: Optional[DeviceProfiler] = None
        self.memory: Optional[MemoryStore] = None
        self.sync: Optional[MemorySyncService] = None
        self._adapters: dict[str, BaseAdapter] = {}
        self._receipt_chains: dict[str, ReceiptChain] = {}
        self._started = False

        # Distributed inference state (Feature 2).
        self._inference_registry: Optional[InferenceRegistry] = None
        self._inference_coordinator: Optional[InferenceCoordinator] = None
        self._inference_ready_futures: dict[str, asyncio.Future[bool]] = {}
        self._inference_rpc_processes: dict[str, "asyncio.subprocess.Process"] = {}
        self._local_inference_capability: Optional[DeviceCapability] = None

        # Live dashboard (Phase 4). Set by `aries start` when the dashboard
        # is active; left as None otherwise.
        self._dashboard: Optional[Any] = None
        self._start_time: float = 0.0

        # Web dashboard HTTP/SSE API (Phase 5). Auto-started in start();
        # left as None if the port is busy or binding fails.
        self._api: Optional[DashboardAPI] = None

    # -----------------------------------------------------------------------
    # init / load
    # -----------------------------------------------------------------------

    async def initialize(self, device_name: str, platform: str) -> dict[str, str]:
        self.household = Household(data_dir=self.data_dir)
        return self.household.initialize(device_name=device_name, platform=platform)

    async def start(
        self,
        *,
        enable_discovery: bool = True,
        enable_profiler: bool = True,
        enable_api: bool = True,
        api_host: str = "127.0.0.1",
        api_port: int = 7272,
    ) -> None:
        """Bring up transport, scheduler, memory + (optionally) discovery and profiler.

        Pass `enable_discovery=False` / `enable_profiler=False` in tests or when
        running headless; the mesh still works (peers can be wired manually).
        """
        if self._started:
            return
        self._started = True
        self._start_time = time.time()
        if self.household is None:
            self.household = Household(data_dir=self.data_dir)
        if not self.household.is_initialized:
            raise RuntimeError(f"Household at {self.data_dir} not initialized; run `aries init` first")
        self.household.load()

        # transport — pass device keypair so all connections are Noise_XX encrypted
        self.transport = TransportServer(device_keypair=self.household._device_key)
        port = await self.transport.start()
        self.transport.on_message(MessageTypes.INVOKE, self._handle_invoke)
        self.transport.on_message(MessageTypes.CONTINUATION, self._handle_continuation)
        self.transport.on_message(MessageTypes.HEARTBEAT, self._handle_heartbeat)
        self.transport.on_message(MessageTypes.PROFILE_UPDATE, self._handle_profile_update)
        self.transport.on_message(MessageTypes.ANNOUNCE, self._handle_announce)
        self.transport.on_message(MessageTypes.PAIRING_REQUEST, self._handle_pairing_request)

        # discovery (skipped under enable_discovery=False or when zeroconf
        # isn't installed — e.g. on Termux. Without it, peers must be added
        # manually via `aries connect <ip:port>`.)
        if enable_discovery and ZEROCONF_AVAILABLE:
            tag = self.household.household_tag
            self.discovery = DiscoveryService(
                device_did=self.household.device_did or "",
                device_name=self._self_device_name(),
                household_tag=tag,
                port=port,
                capabilities=self._aggregate_capabilities(),
            )
            self.discovery.on_peer_found(self._on_peer_discovered)
            await self.discovery.start()
        elif enable_discovery and not ZEROCONF_AVAILABLE:
            import logging as _lg
            _lg.getLogger(__name__).info(
                "mDNS discovery unavailable (zeroconf not installed). "
                "Use `aries connect <ip:port>` to add peers manually."
            )

        # profiler (skipped under enable_profiler=False)
        if enable_profiler:
            self.profiler = DeviceProfiler(device_did=self.household.device_did or "")
            self.profiler.on_update(self._on_health_update)
            await self.profiler.start()

        # scheduler + mandates
        mandates_path = self.data_dir / "mandates.yaml"
        mandates: list[Mandate] = load_mandates_from_yaml(mandates_path)
        self.scheduler = Scheduler(mandates=mandates)
        if self.profiler is not None and self.profiler.latest is not None:
            self.scheduler.update_device_health(self.profiler.latest)

        # memory + sync
        self.memory = MemoryStore(
            device_did=self.household.device_did or "",
            persist_dir=self.data_dir / "memory",
        )
        self.sync = MemorySyncService(
            store=self.memory,
            transport=self.transport,
            device_did=self.household.device_did or "",
        )
        await self.sync.start()

        # Inference registry: probe local capability, register message handlers.
        self._inference_registry = InferenceRegistry()
        try:
            self._local_inference_capability = await probe_inference_capability(
                self.household.device_did or "", self.data_dir
            )
            self._inference_registry.update_device(
                self.household.device_did or "", self._local_inference_capability
            )
        except Exception:
            # Probing must never block daemon startup.
            self._local_inference_capability = None

        self.transport.on_message(MessageTypes.INFERENCE_SETUP, self._handle_inference_setup)
        self.transport.on_message(MessageTypes.INFERENCE_READY, self._handle_inference_ready)
        self.transport.on_message(MessageTypes.INFERENCE_TEARDOWN, self._handle_inference_teardown)
        self.transport.on_message(MessageTypes.INFERENCE_PROBE, self._handle_inference_probe)
        self.transport.on_message(
            MessageTypes.INFERENCE_PROBE_RESPONSE, self._handle_inference_probe_response
        )
        self.transport.on_message(MessageTypes.STREAM_CHUNK, self._handle_stream_chunk)

        # Web dashboard HTTP/SSE API. Best-effort: if the port is in use we
        # log and continue without it — the daemon itself is fully functional.
        if enable_api:
            self._api = DashboardAPI(self, host=api_host, port=api_port)
            ok = await self._api.start()
            if ok:
                import logging as _lg
                _lg.getLogger(__name__).info(
                    "Dashboard available at http://%s:%s", api_host, self._api.port
                )
            else:
                self._api = None

    async def stop(self) -> None:
        for component in (self._api, self.sync, self.profiler, self.discovery, self.transport):
            if component is None:
                continue
            try:
                await component.stop()
            except Exception:
                pass
        self._api = None
        self._started = False

    # -----------------------------------------------------------------------
    # helpers
    # -----------------------------------------------------------------------

    def _self_device_name(self) -> str:
        if self.household and self.household.device_did:
            rec = self.household.devices.get(self.household.device_did)
            if rec:
                return rec.name
        return "aries"

    def _aggregate_capabilities(self) -> list[str]:
        caps: set[str] = set()
        if self.household:
            for agent in self.household.agents.values():
                caps.update(agent.capabilities)
        return sorted(caps)

    def _agent_device_map(self) -> dict[str, str]:
        if self.household is None:
            return {}
        # locally-registered agents all live on this device
        return {a.agent_did: self.household.device_did or "" for a in self.household.agents.values()}

    def _ensure_started(self) -> None:
        if not self._started:
            raise RuntimeError("Node not started; call start() first")

    def _emit_event(self, event_type: str, description: str) -> None:
        """Push an event to the live dashboard + web API event stream."""
        if self._dashboard is not None:
            try:
                self._dashboard.add_event(event_type, description)
            except Exception:
                pass
        if self._api is not None:
            try:
                self._api.push_event(event_type, description)
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # agent registration
    # -----------------------------------------------------------------------

    def register_agent(self, adapter: BaseAdapter, name: Optional[str] = None) -> AgentRecord:
        if self.household is None or not self.household.is_initialized:
            raise RuntimeError("Household not initialized")
        if self.household.device_did is None:
            self.household.load()

        caps = adapter.capabilities()
        record = self.household.register_agent(
            name=name or f"{caps['vendor']}/{caps['model']}",
            vendor=caps["vendor"],
            model=caps.get("model"),
            capabilities=list(caps.get("capabilities", [])),
            context_window=int(caps.get("context_window", 0)),
            locality=str(caps.get("locality", "local")),
            cost_class=str(caps.get("cost_class", "free")),
        )
        self._adapters[record.agent_did] = adapter
        self._emit_event("agent_register", f"Agent registered: {record.name}")
        return record

    def attach_adapter(self, agent_did: str, adapter: BaseAdapter) -> None:
        """Bind an adapter instance to an already-persisted agent record."""
        self._adapters[agent_did] = adapter

    # -----------------------------------------------------------------------
    # invocation
    # -----------------------------------------------------------------------

    async def invoke(
        self,
        messages: list[Message],
        capability: str = "text.qa",
        system_prompt: Optional[str] = None,
        agent_did: Optional[str] = None,
        locality: Locality = Locality.HOUSEHOLD,
        tags: Optional[list[str]] = None,
        max_cost_class: str = "paid",
        stream: bool = False,
    ) -> InvokeResponse:
        if self.household is None or self.scheduler is None:
            raise RuntimeError("Node not initialized")

        # Consider distributed inference configs alongside single-agent routing.
        # Only when the caller hasn't pinned a specific agent.
        if self._inference_registry is not None and agent_did is None:
            constraints = TaskConstraints(
                capability=capability,
                locality=locality,
                tags=list(tags or []),
                max_cost_class=max_cost_class,
            )
            healths = (
                dict(self.scheduler._device_health) if self.scheduler is not None else {}
            )
            best = self._inference_registry.get_best_config(constraints, healths)
            if best is not None and best.config_type == "distributed":
                try:
                    return await self._invoke_distributed(
                        messages, best, system_prompt=system_prompt, stream=stream
                    )
                except RuntimeError:
                    # llama-server unavailable or setup failed — fall through
                    # to single-agent routing.
                    pass

        agent: Optional[AgentRecord]
        if agent_did:
            agent = self.household.agents.get(agent_did)
            if agent is None:
                raise ValueError(f"Agent {agent_did} not registered")
        else:
            constraints = TaskConstraints(
                capability=capability,
                locality=locality,
                tags=list(tags or []),
                max_cost_class=max_cost_class,
            )
            agents = list(self.household.agents.values())
            chosen = self.scheduler.select_agent(
                agents, constraints, device_did_map=self._agent_device_map()
            )
            if chosen is None:
                raise RuntimeError(f"No agent satisfies capability={capability!r}")
            agent, _score = chosen

        adapter = self._adapters.get(agent.agent_did)
        if adapter is None:
            raise RuntimeError(
                f"Agent {agent.agent_did[:24]}... has a record but no live adapter on this device"
            )

        task_id = "task_" + uuid.uuid4().hex[:12]
        ucan_token = agent.ucan_token  # agent-scoped writes pass this through
        if self.memory is not None:
            self.memory.set(
                _canonical_request_key(task_id),
                {"messages": [m.to_dict() for m in messages], "capability": capability},
                ucan_token=ucan_token,
            )

        req = InvokeRequest(messages=list(messages), system_prompt=system_prompt)
        start = time.perf_counter()
        response = await adapter.invoke(req)
        elapsed = (time.perf_counter() - start) * 1000.0

        if self.memory is not None:
            self.memory.set(
                _canonical_response_key(task_id),
                {"content": response.content, "model": response.model, "usage": response.usage},
                ucan_token=ucan_token,
            )
            for m in messages:
                self.memory.log_append(
                    _canonical_history_key(task_id), m.to_dict(), ucan_token=ucan_token
                )
            self.memory.log_append(
                _canonical_history_key(task_id),
                Message(role="assistant", content=response.content).to_dict(),
                ucan_token=ucan_token,
            )

        # receipt
        keypair = self._device_keypair()
        chain = self._receipt_chains.setdefault(task_id, ReceiptChain())
        chain.add(
            Receipt(
                task_id=task_id,
                device_did=self.household.device_did or "",
                agent_did=agent.agent_did,
                action="invoke",
                model_used=response.model,
                tokens_used=response.total_tokens,
                latency_ms=elapsed,
                input_hash=_hash_messages(messages),
                output_hash=_hash_text(response.content),
                summary=response.content[:80],
            ),
            keypair=keypair,
            signer_did=self.household.device_did or "",
        )

        response.metadata["task_id"] = task_id
        return response

    async def invoke_stream(
        self,
        messages: list[Message],
        capability: str = "text.qa",
        system_prompt: Optional[str] = None,
        agent_did: Optional[str] = None,
        locality: Locality = Locality.HOUSEHOLD,
        tags: Optional[list[str]] = None,
        max_cost_class: str = "paid",
    ) -> AsyncIterator[str]:
        """Stream tokens from the best-scoring agent / inference configuration.

        Same scheduler logic as `invoke()` — but yields tokens as they arrive
        from the adapter (or distributed coordinator). Conversation history,
        the assistant reply, and a signed receipt are persisted after the
        stream completes. If the adapter doesn't implement `invoke_stream`
        (raises NotImplementedError), falls back to the batch `invoke()` path
        and yields the response as a single chunk.
        """
        if self.household is None or self.scheduler is None:
            raise RuntimeError("Node not initialized")

        # Distributed inference path (rare in unit tests; gated by llama-server).
        if self._inference_registry is not None and agent_did is None:
            constraints = TaskConstraints(
                capability=capability,
                locality=locality,
                tags=list(tags or []),
                max_cost_class=max_cost_class,
            )
            healths = (
                dict(self.scheduler._device_health) if self.scheduler is not None else {}
            )
            best_cfg = self._inference_registry.get_best_config(constraints, healths)
            if best_cfg is not None and best_cfg.config_type == "distributed":
                try:
                    coordinator = InferenceCoordinator(node=self, config=best_cfg)
                    ok = await coordinator.setup()
                    if ok:
                        try:
                            async for tok in coordinator.generate(
                                prompt=messages[-1].content if messages else "",
                                system_prompt=system_prompt,
                                stream=True,
                            ):
                                yield tok
                            await coordinator.teardown()
                            return
                        except Exception:
                            await coordinator.teardown()
                except RuntimeError:
                    pass  # fall through to single-agent path

        # Single-agent path: same selection logic as invoke().
        agent: Optional[AgentRecord]
        if agent_did:
            agent = self.household.agents.get(agent_did)
            if agent is None:
                raise ValueError(f"Agent {agent_did} not registered")
        else:
            constraints = TaskConstraints(
                capability=capability,
                locality=locality,
                tags=list(tags or []),
                max_cost_class=max_cost_class,
            )
            agents = list(self.household.agents.values())
            chosen = self.scheduler.select_agent(
                agents, constraints, device_did_map=self._agent_device_map()
            )
            if chosen is None:
                raise RuntimeError(f"No agent satisfies capability={capability!r}")
            agent, _score = chosen

        adapter = self._adapters.get(agent.agent_did)
        if adapter is None:
            raise RuntimeError(
                f"Agent {agent.agent_did[:24]}... has a record but no live adapter on this device"
            )

        task_id = "task_" + uuid.uuid4().hex[:12]
        ucan_token = agent.ucan_token  # may be None for unsigned agent records
        if self.memory is not None:
            self.memory.set(
                _canonical_request_key(task_id),
                {"messages": [m.to_dict() for m in messages], "capability": capability},
                ucan_token=ucan_token,
            )

        req = InvokeRequest(
            messages=list(messages), system_prompt=system_prompt, stream=True
        )
        start = time.perf_counter()
        chunks: list[str] = []
        try:
            async for token in adapter.invoke_stream(req):
                chunks.append(token)
                yield token
        except NotImplementedError:
            # Adapter doesn't support streaming — fall back to batch call.
            batch_req = InvokeRequest(messages=list(messages), system_prompt=system_prompt)
            response = await adapter.invoke(batch_req)
            chunks = [response.content]
            yield response.content
        elapsed = (time.perf_counter() - start) * 1000.0
        content = "".join(chunks)

        if self.memory is not None:
            self.memory.set(
                _canonical_response_key(task_id),
                {"content": content, "model": agent.model or agent.vendor, "usage": {}},
                ucan_token=ucan_token,
            )
            for m in messages:
                self.memory.log_append(
                    _canonical_history_key(task_id), m.to_dict(), ucan_token=ucan_token
                )
            self.memory.log_append(
                _canonical_history_key(task_id),
                Message(role="assistant", content=content).to_dict(),
                ucan_token=ucan_token,
            )

        # Receipt (no UCAN — internal node infrastructure).
        keypair = self._device_keypair()
        chain = self._receipt_chains.setdefault(task_id, ReceiptChain())
        chain.add(
            Receipt(
                task_id=task_id,
                device_did=self.household.device_did or "",
                agent_did=agent.agent_did,
                action="invoke_stream",
                model_used=agent.model or agent.vendor,
                tokens_used=len(chunks),
                latency_ms=elapsed,
                input_hash=_hash_messages(messages),
                output_hash=_hash_text(content),
                summary=content[:80],
            ),
            keypair=keypair,
            signer_did=self.household.device_did or "",
        )

    def _device_keypair(self) -> KeyPair:
        if self.household is None or self.household._device_key is None:
            raise RuntimeError("Device key unavailable")
        return self.household._device_key

    # -----------------------------------------------------------------------
    # handoff
    # -----------------------------------------------------------------------

    async def handoff(
        self,
        task_id: str,
        reason: HandoffReason,
        target_device_did: str,
        target_locality: str = "any",
        required_capabilities: Optional[list[str]] = None,
        max_cost_class: str = "paid",
    ) -> Continuation:
        """Send a task continuation to a specific peer.

        `target_device_did` is REQUIRED. There is no silent broadcast fallback —
        an unspecified target would mean spraying the conversation to every
        connected peer, which is unsafe for a privacy-first system. Use
        `handoff_to_best_peer` if you want the node to pick a target based on
        advertised peer capabilities.
        """
        if not target_device_did:
            raise ValueError(
                "handoff() requires an explicit target_device_did. "
                "Use handoff_to_best_peer() if you want the node to select one."
            )
        self._ensure_started()
        assert self.memory is not None and self.transport is not None and self.household is not None

        peer_conn = self.transport.get_peer(target_device_did)
        if peer_conn is None:
            raise ConnectionError(
                f"Target peer {target_device_did[:24]}... is not connected"
            )

        history = self.memory.log_read(_canonical_history_key(task_id))
        messages = [Message.from_dict(m) for m in history if "role" in m]

        cont = build_continuation(
            task_id=task_id,
            source_device_did=self.household.device_did or "",
            source_agent_did="",
            messages=messages,
            reason=reason,
            target_device_did=target_device_did,
            required_capabilities=list(required_capabilities or ["text.qa"]),
            locality_preference=target_locality,
            max_cost_class=max_cost_class,
        )

        keypair = self._device_keypair()
        # Sign the envelope BEFORE the receipt so the receipt hash and the
        # signed bytes describe the exact same object that goes on the wire.
        cont.sign(keypair, self.household.device_did or "")

        chain = self._receipt_chains.setdefault(task_id, ReceiptChain())
        chain.add(
            Receipt(
                task_id=task_id,
                continuation_id=cont.id,
                device_did=self.household.device_did or "",
                agent_did="",
                action="handoff_sent",
                summary=f"reason={reason.code} target={target_device_did[:16]}",
            ),
            keypair=keypair,
            signer_did=self.household.device_did or "",
        )

        msg = AriesMessage(
            type=MessageTypes.CONTINUATION,
            sender_did=self.household.device_did or "",
            body=cont.to_dict(),
        )
        await peer_conn.send(msg)
        self._emit_event(
            "handoff_sent",
            f"Handoff sent → {target_device_did[:16]}... ({reason.code})",
        )
        return cont

    async def handoff_to_best_peer(
        self,
        task_id: str,
        reason: HandoffReason,
        required_capabilities: Optional[list[str]] = None,
        target_locality: str = "any",
        max_cost_class: str = "paid",
    ) -> Continuation:
        """Choose a target peer based on ANNOUNCE-advertised capabilities, then handoff.

        Selection rule: among connected peers whose `capabilities` cover every
        required capability, pick the first one. Raises if no peer matches.
        """
        self._ensure_started()
        assert self.transport is not None
        needed = set(required_capabilities or ["text.qa"])
        for conn in self.transport.connected_peers():
            if needed.issubset(set(conn.peer.capabilities)):
                return await self.handoff(
                    task_id=task_id,
                    reason=reason,
                    target_device_did=conn.peer.device_did,
                    target_locality=target_locality,
                    required_capabilities=list(needed),
                    max_cost_class=max_cost_class,
                )
        raise RuntimeError(
            f"No connected peer advertises capabilities {sorted(needed)!r}"
        )

    async def resume_task(self, task_id: str) -> InvokeResponse:
        """Run the local scheduler against the locally-stored task history."""
        self._ensure_started()
        assert self.memory is not None
        history = self.memory.log_read(_canonical_history_key(task_id))
        messages = [Message.from_dict(m) for m in history if "role" in m]
        if not messages:
            raise RuntimeError(f"Task {task_id} has no history to resume")
        # drop trailing assistant messages so we re-invoke from the last user turn
        while messages and messages[-1].role == "assistant":
            messages.pop()
        return await self.invoke(messages)

    # -----------------------------------------------------------------------
    # distributed inference
    # -----------------------------------------------------------------------

    async def _invoke_distributed(
        self,
        messages: list[Message],
        config: InferenceConfig,
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
    ) -> InvokeResponse:
        """Run inference via llama.cpp RPC across the mesh.

        Sets up an InferenceCoordinator, streams tokens, persists the
        conversation to shared memory, and writes a signed receipt. On any
        failure during setup, raises RuntimeError so the caller in invoke()
        can fall back to single-agent routing.
        """
        assert self.household is not None
        task_id = "task_" + uuid.uuid4().hex[:12]
        start = time.perf_counter()
        coordinator = InferenceCoordinator(node=self, config=config)

        try:
            ok = await coordinator.setup()
            if not ok:
                raise RuntimeError(
                    f"Distributed inference setup failed for config {config.config_id}"
                )

            prompt = messages[-1].content if messages else ""
            chunks: list[str] = []
            async for token in coordinator.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                stream=stream,
            ):
                chunks.append(token)
            content = "".join(chunks)
        finally:
            await coordinator.teardown()

        elapsed_ms = (time.perf_counter() - start) * 1000.0

        if self.memory is not None:
            self.memory.set(
                _canonical_request_key(task_id),
                {
                    "messages": [m.to_dict() for m in messages],
                    "config_id": config.config_id,
                    "model": config.model_name,
                },
            )
            self.memory.set(
                _canonical_response_key(task_id),
                {"content": content, "model": config.model_name, "usage": {}},
            )
            for m in messages:
                self.memory.log_append(_canonical_history_key(task_id), m.to_dict())
            self.memory.log_append(
                _canonical_history_key(task_id),
                Message(role="assistant", content=content).to_dict(),
            )

        # Receipt for the distributed inference event.
        keypair = self._device_keypair()
        chain = self._receipt_chains.setdefault(task_id, ReceiptChain())
        chain.add(
            Receipt(
                task_id=task_id,
                device_did=self.household.device_did or "",
                agent_did="",
                action="invoke_distributed",
                model_used=config.model_name,
                tokens_used=len(chunks),
                latency_ms=elapsed_ms,
                input_hash=_hash_messages(messages),
                output_hash=_hash_text(content),
                summary=f"config={config.config_id} devices={len(config.devices)}",
            ),
            keypair=keypair,
            signer_did=self.household.device_did or "",
        )

        return InvokeResponse(
            content=content,
            model=config.model_name,
            usage={"prompt_tokens": 0, "completion_tokens": len(chunks)},
            latency_ms=elapsed_ms,
            metadata={"task_id": task_id, "config_id": config.config_id},
        )

    # -----------------------------------------------------------------------
    # handlers
    # -----------------------------------------------------------------------

    async def _handle_invoke(self, msg: AriesMessage, conn: PeerConnection) -> None:
        try:
            body = msg.body
            messages = [Message.from_dict(m) for m in body.get("messages", [])]
            capability = body.get("capability", "text.qa")
            self._emit_event(
                "invoke",
                f"Remote invoke: {capability} from {msg.sender_did[:16]}...",
            )
            response = await self.invoke(messages=messages, capability=capability)
            reply = AriesMessage(
                type=MessageTypes.INVOKE_RESULT,
                sender_did=self.household.device_did or "" if self.household else "",
                thread_id=msg.id,
                body={"content": response.content, "model": response.model, "usage": response.usage},
            )
            await conn.send(reply)
        except Exception as exc:
            err = AriesMessage(
                type=MessageTypes.ERROR,
                sender_did=self.household.device_did or "" if self.household else "",
                thread_id=msg.id,
                body={"error": repr(exc)},
            )
            try:
                await conn.send(err)
            except Exception:
                pass

    async def _handle_continuation(self, msg: AriesMessage, conn: PeerConnection) -> None:
        """Receive a signed continuation, verify, auto-resume locally, return result.

        **Canonical receive behavior is auto-resume.** ACK-then-wait is not a
        supported mode; the manual `aries resume <task_id>` CLI command remains
        available for debugging.

        Tamper resistance: if `cont.verify()` returns False, the message is
        dropped without ACK and a signed "handoff_received status=error"
        receipt is recorded locally. The sender gets a typed ERROR back.
        """
        try:
            cont = Continuation.from_dict(msg.body)
        except Exception:
            return
        assert self.memory is not None and self.household is not None

        keypair = self._device_keypair()
        chain = self._receipt_chains.setdefault(cont.task_id, ReceiptChain())

        # ----- signature gate -----
        if not cont.verify():
            chain.add(
                Receipt(
                    task_id=cont.task_id,
                    continuation_id=cont.id,
                    device_did=self.household.device_did or "",
                    agent_did="",
                    action="handoff_received",
                    status="error",
                    summary="rejected: bad signature",
                ),
                keypair=keypair,
                signer_did=self.household.device_did or "",
            )
            err = AriesMessage(
                type=MessageTypes.ERROR,
                sender_did=self.household.device_did or "",
                thread_id=cont.id,
                body={
                    "continuation_id": cont.id,
                    "error": "signature verification failed",
                },
            )
            try:
                await conn.send(err)
            except Exception:
                pass
            return

        self._emit_event(
            "handoff_recv",
            f"Continuation received from {cont.source_device_did[:16]}...",
        )

        # persist messages locally so resume / inspection works
        for m in cont.messages:
            self.memory.log_append(
                _canonical_history_key(cont.task_id), m.to_dict()
            )

        chain.add(
            Receipt(
                task_id=cont.task_id,
                continuation_id=cont.id,
                device_did=self.household.device_did or "",
                agent_did="",
                action="handoff_received",
                summary=f"from={cont.source_device_did[:20]}",
            ),
            keypair=keypair,
            signer_did=self.household.device_did or "",
        )

        ack = AriesMessage(
            type=MessageTypes.ACK,
            sender_did=self.household.device_did or "",
            thread_id=cont.id,
            body={"continuation_id": cont.id, "status": "received"},
        )
        try:
            await conn.send(ack)
        except Exception:
            pass

        # auto-resume
        cap = cont.required_capabilities[0] if cont.required_capabilities else "text.qa"
        try:
            locality = Locality(cont.locality_preference) if cont.locality_preference in ("local-only", "household", "any") else Locality.ANY
        except ValueError:
            locality = Locality.ANY
        try:
            response = await self.invoke(
                messages=list(cont.messages),
                capability=cap,
                locality=locality,
                max_cost_class=cont.max_cost_class,
            )
            # Mirror the assistant reply under the *continuation's* task_id so the
            # original log stays coherent on this device (invoke() logged under its
            # own freshly-minted task_id).
            self.memory.log_append(
                _canonical_history_key(cont.task_id),
                Message(role="assistant", content=response.content).to_dict(),
            )
        except Exception as exc:
            err = AriesMessage(
                type=MessageTypes.ERROR,
                sender_did=self.household.device_did or "",
                thread_id=cont.id,
                body={"error": repr(exc), "continuation_id": cont.id},
            )
            try:
                await conn.send(err)
            except Exception:
                pass
            return

        result = AriesMessage(
            type=MessageTypes.INVOKE_RESULT,
            sender_did=self.household.device_did or "",
            thread_id=cont.id,
            body={
                "continuation_id": cont.id,
                "task_id": cont.task_id,
                "content": response.content,
                "model": response.model,
                "usage": response.usage,
            },
        )
        try:
            await conn.send(result)
        except Exception:
            pass

    async def _handle_heartbeat(self, msg: AriesMessage, conn: PeerConnection) -> None:
        conn.peer.last_seen = time.time()

    async def _handle_profile_update(self, msg: AriesMessage, conn: PeerConnection) -> None:
        if self.scheduler is None:
            return
        try:
            health = DeviceHealth(**msg.body)
            self.scheduler.update_device_health(health)
        except (TypeError, ValueError):
            pass

    async def _handle_announce(self, msg: AriesMessage, conn: PeerConnection) -> None:
        # Build / update peer record from the announcement body
        conn.peer.last_seen = time.time()
        conn.peer.name = msg.body.get("name", conn.peer.name)
        conn.peer.capabilities = list(msg.body.get("capabilities", conn.peer.capabilities))

    async def _handle_pairing_request(self, msg: AriesMessage, conn: PeerConnection) -> None:
        """Inviter side: receive joiner's device DID + presented code; reply with membership UCAN."""
        if self.household is None:
            return
        body = msg.body
        try:
            membership_jwt = self.household.accept_pairing_request(
                candidate_device_did=body["device_did"],
                candidate_device_name=body.get("name", "joiner"),
                candidate_platform=body.get("platform", "unknown"),
                presented_code=body["code"],
            )
        except Exception as exc:
            err = AriesMessage(
                type=MessageTypes.ERROR,
                sender_did=self.household.device_did or "",
                thread_id=msg.id,
                body={"error": repr(exc)},
            )
            try:
                await conn.send(err)
            except Exception:
                pass
            return

        reply = AriesMessage(
            type=MessageTypes.PAIRING_ACCEPT,
            sender_did=self.household.device_did or "",
            thread_id=msg.id,
            body={
                "membership_ucan": membership_jwt,
                "user_root_did": self.household.user_root_did,
            },
        )
        try:
            await conn.send(reply)
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # inference message handlers (Feature 2)
    # -----------------------------------------------------------------------

    async def _handle_inference_setup(self, msg: AriesMessage, conn: PeerConnection) -> None:
        """A peer is asking this node to start an rpc-server for distributed inference."""
        if self.household is None:
            return
        body = msg.body
        session_id = str(body.get("session_id", ""))
        port = int(body.get("port", 50052))

        rpc_path = None
        if self._local_inference_capability is not None:
            rpc_path = self._local_inference_capability.rpc_server_path

        if not rpc_path:
            err = AriesMessage(
                type=MessageTypes.ERROR,
                sender_did=self.household.device_did or "",
                thread_id=msg.id,
                body={
                    "error": "rpc-server not available on this device",
                    "session_id": session_id,
                },
            )
            try:
                await conn.send(err)
            except Exception:
                pass
            return

        try:
            proc = await asyncio.create_subprocess_exec(
                rpc_path,
                "--host", "0.0.0.0",
                "--port", str(port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Brief wait for the listening socket to bind.
            await asyncio.sleep(0.5)
            self._inference_rpc_processes[session_id] = proc

            ready = AriesMessage(
                type=MessageTypes.INFERENCE_READY,
                sender_did=self.household.device_did or "",
                thread_id=msg.id,
                body={"session_id": session_id, "port": port},
            )
            await conn.send(ready)
        except Exception as exc:
            err = AriesMessage(
                type=MessageTypes.ERROR,
                sender_did=self.household.device_did or "",
                thread_id=msg.id,
                body={"error": repr(exc), "session_id": session_id},
            )
            try:
                await conn.send(err)
            except Exception:
                pass

    async def _handle_inference_ready(self, msg: AriesMessage, conn: PeerConnection) -> None:
        """A worker reports its rpc-server is up; resolve the pending future."""
        fut = self._inference_ready_futures.pop(msg.sender_did, None)
        if fut is not None and not fut.done():
            fut.set_result(True)

    async def _handle_inference_teardown(self, msg: AriesMessage, conn: PeerConnection) -> None:
        """Peer is finishing the session; stop our rpc-server subprocess."""
        if self.household is None:
            return
        session_id = str(msg.body.get("session_id", ""))
        proc = self._inference_rpc_processes.pop(session_id, None)
        if proc is not None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except (ProcessLookupError, Exception):
                pass
        ack = AriesMessage(
            type=MessageTypes.ACK,
            sender_did=self.household.device_did or "",
            thread_id=msg.id,
            body={"session_id": session_id, "status": "torn-down"},
        )
        try:
            await conn.send(ack)
        except Exception:
            pass

    async def _handle_inference_probe(self, msg: AriesMessage, conn: PeerConnection) -> None:
        """Echo the probe payload back for round-trip / bandwidth measurement."""
        if self.household is None:
            return
        reply = AriesMessage(
            type=MessageTypes.INFERENCE_PROBE_RESPONSE,
            sender_did=self.household.device_did or "",
            thread_id=msg.id,
            body=dict(msg.body),
        )
        try:
            await conn.send(reply)
        except Exception:
            pass

    async def _handle_inference_probe_response(
        self, msg: AriesMessage, conn: PeerConnection
    ) -> None:
        """Resolve the pending measure_peer_network future for this probe id."""
        thread = msg.thread_id or ""
        fut = _PENDING_PROBES.pop(thread, None)
        if fut is None or fut.done():
            return
        sent_ts = msg.body.get("ts")
        if isinstance(sent_ts, (int, float)):
            elapsed_ms = (time.perf_counter() - float(sent_ts)) * 1000.0
            fut.set_result(elapsed_ms)
        else:
            fut.set_result(0.0)

    async def _handle_stream_chunk(self, msg: AriesMessage, conn: PeerConnection) -> None:
        """Receive a streamed token from a remote inference session.

        Persist it to the relevant task's history. The CLI streaming UX is
        responsible for any live-display behavior; v0.2 just stores the chunks.
        """
        if self.memory is None:
            return
        body = msg.body
        task_id = str(body.get("task_id", ""))
        token = str(body.get("token", ""))
        done = bool(body.get("done", False))
        if not task_id or (not token and not done):
            return
        self.memory.log_append(
            _canonical_history_key(task_id),
            {"role": "assistant_chunk", "content": token, "done": done},
        )

    # -----------------------------------------------------------------------
    # discovery / health callbacks
    # -----------------------------------------------------------------------

    def _on_peer_discovered(self, peer: PeerInfo) -> None:
        if self.transport is None or self.household is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._connect_and_announce(peer))

    async def _connect_and_announce(self, peer: PeerInfo) -> None:
        if self.transport is None or self.household is None:
            return
        try:
            conn = await self.transport.connect_to_peer(peer)
        except (OSError, ConnectionError):
            return
        self._emit_event(
            "peer_connect",
            f"Peer connected: {peer.name or peer.device_did[:16]}",
        )
        announce = AriesMessage(
            type=MessageTypes.ANNOUNCE,
            sender_did=self.household.device_did or "",
            body={
                "name": self._self_device_name(),
                "capabilities": self._aggregate_capabilities(),
                "agents": [
                    {"did": a.agent_did, "name": a.name, "capabilities": a.capabilities}
                    for a in self.household.agents.values()
                ],
            },
        )
        try:
            await conn.send(announce)
        except Exception:
            return
        if self.sync is not None:
            await self.sync.sync_with_peer(conn)

    def _on_health_update(self, health: DeviceHealth) -> None:
        if self.scheduler is not None:
            self.scheduler.update_device_health(health)
        if self.transport is None or self.household is None:
            return
        msg = AriesMessage(
            type=MessageTypes.PROFILE_UPDATE,
            sender_did=self.household.device_did or "",
            body={
                "device_did": health.device_did,
                "cpu_percent": health.cpu_percent,
                "ram_available_gb": health.ram_available_gb,
                "ram_total_gb": health.ram_total_gb,
                "battery_pct": health.battery_pct,
                "charging": health.charging,
                "thermal": health.thermal,
            },
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.transport.broadcast(msg))
        except RuntimeError:
            pass

    # -----------------------------------------------------------------------
    # pairing (joiner side)
    # -----------------------------------------------------------------------

    async def pair_with_invitation(
        self,
        code: str,
        device_name: str,
        platform: str,
    ) -> dict[str, str]:
        """Joiner: bring up a household on this fresh device using an invitation code.

        Discovers the inviter via mDNS, sends a PAIRING_REQUEST with the code,
        awaits PAIRING_ACCEPT, persists the membership UCAN.
        """
        if self.household is not None and self.household.is_initialized:
            raise RuntimeError(f"Household at {self.data_dir} already initialized")

        self.household = Household(data_dir=self.data_dir)
        device_did, _ = self.household.initialize_joiner(device_name=device_name, platform=platform)

        # bring up transport — keypair is set by initialize_joiner above
        self.transport = TransportServer(device_keypair=self.household._device_key)
        port = await self.transport.start()
        accept_future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()

        async def _on_accept(msg: AriesMessage, conn: PeerConnection) -> None:
            if not accept_future.done():
                accept_future.set_result(msg.body)

        self.transport.on_message(MessageTypes.PAIRING_ACCEPT, _on_accept)

        # discover all aries services (any household)
        self.discovery = DiscoveryService(
            device_did=device_did,
            device_name=device_name,
            household_tag="*pending*",
            port=port,
        )

        # We need to find *any* peer to ask. Use a permissive discovery that
        # ignores the household_tag filter for the joiner.
        from .transport.discovery import SERVICE_TYPE  # noqa: F401
        from zeroconf import IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

        zc = AsyncZeroconf(ip_version=IPVersion.V4Only)

        peers_found: asyncio.Queue[PeerInfo] = asyncio.Queue()

        async def _resolve(name: str) -> None:
            info = AsyncServiceInfo(SERVICE_TYPE, name)
            ok = await info.async_request(zc.zeroconf, 3000)
            if not ok:
                return
            props = info.decoded_properties or {}
            if props.get("did") == device_did:
                return
            addrs = info.parsed_scoped_addresses() or info.parsed_addresses()
            if not addrs or not info.port:
                return
            peer = PeerInfo(
                device_did=props.get("did", ""),
                name=props.get("name", name),
                host=addrs[0],
                port=info.port,
                household_tag=props.get("household", ""),
            )
            await peers_found.put(peer)

        def _on_state(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Added:
                asyncio.ensure_future(_resolve(name))

        browser = AsyncServiceBrowser(zc.zeroconf, SERVICE_TYPE, handlers=[_on_state])

        try:
            membership: Optional[str] = None
            deadline = time.time() + 30
            while time.time() < deadline and membership is None:
                try:
                    peer = await asyncio.wait_for(peers_found.get(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                try:
                    conn = await self.transport.connect_to_peer(peer)
                except (OSError, ConnectionError):
                    continue
                request = AriesMessage(
                    type=MessageTypes.PAIRING_REQUEST,
                    sender_did=device_did,
                    body={
                        "device_did": device_did,
                        "name": device_name,
                        "platform": platform,
                        "code": code,
                    },
                )
                try:
                    await conn.send(request)
                except Exception:
                    continue
                try:
                    body = await asyncio.wait_for(accept_future, timeout=10)
                    membership = body.get("membership_ucan")
                    if membership:
                        break
                except asyncio.TimeoutError:
                    continue

            if not membership:
                raise RuntimeError("No inviter responded with a membership UCAN")
            return self.household.complete_joining(membership, device_name, platform)
        finally:
            await browser.async_cancel()
            await zc.async_close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _hash_messages(messages: list[Message]) -> str:
    h = hashlib.sha256()
    for m in messages:
        h.update(m.role.encode("utf-8"))
        h.update(b"\x00")
        h.update(m.content.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---- canonical resource keys (Fix 5) -----------------------------------------
# All task-scoped memory uses the `aries:context://tasks/<id>/<bucket>` form.

def _canonical_history_key(task_id: str) -> str:
    return f"aries:context://tasks/{task_id}/history"


def _canonical_request_key(task_id: str) -> str:
    return f"aries:context://tasks/{task_id}/request"


def _canonical_response_key(task_id: str) -> str:
    return f"aries:context://tasks/{task_id}/response"
