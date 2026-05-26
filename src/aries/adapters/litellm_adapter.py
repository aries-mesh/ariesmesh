"""Universal LLM adapter via litellm.

Spec reference: §15.

litellm is an optional dependency (it pulls in ~50 transitive packages and
isn't always installable on Android/Termux). It is imported lazily inside
``invoke`` / ``invoke_stream`` so this module loads on minimal installs;
those methods raise ``ImportError`` with a helpful install hint if the
caller tries to actually run a cloud / Ollama agent without it.
"""
from __future__ import annotations

import time
from typing import Any, AsyncIterator, Optional

from .base import BaseAdapter, InvokeRequest, InvokeResponse


try:
    import litellm as _litellm_probe  # noqa: F401  — presence check only
    LITELLM_AVAILABLE = True
    del _litellm_probe
except ImportError:  # pragma: no cover — exercised on minimal installs only
    LITELLM_AVAILABLE = False


_LITELLM_MISSING_MSG = (
    "litellm is required for LiteLLMAdapter. "
    "Install the full extras: `pip install aries-mesh[full]`."
)


def _infer_vendor(model: str) -> str:
    m = model.lower()
    if m.startswith("ollama/"):
        return "ollama"
    if "claude" in m or m.startswith("anthropic/"):
        return "anthropic"
    if any(x in m for x in ("gpt", "o1", "o3", "o4")):
        return "openai"
    if "gemini" in m:
        return "google"
    if m.startswith("openai/"):
        return "openai-compatible"
    return "custom"


class LiteLLMAdapter(BaseAdapter):
    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        vendor: Optional[str] = None,
        context_window: int = 32000,
        cost_class: str = "free",
        locality: str = "local",
        custom_capabilities: Optional[list[str]] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.vendor = vendor or _infer_vendor(model)
        self.context_window = context_window
        self.cost_class = cost_class
        self.locality = locality
        self._custom_capabilities = list(custom_capabilities or ["text.qa"])

    # ----- core ---------------------------------------------------------

    def _build_messages(self, request: InvokeRequest) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        if request.system_prompt:
            msgs.append({"role": "system", "content": request.system_prompt})
        for m in request.messages:
            msgs.append({"role": m.role, "content": m.content})
        return msgs

    def _kwargs(self, request: InvokeRequest) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(request),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.stop_sequences:
            kw["stop"] = request.stop_sequences
        if self.api_key:
            kw["api_key"] = self.api_key
        if self.api_base:
            kw["api_base"] = self.api_base
        return kw

    async def invoke(self, request: InvokeRequest) -> InvokeResponse:
        try:
            import litellm
        except ImportError as e:
            raise ImportError(_LITELLM_MISSING_MSG) from e

        start = time.perf_counter()
        completion = await litellm.acompletion(**self._kwargs(request))
        latency = (time.perf_counter() - start) * 1000.0

        choice = completion.choices[0]
        content = getattr(choice.message, "content", "") or ""
        usage = getattr(completion, "usage", None)
        usage_dict: dict[str, int] = {}
        if usage is not None:
            usage_dict = {
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            }
        return InvokeResponse(
            content=content,
            model=self.model,
            finish_reason=getattr(choice, "finish_reason", "stop") or "stop",
            usage=usage_dict,
            latency_ms=latency,
            metadata={"vendor": self.vendor},
        )

    async def invoke_stream(self, request: InvokeRequest) -> AsyncIterator[str]:
        try:
            import litellm
        except ImportError as e:
            raise ImportError(_LITELLM_MISSING_MSG) from e

        kwargs = self._kwargs(request)
        kwargs["stream"] = True
        async for chunk in await litellm.acompletion(**kwargs):
            delta = getattr(chunk.choices[0].delta, "content", "") if chunk.choices else ""
            if delta:
                yield delta

    async def health_check(self) -> bool:
        try:
            req = InvokeRequest(
                messages=[__import__("aries.adapters.base", fromlist=["Message"]).Message(role="user", content="ping")],
                max_tokens=5,
            )
            resp = await self.invoke(req)
            return bool(resp.content)
        except Exception:
            return False

    def capabilities(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "model": self.model,
            "capabilities": list(self._custom_capabilities),
            "context_window": self.context_window,
            "locality": self.locality,
            "cost_class": self.cost_class,
        }


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

def ollama_adapter(
    model: str = "qwen3:32b",
    api_base: str = "http://localhost:11434",
    capabilities: Optional[list[str]] = None,
    context_window: int = 32_000,
) -> LiteLLMAdapter:
    return LiteLLMAdapter(
        model=f"ollama/{model}" if not model.startswith("ollama/") else model,
        api_base=api_base,
        vendor="ollama",
        context_window=context_window,
        cost_class="free",
        locality="local",
        custom_capabilities=capabilities or ["text.qa", "code.generate"],
    )


def anthropic_adapter(
    model: str = "claude-sonnet-4-20250514",
    api_key: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
    context_window: int = 200_000,
) -> LiteLLMAdapter:
    return LiteLLMAdapter(
        model=model,
        api_key=api_key,
        vendor="anthropic",
        context_window=context_window,
        cost_class="paid",
        locality="cloud-routed",
        custom_capabilities=capabilities or ["text.qa", "code.generate", "summarize"],
    )


def openai_adapter(
    model: str = "gpt-4o",
    api_key: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
    context_window: int = 128_000,
) -> LiteLLMAdapter:
    return LiteLLMAdapter(
        model=model,
        api_key=api_key,
        vendor="openai",
        context_window=context_window,
        cost_class="paid",
        locality="cloud-routed",
        custom_capabilities=capabilities or ["text.qa", "code.generate"],
    )


def google_adapter(
    model: str = "gemini/gemini-2.5-flash",
    api_key: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
    context_window: int = 1_000_000,
) -> LiteLLMAdapter:
    return LiteLLMAdapter(
        model=model,
        api_key=api_key,
        vendor="google",
        context_window=context_window,
        cost_class="metered",
        locality="cloud-routed",
        custom_capabilities=capabilities or ["text.qa", "summarize"],
    )


def custom_adapter(
    model: str,
    api_base: str,
    api_key: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
    context_window: int = 8000,
) -> LiteLLMAdapter:
    if not model.startswith(("openai/", "ollama/", "anthropic/")):
        model = f"openai/{model}"
    return LiteLLMAdapter(
        model=model,
        api_key=api_key,
        api_base=api_base,
        vendor="openai-compatible",
        context_window=context_window,
        cost_class="free",
        locality="local",
        custom_capabilities=capabilities or ["text.qa"],
    )
