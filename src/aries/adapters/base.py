"""Abstract base class for vendor adapters and the shared request/response types.

Spec reference: §14.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional


# ---------------------------------------------------------------------------
# Message / Request / Response
# ---------------------------------------------------------------------------

@dataclass
class Message:
    role: str  # "user" | "assistant" | "system"
    content: str
    name: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content, "timestamp": self.timestamp}
        if self.name is not None:
            d["name"] = self.name
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            role=d["role"],
            content=d["content"],
            name=d.get("name"),
            timestamp=d.get("timestamp", time.time()),
        )


@dataclass
class InvokeRequest:
    messages: list[Message]
    system_prompt: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.7
    stop_sequences: list[str] = field(default_factory=list)
    stream: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InvokeResponse:
    content: str
    model: str
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("prompt_tokens", 0)) + int(self.usage.get("completion_tokens", 0))


# ---------------------------------------------------------------------------
# BaseAdapter
# ---------------------------------------------------------------------------

class BaseAdapter(ABC):
    vendor: str = ""
    model: str = ""

    @abstractmethod
    async def invoke(self, request: InvokeRequest) -> InvokeResponse: ...

    @abstractmethod
    async def invoke_stream(self, request: InvokeRequest) -> AsyncIterator[str]: ...

    @abstractmethod
    async def health_check(self) -> bool: ...

    @abstractmethod
    def capabilities(self) -> dict[str, Any]: ...
