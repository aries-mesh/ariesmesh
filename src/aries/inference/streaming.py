"""Token streaming from llama-server SSE → Aries STREAM_CHUNK envelopes.

Used when a remote device (the requester) asked this device to run inference
on its behalf. The local llama-server emits an SSE token stream; this class
wraps each token in a signed-and-encrypted STREAM_CHUNK message and forwards
it back over the encrypted transport, while also yielding the token to the
local caller so it can be persisted to memory.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

import httpx

if TYPE_CHECKING:
    from ..transport.peer import TransportServer

logger = logging.getLogger(__name__)


class StreamForwarder:
    """Stream tokens from a local llama-server and forward them via transport."""

    def __init__(self, sender_did: str) -> None:
        self._sender_did = sender_did

    async def stream_from_llama_server(
        self,
        *,
        server_url: str,
        messages: list[dict[str, Any]],
        task_id: str,
        transport: Optional["TransportServer"],
        requesting_device_did: Optional[str],
        model_name: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Pull tokens from llama-server's OpenAI-compatible streaming endpoint.

        Each token is wrapped in a STREAM_CHUNK AriesMessage and pushed to
        ``requesting_device_did`` via the encrypted transport (if both are
        set). The token is also yielded to the local caller.
        """
        from ..transport.peer import AriesMessage, MessageTypes

        full_messages: list[dict[str, Any]] = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        payload = {
            "model": model_name,
            "messages": full_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        url = server_url.rstrip("/") + "/v1/chat/completions"

        index = 0
        peer_conn = None
        if transport is not None and requesting_device_did:
            peer_conn = transport.get_peer(requesting_device_did)

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        if peer_conn is not None:
                            done_msg = AriesMessage(
                                type=MessageTypes.STREAM_CHUNK,
                                sender_did=self._sender_did,
                                body={
                                    "task_id": task_id,
                                    "token": "",
                                    "index": index,
                                    "done": True,
                                },
                            )
                            try:
                                await peer_conn.send(done_msg)
                            except Exception:
                                pass
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
                    if not token:
                        continue
                    if peer_conn is not None:
                        chunk_msg = AriesMessage(
                            type=MessageTypes.STREAM_CHUNK,
                            sender_did=self._sender_did,
                            body={
                                "task_id": task_id,
                                "token": token,
                                "index": index,
                                "done": False,
                            },
                        )
                        try:
                            await peer_conn.send(chunk_msg)
                        except Exception:
                            pass
                    index += 1
                    yield token
