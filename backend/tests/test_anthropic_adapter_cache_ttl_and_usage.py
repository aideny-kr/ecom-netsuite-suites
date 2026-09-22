"""Cache TTL placement and the per-call usage line in the Anthropic adapter."""

import logging
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.chat.adapters import anthropic_adapter as aa

TOOLS = [
    {"name": "a", "description": "", "input_schema": {"type": "object"}, "category": "x"},
    {"name": "b", "input_schema": {}},
]


def _kwargs(**over):
    base = dict(
        model="claude-sonnet-5", max_tokens=100, system="static", system_dynamic="dyn", messages=[], tools=TOOLS
    )
    base.update(over)
    return aa._build_request_kwargs(**base)


def test_default_ttl_is_the_five_minute_default(monkeypatch):
    monkeypatch.setattr(settings, "PROMPT_CACHE_STABLE_TTL", "5m")
    k = _kwargs()
    assert k["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert k["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert k["extra_body"] == {"cache_control": {"type": "ephemeral"}}


def test_one_hour_ttl_goes_only_on_the_stable_prefix(monkeypatch):
    """1h entries must precede 5m ones: tools and the static system block are the stable
    prefix and take the long TTL; the growing conversation keeps the 5m auto-cache."""
    monkeypatch.setattr(settings, "PROMPT_CACHE_STABLE_TTL", "1h")
    k = _kwargs()
    assert k["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert k["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in k["system"][1]  # the dynamic block is never cached
    assert k["extra_body"] == {"cache_control": {"type": "ephemeral"}}
    assert "category" not in k["tools"][0]  # allowlisting still applies


async def test_every_call_logs_its_usage_with_the_four_token_fields(monkeypatch, caplog):
    usage = SimpleNamespace(
        input_tokens=17, output_tokens=104, cache_creation_input_tokens=2357, cache_read_input_tokens=0
    )
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")], usage=usage, stop_reason="end_turn")

    class FakeMessages:
        async def create(self, **kwargs):
            return response

    adapter = aa.AnthropicAdapter.__new__(aa.AnthropicAdapter)
    adapter._client = SimpleNamespace(messages=FakeMessages())
    with caplog.at_level(logging.INFO, logger=aa.__name__):
        await adapter.create_message(
            model="claude-sonnet-5", max_tokens=10, system="s", messages=[{"role": "user", "content": "q"}]
        )
    line = next(r for r in caplog.records if r.getMessage().startswith("llm.usage"))
    msg = line.getMessage()
    for field in ("model=claude-sonnet-5", "in=17", "cache_write=2357", "cache_read=0", "out=104", "stream=false"):
        assert field in msg, msg


# ── attribution: the line must be able to explain WHICH call paid what ─────


async def test_usage_line_carries_purpose_duration_prefix_and_ttl_split(monkeypatch, caplog):
    from app.services.chat.llm_purpose import llm_purpose

    usage = SimpleNamespace(
        input_tokens=17,
        output_tokens=104,
        cache_creation_input_tokens=2357,
        cache_read_input_tokens=0,
        cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=2000, ephemeral_1h_input_tokens=357),
    )
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")], usage=usage, stop_reason="end_turn")

    class FakeMessages:
        async def create(self, **kwargs):
            return response

    adapter = aa.AnthropicAdapter.__new__(aa.AnthropicAdapter)
    adapter._client = SimpleNamespace(messages=FakeMessages())
    with caplog.at_level(logging.INFO, logger=aa.__name__), llm_purpose("request_routing"):
        await adapter.create_message(
            model="claude-sonnet-5",
            max_tokens=10,
            system="stable system",
            messages=[{"role": "user", "content": "q"}],
            tools=TOOLS,
        )
    msg = next(r for r in caplog.records if r.getMessage().startswith("llm.usage")).getMessage()
    for field in ("purpose=request_routing", "cache_write_5m=2000", "cache_write_1h=357", "ms="):
        assert field in msg, msg
    import re

    prefix = re.search(r"prefix=([0-9a-f]{12})", msg)
    assert prefix, msg
    # same tools + same static system → same fingerprint; different system → different
    sent = _kwargs(system="stable system", system_dynamic="", messages=[{"role": "user", "content": "q"}])
    assert aa._prefix_fingerprint(sent) == prefix.group(1)
    assert aa._prefix_fingerprint(_kwargs(system="other system")) != prefix.group(1)
    assert aa._prefix_fingerprint(_kwargs(system="stable system", tools=None)) != prefix.group(1)


def test_purpose_defaults_to_unlabelled_and_nests():
    from app.services.chat.llm_purpose import current_purpose, llm_purpose

    assert current_purpose() == "unlabelled"
    with llm_purpose("agent_step"):
        assert current_purpose() == "agent_step"
        with llm_purpose("completion_review"):
            assert current_purpose() == "completion_review"
        assert current_purpose() == "agent_step"
    assert current_purpose() == "unlabelled"


async def test_purpose_decorator_handles_async_generators():
    from app.services.chat.llm_purpose import current_purpose, with_llm_purpose

    @with_llm_purpose("stream_label")
    async def events():
        yield current_purpose()
        yield current_purpose()

    assert [e async for e in events()] == ["stream_label", "stream_label"]
    assert current_purpose() == "unlabelled"


# ── gate round 1 on #287 ───────────────────────────────────────────────────


async def test_a_generators_purpose_never_leaks_into_the_caller_between_yields():
    from app.services.chat.llm_purpose import current_purpose, with_llm_purpose

    @with_llm_purpose("agent_turn_stream")
    async def events():
        for _ in range(3):
            yield current_purpose()

    seen_inside, seen_between = [], []
    async for purpose in events():
        seen_inside.append(purpose)
        seen_between.append(current_purpose())
    assert seen_inside == ["agent_turn_stream"] * 3
    assert seen_between == ["unlabelled"] * 3


async def test_breaking_out_early_leaves_no_label_behind():
    from app.services.chat.llm_purpose import current_purpose, with_llm_purpose

    @with_llm_purpose("agent_turn_stream")
    async def events():
        yield 1
        yield 2

    gen = events()
    async for _ in gen:
        break
    assert current_purpose() == "unlabelled"
    await gen.aclose()
    assert current_purpose() == "unlabelled"


async def test_closing_an_abandoned_generator_from_another_task_does_not_raise():
    import asyncio

    from app.services.chat.llm_purpose import with_llm_purpose

    @with_llm_purpose("agent_turn_stream")
    async def events():
        yield 1
        yield 2

    gen = events()
    await gen.__anext__()
    await asyncio.create_task(gen.aclose())  # a different Context from the one that started it


async def test_stream_timing_excludes_retry_backoff(monkeypatch, caplog):
    """ms= is the successful attempt's wall time, not the jittered overload sleep."""
    from tests.test_llm_adapters import _install_stream, _make_api_error

    clock = {"t": 1000.0}
    monkeypatch.setattr(aa.time, "monotonic", lambda: clock["t"])

    async def fake_sleep(seconds):
        clock["t"] += 45.0  # a long overload backoff

    monkeypatch.setattr(aa.asyncio, "sleep", fake_sleep)
    adapter = aa.AnthropicAdapter(api_key="sk-test")
    _install_stream(adapter, _make_api_error("overloaded_error"))
    with caplog.at_level(logging.INFO, logger=aa.__name__):
        async for _ in adapter.stream_message(
            model="claude-sonnet-5", max_tokens=10, system="s", messages=[{"role": "user", "content": "q"}]
        ):
            pass
    msg = next(r for r in caplog.records if r.getMessage().startswith("llm.usage")).getMessage()
    assert " ms=0 " in msg and "retries=1" in msg, msg


def test_fingerprint_is_computed_from_the_payload_actually_sent(monkeypatch):
    monkeypatch.setattr(settings, "PROMPT_CACHE_STABLE_TTL", "5m")
    k = _kwargs()
    assert aa._prefix_fingerprint(k) == aa._prefix_fingerprint(_kwargs())
    monkeypatch.setattr(settings, "PROMPT_CACHE_STABLE_TTL", "1h")
    assert aa._prefix_fingerprint(_kwargs()) != aa._prefix_fingerprint(k)  # a different TTL is a different prefix


def test_an_invalid_ttl_fails_at_startup():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError, match="PROMPT_CACHE_STABLE_TTL"):
        Settings(PROMPT_CACHE_STABLE_TTL="60m")
    assert Settings(PROMPT_CACHE_STABLE_TTL="1h").PROMPT_CACHE_STABLE_TTL == "1h"
