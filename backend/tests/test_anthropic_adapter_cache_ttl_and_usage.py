"""Cache TTL placement and the per-call usage line in the Anthropic adapter."""

import logging
from types import SimpleNamespace

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


def test_unknown_ttl_falls_back_to_the_default(monkeypatch):
    monkeypatch.setattr(settings, "PROMPT_CACHE_STABLE_TTL", "2d")
    assert _kwargs()["system"][0]["cache_control"] == {"type": "ephemeral"}


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
