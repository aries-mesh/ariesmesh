"""Two-phase memory sync protocol over the transport layer.

Spec reference: §13.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..transport.peer import AriesMessage, MessageTypes, PeerConnection, TransportServer
from .store import LWWEntry, MemoryStore


SYNC_DEBOUNCE_MS = 100
SYNC_INTERVAL_S = 30


class MemorySyncService:
    def __init__(
        self,
        store: MemoryStore,
        transport: TransportServer,
        device_did: str,
    ) -> None:
        self.store = store
        self.transport = transport
        self.device_did = device_did
        self._running = False
        self._periodic_task: Optional[asyncio.Task[None]] = None
        self._debounce_task: Optional[asyncio.Task[None]] = None

        transport.on_message(MessageTypes.MEMORY_SYNC, self._handle_sync)
        transport.on_message(MessageTypes.MEMORY_UPDATE, self._handle_update)
        store.on_change(self._on_local_change)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._periodic_task = asyncio.create_task(self._periodic_loop())

    async def stop(self) -> None:
        self._running = False
        for task in (self._periodic_task, self._debounce_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # -----------------------------------------------------------------------
    # outgoing
    # -----------------------------------------------------------------------

    async def sync_with_peer(self, peer_conn: PeerConnection) -> None:
        msg = AriesMessage(
            type=MessageTypes.MEMORY_SYNC,
            sender_did=self.device_did,
            body={"phase": "request", "state": self.store.get_sync_state()},
        )
        try:
            await peer_conn.send(msg)
        except Exception:
            pass

    async def _push_to_peers(self) -> None:
        msg = AriesMessage(
            type=MessageTypes.MEMORY_SYNC,
            sender_did=self.device_did,
            body={"phase": "request", "state": self.store.get_sync_state()},
        )
        await self.transport.broadcast(msg)

    def _on_local_change(self, key: str, value: Any) -> None:
        if self._debounce_task is not None and not self._debounce_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._debounce_task = loop.create_task(self._debounce_and_push())

    async def _debounce_and_push(self) -> None:
        try:
            await asyncio.sleep(SYNC_DEBOUNCE_MS / 1000.0)
            await self._push_to_peers()
        except asyncio.CancelledError:
            pass

    async def _periodic_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(SYNC_INTERVAL_S)
                if not self._running:
                    break
                await self._push_to_peers()
        except asyncio.CancelledError:
            pass

    # -----------------------------------------------------------------------
    # incoming
    # -----------------------------------------------------------------------

    async def _handle_sync(self, msg: AriesMessage, conn: PeerConnection) -> None:
        phase = msg.body.get("phase")
        remote_state = msg.body.get("state", {})
        if phase == "request":
            diff = self.store.compute_diff(remote_state)
            self.store.apply_diff(_diff_from_wire(msg.body.get("diff", {})))
            response = AriesMessage(
                type=MessageTypes.MEMORY_SYNC,
                sender_did=self.device_did,
                body={
                    "phase": "response",
                    "diff": diff,
                    "state": self.store.get_sync_state(),
                },
            )
            try:
                await conn.send(response)
            except Exception:
                pass
        elif phase == "response":
            self.store.apply_diff(_diff_from_wire(msg.body.get("diff", {})))
            our_diff = self.store.compute_diff(remote_state)
            if our_diff.get("registers") or our_diff.get("logs"):
                update = AriesMessage(
                    type=MessageTypes.MEMORY_UPDATE,
                    sender_did=self.device_did,
                    body={"diff": our_diff},
                )
                try:
                    await conn.send(update)
                except Exception:
                    pass

    async def _handle_update(self, msg: AriesMessage, conn: PeerConnection) -> None:
        self.store.apply_diff(_diff_from_wire(msg.body.get("diff", {})))


def _diff_from_wire(diff: dict[str, Any]) -> dict[str, Any]:
    """Diff arrives as plain dicts; convert register payloads via LWWEntry.from_dict
    happens inside apply_diff. This helper is currently a no-op but kept for symmetry."""
    return diff
