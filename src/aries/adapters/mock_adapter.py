"""MockAdapter — deterministic, offline adapter for unit tests and demos."""
from __future__ import annotations

import time
from typing import Any, AsyncIterator, Optional

from .base import BaseAdapter, InvokeRequest, InvokeResponse


class MockAdapter(BaseAdapter):
    vendor = "mock"

    def __init__(
        self,
        model: str = "mock-1",
        canned_response: str = "[mock] hello from the mock adapter",
        capabilities: Optional[list[str]] = None,
        context_window: int = 8000,
    ) -> None:
        self.model = model
        self._canned = canned_response
        self._caps = list(capabilities or ["text.qa", "code.generate"])
        self.context_window = context_window
        self.locality = "local"
        self.cost_class = "free"

    async def invoke(self, request: InvokeRequest) -> InvokeResponse:
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user"),
            "",
        )
        content = f"{self._canned} :: echo={last_user[:80]}"
        return InvokeResponse(
            content=content,
            model=self.model,
            finish_reason="stop",
            usage={"prompt_tokens": len(last_user), "completion_tokens": len(content)},
            latency_ms=0.5,
            metadata={"vendor": self.vendor, "mocked": True},
        )

    async def invoke_stream(self, request: InvokeRequest) -> AsyncIterator[str]:
        resp = await self.invoke(request)
        for chunk in resp.content.split():
            yield chunk + " "

    async def health_check(self) -> bool:
        return True

    def capabilities(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "model": self.model,
            "capabilities": list(self._caps),
            "context_window": self.context_window,
            "locality": self.locality,
            "cost_class": self.cost_class,
        }
