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
async def test_legacy_model_uses_budget_tokens_temperature_maxtokens():
    """Sonnet 4.5 (and other pre-4.6 models) use LEGACY extended thinking:
    thinking={type:enabled,budget_tokens} + temperature=1 + bumped max_tokens."""
    adapter = AnthropicAdapter(api_key="sk-test")
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_message([_block("text", text="hi")])

    adapter._client = MagicMock()
    adapter._client.messages.create = AsyncMock(side_effect=fake_create)

    await adapter.create_message(
        model="claude-sonnet-4-5-20250929",
        max_tokens=16384,
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="med",
    )

    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 6144}
    assert captured["temperature"] == 1
    assert captured["max_tokens"] > 6144  # must exceed budget
    assert "output_config" not in captured  # legacy path uses no effort


@pytest.mark.asyncio
async def test_adaptive_thinking_for_sonnet5():
    """Sonnet 5 (and 4.6 / Opus 4.6+) use ADAPTIVE thinking + output_config.effort —
    NOT budget_tokens / temperature (those would 400 on these models)."""
    adapter = AnthropicAdapter(api_key="sk-test")
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_message([_block("text", text="hi")])

    adapter._client = MagicMock()
    adapter._client.messages.create = AsyncMock(side_effect=fake_create)

    await adapter.create_message(
        model="claude-sonnet-5",
        max_tokens=16384,
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="med",
    )

    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"] == {"effort": "medium"}
    assert "temperature" not in captured
    assert "budget_tokens" not in str(captured.get("thinking"))


@pytest.mark.asyncio
async def test_adaptive_thinking_maps_xhigh_effort():
    adapter = AnthropicAdapter(api_key="sk-test")
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_message([_block("text", text="hi")])

    adapter._client = MagicMock()
    adapter._client.messages.create = AsyncMock(side_effect=fake_create)

    await adapter.create_message(
        model="claude-sonnet-5",
        max_tokens=16384,
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="xhigh",
    )

    assert captured["output_config"]["effort"] == "xhigh"


@pytest.mark.asyncio
async def test_no_thinking_for_haiku():
    """Haiku does not support thinking/effort — the adapter must send neither."""
    adapter = AnthropicAdapter(api_key="sk-test")
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_message([_block("text", text="hi")])

    adapter._client = MagicMock()
    adapter._client.messages.create = AsyncMock(side_effect=fake_create)

    await adapter.create_message(
        model="claude-haiku-4-5-20251001",
        max_tokens=16384,
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="med",
    )

    assert "thinking" not in captured
    assert "output_config" not in captured
    assert "temperature" not in captured


@pytest.mark.asyncio
async def test_none_level_disables_thinking_on_adaptive_model():
    """Adaptive-default models (Sonnet 5 / 4.6) THINK unless explicitly disabled —
    so a none-level turn must send thinking={type:disabled}, not omit it."""
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

    assert captured["thinking"] == {"type": "disabled"}
    assert "temperature" not in captured
    assert "output_config" not in captured


@pytest.mark.asyncio
async def test_none_level_omits_thinking_on_legacy_model():
    """Legacy models default to no thinking when omitted — so none-level sends nothing."""
    adapter = AnthropicAdapter(api_key="sk-test")
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_message([_block("text", text="hi")])

    adapter._client = MagicMock()
    adapter._client.messages.create = AsyncMock(side_effect=fake_create)

    await adapter.create_message(
        model="claude-sonnet-4-5-20250929",
        max_tokens=16384,
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="none",
    )

    assert "thinking" not in captured


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


@pytest.mark.asyncio
async def test_thinking_suppressed_when_tool_choice_is_forced():
    """Extended thinking is incompatible with a forced tool_choice (type tool/any)
    — Anthropic returns 400. On adaptive-default models (Sonnet 5 / 4.6) the adapter
    must explicitly DISABLE thinking on those turns (omitting leaves it ON → 400),
    even when a thinking_level is requested (plan-mode clarify, step-0 guard)."""
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
        tool_choice={"type": "tool", "name": "clarify"},
        thinking_level="med",
    )

    assert captured["thinking"] == {"type": "disabled"}  # explicitly off — forced tool
    assert "output_config" not in captured
    assert captured["tool_choice"] == {"type": "tool", "name": "clarify"}


@pytest.mark.asyncio
async def test_thinking_applied_when_tool_choice_is_auto():
    """Auto/none tool_choice is compatible with thinking — must still enable it
    (adaptive on Sonnet 4.6)."""
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
        tool_choice={"type": "auto"},
        thinking_level="med",
    )

    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"] == {"effort": "medium"}
