# backend/tests/test_anthropic_thinking.py
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter
from app.services.chat.llm_adapter import LLMResponse


def _block(btype, **kw):
    b = types.SimpleNamespace(type=btype)
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _fake_message(content, in_tok=10, out_tok=20):
    usage = types.SimpleNamespace(
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return types.SimpleNamespace(content=content, usage=usage)


@pytest.mark.asyncio
async def test_thinking_level_med_sets_budget_temperature_and_maxtokens():
    adapter = AnthropicAdapter(api_key="sk-test")
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_message([_block("text", text="hi")])

    adapter._client = MagicMock()
    adapter._client.messages.create = AsyncMock(side_effect=fake_create)

    await adapter.create_message(
        model="claude-sonnet-4-6",
        max_tokens=16384,
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="med",
    )

    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 6144}
    assert captured["temperature"] == 1
    assert captured["max_tokens"] > 6144  # must exceed budget


@pytest.mark.asyncio
async def test_thinking_level_none_omits_thinking():
    adapter = AnthropicAdapter(api_key="sk-test")
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_message([_block("text", text="hi")])

    adapter._client = MagicMock()
    adapter._client.messages.create = AsyncMock(side_effect=fake_create)

    await adapter.create_message(
        model="claude-sonnet-4-6",
        max_tokens=16384,
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="none",
    )

    assert "thinking" not in captured
    assert "temperature" not in captured


@pytest.mark.asyncio
async def test_thinking_blocks_captured_and_round_tripped():
    adapter = AnthropicAdapter(api_key="sk-test")
    content = [
        _block("thinking", thinking="let me reason", signature="sig123"),
        _block("text", text="the answer"),
    ]
    adapter._client = MagicMock()
    adapter._client.messages.create = AsyncMock(return_value=_fake_message(content))

    resp: LLMResponse = await adapter.create_message(
        model="claude-sonnet-4-6",
        max_tokens=16384,
        system="s",
        messages=[{"role": "user", "content": "q"}],
        thinking_level="high",
    )

    # Captured
    assert resp.thinking_blocks == [{"type": "thinking", "thinking": "let me reason", "signature": "sig123"}]
    # Round-tripped FIRST in the assistant message (Anthropic requires this)
    assistant = adapter.build_assistant_message(resp)
    assert assistant["content"][0] == {"type": "thinking", "thinking": "let me reason", "signature": "sig123"}
    assert assistant["content"][1] == {"type": "text", "text": "the answer"}
