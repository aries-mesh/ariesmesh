"""Wire-level message envelope, TCP peer connections, and transport server.

Spec reference: §8. Plus pairing message types per plan.
"""
from __future__ import annotations

import asyncio
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import cbor2


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------

class MessageTypes:
    ANNOUNCE = "aries/v0.1/discovery/announce"
    QUERY = "aries/v0.1/discovery/query"
    RESULT = "aries/v0.1/discovery/result"
    INVOKE = "aries/v0.1/agent/invoke"
    INVOKE_RESULT = "aries/v0.1/agent/result"
    ERROR = "aries/v0.1/agent/error"
    CONTINUATION = "aries/v0.1/handoff/continuation"
    ACK = "aries/v0.1/handoff/ack"
    RECEIPT = "aries/v0.1/handoff/receipt"
    REVOCATION_APPEND = "aries/v0.1/revocation/append"
    REVOCATION_SYNC = "aries/v0.1/revocation/sync"
    MEMORY_SYNC = "aries/v0.1/memory/sync"
    MEMORY_UPDATE = "aries/v0.1/memory/update"
    HEARTBEAT = "aries/v0.1/health/heartbeat"
    PROFILE_UPDATE = "aries/v0.1/health/profile"
    PAIRING_OFFER = "aries/v0.1/pairing/offer"
    PAIRING_REQUEST = "aries/v0.1/pairing/request"
    PAIRING_ACCEPT = "aries/v0.1/pairing/accept"


# Maximum message size (sanity limit)
MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB


# ---------------------------------------------------------------------------
# AriesMessage
# ---------------------------------------------------------------------------

@dataclass
class AriesMessage:
    type: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sender_did: str = ""
    thread_id: Optional[str] = None
    body: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    seq: int = 0

    def to_cbor(self) -> bytes:
        payload: dict[str, Any] = {
            "@type": self.type,
            "@id": self.id,
            "sender": self.sender_did,
            "body": self.body,
            "ts": self.timestamp,
            "seq": self.seq,
        }
        if self.thread_id is not None:
            payload["@thread"] = self.thread_id
        return cbor2.dumps(payload)

    @classmethod
    def from_cbor(cls, data: bytes) -> "AriesMessage":
        d = cbor2.loads(data)
        return cls(
            type=d["@type"],
            id=d.get("@id", uuid.uuid4().hex),
            sender_did=d.get("sender", ""),
            thread_id=d.get("@thread"),
            body=d.get("body", {}),
            timestamp=d.get("ts", time.time()),
            seq=d.get("seq", 0),
        )

    def to_bytes(self) -> bytes:
        payload = self.to_cbor()
        if len(payload) > MAX_MESSAGE_SIZE:
            raise ValueError(f"Message size {len(payload)} exceeds {MAX_MESSAGE_SIZE}")
        return struct.pack("!I", len(payload)) + payload


# ---------------------------------------------------------------------------
# PeerInfo
# ---------------------------------------------------------------------------

@dataclass
class PeerInfo:
    device_did: str
    name: str
    host: str
    port: int
    household_tag: str
    capabilities: list[str] = field(default_factory=list)
    last_seen: float = field(default_factory=time.time)
    latency_ms: Optional[float] = None


# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

MessageHandler = Callable[["AriesMessage", "PeerConnection"], Awaitable[None]]


# ---------------------------------------------------------------------------
# PeerConnection
# ---------------------------------------------------------------------------

class PeerConnection:
    def __init__(
        self,
        peer: PeerInfo,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.peer = peer
        self.reader = reader
        self.writer = writer
        self._seq = 0
        self._connected = True
        self._send_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def send(self, msg: AriesMessage) -> None:
        if not self._connected:
            raise ConnectionError("Peer connection closed")
        async with self._send_lock:
            self._seq += 1
            msg.seq = self._seq
            self.writer.write(msg.to_bytes())
            await self.writer.drain()

    async def recv(self) -> Optional[AriesMessage]:
        try:
            header = await self.reader.readexactly(4)
            (length,) = struct.unpack("!I", header)
            if length > MAX_MESSAGE_SIZE:
                self._connected = False
                raise ValueError(f"Message size {length} exceeds {MAX_MESSAGE_SIZE}")
            payload = await self.reader.readexactly(length)
            return AriesMessage.from_cbor(payload)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self._connected = False
            return None

    async def close(self) -> None:
        self._connected = False
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TransportServer
# ---------------------------------------------------------------------------

class TransportServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 0) -> None:
        self.host = host
        self.port = port
        self._server: Optional[asyncio.Server] = None
        self._handlers: dict[str, MessageHandler] = {}
        self._connections: dict[str, PeerConnection] = {}
        self._recv_tasks: set[asyncio.Task[None]] = set()

    def on_message(self, msg_type: str, handler: MessageHandler) -> None:
        self._handlers[msg_type] = handler

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle_connection, self.host, self.port)
        sockets = self._server.sockets or []
        if sockets:
            self.port = sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        # close connections + cancel recv loops FIRST; otherwise wait_closed
        # blocks on Windows asyncio while inbound connections remain open
        for conn in list(self._connections.values()):
            await conn.close()
        self._connections.clear()
        for task in list(self._recv_tasks):
            task.cancel()
        if self._recv_tasks:
            await asyncio.gather(*self._recv_tasks, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass

    async def connect_to_peer(self, peer: PeerInfo) -> PeerConnection:
        reader, writer = await asyncio.open_connection(peer.host, peer.port)
        conn = PeerConnection(peer, reader, writer)
        if peer.device_did:
            self._connections[peer.device_did] = conn
        task = asyncio.create_task(self._receive_loop(conn))
        self._recv_tasks.add(task)
        task.add_done_callback(self._recv_tasks.discard)
        return conn

    def get_peer(self, device_did: str) -> Optional[PeerConnection]:
        return self._connections.get(device_did)

    def connected_peers(self) -> list[PeerConnection]:
        return [c for c in self._connections.values() if c.is_connected]

    async def broadcast(self, msg: AriesMessage) -> None:
        for did, conn in list(self._connections.items()):
            try:
                await conn.send(msg)
            except Exception:
                # connection probably died; remove
                self._connections.pop(did, None)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peerinfo = PeerInfo(device_did="unknown", name="", host="", port=0, household_tag="")
        try:
            sockname = writer.get_extra_info("peername")
            if sockname:
                peerinfo.host, peerinfo.port = sockname[0], sockname[1]
        except Exception:
            pass
        conn = PeerConnection(peerinfo, reader, writer)
        await self._receive_loop(conn)

    async def _receive_loop(self, conn: PeerConnection) -> None:
        try:
            while conn.is_connected:
                msg = await conn.recv()
                if msg is None:
                    break
                if msg.sender_did and conn.peer.device_did == "unknown":
                    conn.peer.device_did = msg.sender_did
                    self._connections[msg.sender_did] = conn
                handler = self._handlers.get(msg.type)
                if handler is None:
                    continue
                try:
                    await handler(msg, conn)
                except Exception:
                    # Handler errors must not kill the loop
                    pass
        finally:
            await conn.close()
            did = conn.peer.device_did
            if did and self._connections.get(did) is conn:
                self._connections.pop(did, None)
