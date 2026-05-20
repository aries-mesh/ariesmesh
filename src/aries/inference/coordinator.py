"""Lifecycle manager for a single distributed-inference session.

The coordinator owns three things:
  1. The rpc-server subprocesses running on worker peers (started via
     INFERENCE_SETUP messages over the encrypted transport).
  2. A local llama-server subprocess that loads the GGUF model and federates
     tensor shards to the workers.
  3. A streaming HTTP client that consumes llama-server's OpenAI-compatible
     /v1/chat/completions SSE stream and yields tokens.

When llama-server isn't installed locally, `setup()` still negotiates worker
readiness over the transport — the actual generation is gated behind a
runtime check, so unit tests can exercise the setup/teardown protocol
without needing real binaries on disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

import httpx

from .registry import InferenceConfig

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

DEFAULT_SETUP_TIMEOUT_S = 30.0
DEFAULT_RPC_PORT = 50052
DEFAULT_LLAMA_HOST = "127.0.0.1"
DEFAULT_LLAMA_PORT = 8081


class InferenceCoordinator:
    """Brings up, drives, and tears down a single distributed-inference session."""

    def __init__(
        self,
        node: Any,
        config: InferenceConfig,
        *,
        setup_timeout: float = DEFAULT_SETUP_TIMEOUT_S,
        llama_host: str = DEFAULT_LLAMA_HOST,
        llama_port: int = DEFAULT_LLAMA_PORT,
        rpc_port: int = DEFAULT_RPC_PORT,
    ) -> None:
        self.node = node
        self.config = config
        self._setup_timeout = setup_timeout
        self._llama_host = llama_host
        self._llama_port = llama_port
        self._rpc_port = rpc_port

        self._llama_process: Optional[asyncio.subprocess.Process] = None
        self._workers_ready: dict[str, bool] = {}
        self._has_local_llama_server: bool = False
        self._active: bool = False
        self._health_task: Optional[asyncio.Task[None]] = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def workers_ready(self) -> dict[str, bool]:
        return dict(self._workers_ready)

    # ------------------------------------------------------------------ setup

    async def setup(self) -> bool:
        """Bring up rpc-servers on workers + (best-effort) llama-server locally.

        Returns True if every worker reported INFERENCE_READY before the
        timeout. The local llama-server is started only if its binary is
        discoverable; absence is logged but doesn't fail setup, because the
        coordinator is reusable in environments that drive llama-server
        externally.
        """
        # Local import keeps the transport / inference packages decoupled.
        from ..transport.peer import AriesMessage, MessageTypes

        workers = [r for r in self.config.devices if r.role == "worker"]
        if not workers and self.config.config_type == "distributed":
            logger.warning("Distributed config %s has no worker roles", self.config.config_id)
            return False

        for role in workers:
            peer_conn = self.node.transport.get_peer(role.device_did)
            if peer_conn is None:
                logger.warning(
                    "Cannot reach worker %s for session %s",
                    role.device_did[:24],
                    self.config.config_id,
                )
                return False

            fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
            self.node._inference_ready_futures[role.device_did] = fut

            setup_msg = AriesMessage(
                type=MessageTypes.INFERENCE_SETUP,
                sender_did=self.node.household.device_did or "",
                body={
                    "session_id": self.config.config_id,
                    "model_name": self.config.model_name,
                    "port": self._rpc_port,
                },
            )
            try:
                await peer_conn.send(setup_msg)
            except Exception as exc:
                logger.warning("Failed to send INFERENCE_SETUP to %s: %s", role.device_did[:24], exc)
                self.node._inference_ready_futures.pop(role.device_did, None)
                return False

            try:
                await asyncio.wait_for(fut, timeout=self._setup_timeout)
                self._workers_ready[role.device_did] = True
            except asyncio.TimeoutError:
                logger.warning(
                    "Worker %s did not become ready within %.1fs",
                    role.device_did[:24],
                    self._setup_timeout,
                )
                self.node._inference_ready_futures.pop(role.device_did, None)
                return False

        # Try to start llama-server locally (best-effort).
        self._has_local_llama_server = await self._maybe_start_llama_server()

        self._active = True
        self._health_task = asyncio.create_task(self._monitor_health())
        return True

    async def _maybe_start_llama_server(self) -> bool:
        """Start the local llama-server subprocess if the binary is available."""
        registry = getattr(self.node, "_inference_registry", None)
        if registry is None:
            return False
        host_did = self.node.household.device_did or ""
        host_cap = registry._device_capabilities.get(host_did)
        if host_cap is None or not host_cap.llama_cpp_path:
            logger.info("llama-server not available locally; skipping host process start")
            return False

        model_path = self._resolve_model_path(host_cap)
        if not model_path:
            logger.warning("Could not locate model file %s", self.config.model_name)
            return False

        cmd = [
            host_cap.llama_cpp_path,
            "--model", model_path,
            "--port", str(self._llama_port),
            "--host", self._llama_host,
        ]
        if self.config.rpc_endpoints:
            cmd += ["--rpc", ",".join(self.config.rpc_endpoints)]
        if self.config.tensor_split:
            cmd += ["--tensor-split", ",".join(f"{s:.3f}" for s in self.config.tensor_split)]

        try:
            self._llama_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as exc:
            logger.warning("Failed to start llama-server: %s", exc)
            return False

        # Poll the health endpoint
        return await self._wait_for_llama_health(timeout_s=30.0)

    def _resolve_model_path(self, host_cap: Any) -> Optional[str]:
        for model in host_cap.available_models:
            if model.name == self.config.model_name:
                return model.path
        return None

    async def _wait_for_llama_health(self, timeout_s: float) -> bool:
        deadline = time.perf_counter() + timeout_s
        url = f"http://{self._llama_host}:{self._llama_port}/health"
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.perf_counter() < deadline:
                try:
                    r = await client.get(url)
                    if r.status_code == 200:
                        return True
                except (httpx.HTTPError, OSError):
                    pass
                await asyncio.sleep(0.5)
        return False

    # --------------------------------------------------------------- generate

    async def generate(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        stream: bool = True,
    ) -> AsyncIterator[str]:
        """Stream tokens from the local llama-server.

        Yields one chunk per SSE delta. If llama-server isn't running, raises
        RuntimeError — the caller is expected to fall back to single-agent
        inference in that case.
        """
        if not self._has_local_llama_server:
            raise RuntimeError(
                "llama-server is not running locally; cannot generate. "
                "Install llama.cpp and re-probe inference capability."
            )

        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        url = f"http://{self._llama_host}:{self._llama_port}/v1/chat/completions"
        payload = {
            "model": self.config.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }

        async with httpx.AsyncClient(timeout=None) as client:
            if not stream:
                r = await client.post(url, json=payload)
                r.raise_for_status()
                data = r.json()
                yield data["choices"][0]["message"]["content"]
                return

            async with client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    token = delta.get("content")
                    if token:
                        yield token

    # ---------------------------------------------------------------- teardown

    async def teardown(self) -> None:
        """Kill local llama-server, ask workers to stop their rpc-servers."""
        from ..transport.peer import AriesMessage, MessageTypes

        if self._health_task is not None:
            self._health_task.cancel()
            try:
                await self._health_task
            except (asyncio.CancelledError, Exception):
                pass
            self._health_task = None

        if self._llama_process is not None:
            try:
                self._llama_process.terminate()
                try:
                    await asyncio.wait_for(self._llama_process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._llama_process.kill()
                    await self._llama_process.wait()
            except (ProcessLookupError, Exception):
                pass
            self._llama_process = None

        for role in self.config.devices:
            if role.role != "worker":
                continue
            peer_conn = self.node.transport.get_peer(role.device_did)
            if peer_conn is None:
                continue
            msg = AriesMessage(
                type=MessageTypes.INFERENCE_TEARDOWN,
                sender_did=self.node.household.device_did or "",
                body={"session_id": self.config.config_id},
            )
            try:
                await peer_conn.send(msg)
            except Exception:
                pass

        self._active = False
        self._workers_ready.clear()

    # ---------------------------------------------------------------- monitor

    async def _monitor_health(self) -> None:
        """Background heartbeat. v0.3 will react to degradation; for now just log."""
        try:
            while self._active:
                await asyncio.sleep(10.0)
                # Disconnected worker → trigger teardown (one-shot, no repartition yet)
                disconnected = []
                for role in self.config.devices:
                    if role.role != "worker":
                        continue
                    if self.node.transport.get_peer(role.device_did) is None:
                        disconnected.append(role.device_did)
                if disconnected:
                    logger.warning(
                        "Worker(s) disconnected from session %s: %s",
                        self.config.config_id,
                        [d[:16] for d in disconnected],
                    )
                    break
        except asyncio.CancelledError:
            return
