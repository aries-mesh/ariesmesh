"""Unit tests for adapters: vendor inference, capabilities, mock invocation."""
from __future__ import annotations

import asyncio

import pytest

from aries.adapters.base import InvokeRequest, InvokeResponse, Message
from aries.adapters.litellm_adapter import (
    LiteLLMAdapter,
    _infer_vendor,
    anthropic_adapter,
    google_adapter,
    ollama_adapter,
    openai_adapter,
)
from aries.adapters.mock_adapter import MockAdapter


def test_vendor_inference() -> None:
    assert _infer_vendor("ollama/qwen3:32b") == "ollama"
    assert _infer_vendor("claude-sonnet-4-20250514") == "anthropic"
    assert _infer_vendor("anthropic/claude-3-5") == "anthropic"
    assert _infer_vendor("gpt-4o") == "openai"
    assert _infer_vendor("o3-mini") == "openai"
    assert _infer_vendor("gemini/gemini-2.5-flash") == "google"
    assert _infer_vendor("openai/llama3") == "openai-compatible"


def test_capabilities_shape() -> None:
    adapter = ollama_adapter(model="qwen3:32b")
    caps = adapter.capabilities()
    assert caps["vendor"] == "ollama"
    assert "text.qa" in caps["capabilities"]
    assert caps["locality"] == "local"
    assert caps["cost_class"] == "free"


def test_convenience_constructors() -> None:
    assert anthropic_adapter().vendor == "anthropic"
    assert openai_adapter(api_key="dummy").vendor == "openai"
    assert google_adapter().vendor == "google"


def test_request_response_serialization() -> None:
    msg = Message(role="user", content="hi")
    d = msg.to_dict()
    assert Message.from_dict(d).content == "hi"

    resp = InvokeResponse(content="x", model="m", usage={"prompt_tokens": 1, "completion_tokens": 2})
    assert resp.total_tokens == 3


@pytest.mark.asyncio
async def test_mock_adapter_invoke() -> None:
    adapter = MockAdapter()
    req = InvokeRequest(messages=[Message(role="user", content="hello aries")])
    resp = await adapter.invoke(req)
    assert "hello aries" in resp.content
    assert resp.model == "mock-1"
    assert await adapter.health_check() is True
