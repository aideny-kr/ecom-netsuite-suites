"""Tests for the LLM adapter layer — factory, format translation, response normalization."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.llm_adapter import (
    DEFAULT_MODELS,
    VALID_MODELS,
    VALID_PROVIDERS,
    LLMResponse,
    TokenUsage,
    ToolUseBlock,
    get_adapter,
)

# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


class TestGetAdapter:
    def test_returns_anthropic_adapter(self):
        adapter = get_adapter("anthropic", "sk-test")
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        assert isinstance(adapter, AnthropicAdapter)

    def test_returns_openai_adapter(self):
        adapter = get_adapter("openai", "sk-test")
        from app.services.chat.adapters.openai_adapter import OpenAIAdapter

        assert isinstance(adapter, OpenAIAdapter)

    def test_returns_gemini_adapter(self):
        adapter = get_adapter("gemini", "test-key")
        from app.services.chat.adapters.gemini_adapter import GeminiAdapter

        assert isinstance(adapter, GeminiAdapter)

    def test_invalid_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            get_adapter("mistral", "key")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_valid_providers(self):
        assert VALID_PROVIDERS == {"anthropic", "openai", "gemini", "openrouter"}

    def test_all_providers_have_default_model(self):
        for provider in VALID_PROVIDERS:
            assert provider in DEFAULT_MODELS

    def test_all_providers_have_model_list(self):
        for provider in VALID_PROVIDERS:
            assert provider in VALID_MODELS
            assert len(VALID_MODELS[provider]) > 0


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------


class TestAnthropicAdapter:
    @pytest.mark.asyncio
    async def test_text_response(self):
        adapter = get_adapter("anthropic", "sk-test")

        mock_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Hello!"
        mock_response.content = [text_block]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)

        with patch.object(adapter._client.messages, "create", new_callable=AsyncMock, return_value=mock_response):
            result = await adapter.create_message(
                model="claude-sonnet-4-20250514",
                max_tokens=100,
                system="test",
                messages=[{"role": "user", "content": "Hi"}],
            )

        assert result.text_blocks == ["Hello!"]
        assert result.tool_use_blocks == []
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5

    @pytest.mark.asyncio
    async def test_tool_use_response(self):
        adapter = get_adapter("anthropic", "sk-test")

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "tool_1"
        tool_block.name = "search"
        tool_block.input = {"query": "test"}

        mock_response = MagicMock()
        mock_response.content = [tool_block]
        mock_response.usage = MagicMock(input_tokens=20, output_tokens=10)

        with patch.object(adapter._client.messages, "create", new_callable=AsyncMock, return_value=mock_response):
            result = await adapter.create_message(
                model="claude-sonnet-4-20250514",
                max_tokens=100,
                system="test",
                messages=[{"role": "user", "content": "Search"}],
            )

        assert len(result.tool_use_blocks) == 1
        assert result.tool_use_blocks[0].name == "search"
        assert result.tool_use_blocks[0].input == {"query": "test"}

    def test_build_assistant_message(self):
        adapter = get_adapter("anthropic", "sk-test")
        response = LLMResponse(
            text_blocks=["hello"],
            tool_use_blocks=[ToolUseBlock(id="t1", name="search", input={"q": "x"})],
        )
        msg = adapter.build_assistant_message(response)
        assert msg["role"] == "assistant"
        assert len(msg["content"]) == 2
        assert msg["content"][0] == {"type": "text", "text": "hello"}
        assert msg["content"][1]["type"] == "tool_use"

    def test_build_tool_result_message(self):
        adapter = get_adapter("anthropic", "sk-test")
        results = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
        msg = adapter.build_tool_result_message(results)
        assert msg["role"] == "user"
        assert msg["content"] == results


# ---------------------------------------------------------------------------
# OpenAI adapter — message conversion
# ---------------------------------------------------------------------------


class TestOpenAIAdapter:
    def test_convert_tools(self):
        adapter = get_adapter("openai", "sk-test")
        tools = [
            {
                "name": "search",
                "description": "Search data",
                "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
            }
        ]
        converted = adapter._convert_tools(tools)
        assert len(converted) == 1
        assert converted[0]["type"] == "function"
        assert converted[0]["function"]["name"] == "search"
        assert converted[0]["function"]["parameters"]["properties"]["q"]["type"] == "string"

    def test_convert_simple_messages(self):
        adapter = get_adapter("openai", "sk-test")
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        converted = adapter._convert_messages(messages, "You are helpful")
        assert converted[0]["role"] == "system"
        assert converted[0]["content"] == "You are helpful"
        assert converted[1]["role"] == "user"
        assert converted[2]["role"] == "assistant"

    def test_convert_tool_results(self):
        adapter = get_adapter("openai", "sk-test")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "result data"},
                ],
            },
        ]
        converted = adapter._convert_messages(messages, "sys")
        # system + tool result
        assert len(converted) == 2
        assert converted[1]["role"] == "tool"
        assert converted[1]["tool_call_id"] == "call_1"

    @pytest.mark.asyncio
    async def test_text_response(self):
        adapter = get_adapter("openai", "sk-test")

        mock_choice = MagicMock()
        mock_choice.message.content = "Hi there"
        mock_choice.message.tool_calls = None

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=8)

        with patch.object(
            adapter._client.chat.completions,
            "create",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await adapter.create_message(
                model="gpt-4o",
                max_tokens=100,
                system="test",
                messages=[{"role": "user", "content": "Hi"}],
            )

        assert result.text_blocks == ["Hi there"]
        assert result.usage.input_tokens == 15
        assert result.usage.output_tokens == 8

    @pytest.mark.asyncio
    async def test_tool_call_response(self):
        adapter = get_adapter("openai", "sk-test")

        tool_call = MagicMock()
        tool_call.id = "call_abc"
        tool_call.function.name = "search"
        tool_call.function.arguments = '{"q": "test"}'

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = [tool_call]

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=10)

        with patch.object(
            adapter._client.chat.completions,
            "create",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await adapter.create_message(
                model="gpt-4o",
                max_tokens=100,
                system="test",
                messages=[{"role": "user", "content": "Search"}],
            )

        assert len(result.tool_use_blocks) == 1
        assert result.tool_use_blocks[0].id == "call_abc"
        assert result.tool_use_blocks[0].name == "search"
        assert result.tool_use_blocks[0].input == {"q": "test"}


# ---------------------------------------------------------------------------
# Gemini adapter
# ---------------------------------------------------------------------------


class TestGeminiAdapter:
    def test_convert_tools(self):
        adapter = get_adapter("gemini", "test-key")
        tools = [
            {
                "name": "search",
                "description": "Search data",
                "input_schema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            }
        ]
        converted = adapter._convert_tools(tools)
        assert len(converted) == 1
        # Should be a genai Tool with function_declarations
        assert hasattr(converted[0], "function_declarations")

    def test_build_assistant_message(self):
        adapter = get_adapter("gemini", "test-key")
        response = LLMResponse(
            text_blocks=["result"],
            tool_use_blocks=[ToolUseBlock(id="t1", name="fn", input={"k": "v"})],
        )
        msg = adapter.build_assistant_message(response)
        assert msg["role"] == "assistant"
        assert len(msg["content"]) == 2


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_token_usage_defaults(self):
        usage = TokenUsage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0

    def test_llm_response_defaults(self):
        resp = LLMResponse()
        assert resp.text_blocks == []
        assert resp.tool_use_blocks == []
        assert resp.usage.input_tokens == 0

    def test_tool_use_block(self):
        block = ToolUseBlock(id="1", name="test", input={"a": 1})
        assert block.id == "1"
        assert block.name == "test"
        assert block.input == {"a": 1}


# ---------------------------------------------------------------------------
# Anthropic adapter: strip non-API fields from tool dicts (regression)
# ---------------------------------------------------------------------------


class TestAnthropicToolFieldStripping:
    """Tools dicts may carry internal-only fields (e.g. `category` from
    `tool_categories.categorize()`). Anthropic's API rejects unknown fields with
    400 `tools.0.custom.category: Extra inputs are not permitted`, so the
    adapter MUST strip any non-API keys before sending."""

    _ALLOWED_TOOL_KEYS = {"name", "description", "input_schema", "cache_control", "type"}

    def _tools_with_category(self) -> list[dict]:
        return [
            {
                "name": "search",
                "description": "Search data",
                "input_schema": {"type": "object", "properties": {}},
                "category": "data",  # internal-only — must be stripped
            },
            {
                "name": "create_record",
                "description": "Write tool",
                "input_schema": {"type": "object", "properties": {}},
                "category": "mutation",  # internal-only — must be stripped
            },
        ]

    @pytest.mark.asyncio
    async def test_create_message_strips_non_api_fields(self):
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        captured: dict = {}

        async def _create(**kwargs):
            captured.update(kwargs)
            mock_response = MagicMock()
            mock_response.content = []
            mock_response.usage = MagicMock(
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )
            return mock_response

        adapter = AnthropicAdapter(api_key="sk-test")
        adapter._client = MagicMock()
        adapter._client.messages.create = _create

        await adapter.create_message(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=self._tools_with_category(),
        )

        sent_tools = captured["tools"]
        for t in sent_tools:
            extra = set(t.keys()) - self._ALLOWED_TOOL_KEYS
            assert not extra, f"tool {t.get('name')!r} has unknown keys: {extra}"

    @pytest.mark.asyncio
    async def test_stream_message_strips_non_api_fields(self):
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        captured: dict = {}

        class _FakeStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            @property
            def text_stream(self):
                async def _gen():
                    yield "ok"

                return _gen()

            async def get_final_message(self):
                final = MagicMock()
                final.content = []
                final.usage = MagicMock(
                    input_tokens=1,
                    output_tokens=1,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                )
                return final

        def _stream(**kwargs):
            captured.update(kwargs)
            return _FakeStream()

        adapter = AnthropicAdapter(api_key="sk-test")
        adapter._client = MagicMock()
        adapter._client.messages.stream = _stream

        async for _ in adapter.stream_message(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=self._tools_with_category(),
        ):
            pass

        sent_tools = captured["tools"]
        for t in sent_tools:
            extra = set(t.keys()) - self._ALLOWED_TOOL_KEYS
            assert not extra, f"tool {t.get('name')!r} has unknown keys: {extra}"


# ---------------------------------------------------------------------------
# Anthropic stream retry — per-error-type backoff
# ---------------------------------------------------------------------------


def _make_api_error(kind: str, status: int = 529, retry_after: str | None = None):
    """Build an anthropic.APIStatusError with the given error type / status / header."""
    import anthropic

    exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
    exc.status_code = status
    exc.body = {"error": {"type": kind, "message": kind}}
    exc.message = kind
    if retry_after is not None:
        resp = MagicMock()
        resp.headers = {"retry-after": retry_after}
        exc.response = resp
    else:
        exc.response = None
    return exc


class _FakeStreamRaisingOnce:
    """Context manager that raises `error` on first __aenter__, then yields a happy stream."""

    _factory_state: dict

    def __init__(self, error, state):
        self._error = error
        self._state = state

    async def __aenter__(self):
        if self._state["attempts"] == 1 and self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *args):
        return False

    @property
    def text_stream(self):
        async def _gen():
            yield "hello"

        return _gen()

    async def get_final_message(self):
        final = MagicMock()
        final.content = []
        final.usage = MagicMock(
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        return final


def _install_stream(adapter, error):
    """Wire adapter.messages.stream so the first call raises `error`, the second succeeds."""
    state = {"attempts": 0}

    def _stream(**kwargs):
        state["attempts"] += 1
        return _FakeStreamRaisingOnce(error, state)

    adapter._client = MagicMock()
    adapter._client.messages.stream = _stream
    return state


class TestAnthropicBackoffStrategy:
    """Per-error-type backoff: overloaded_error uses long delays, rate_limit honors
    Retry-After header, api_error keeps the short schedule. All delays jittered ±25%."""

    @pytest.mark.asyncio
    async def test_overload_uses_longer_backoff(self, monkeypatch):
        from app.services.chat.adapters import anthropic_adapter as mod
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        # Deterministic jitter (upper end) so we can assert the exact delay.
        monkeypatch.setattr(mod.random, "uniform", lambda _lo, _hi: 1.0)
        sleep_spy = AsyncMock()
        monkeypatch.setattr(mod.asyncio, "sleep", sleep_spy)

        adapter = AnthropicAdapter(api_key="sk-test")
        state = _install_stream(adapter, _make_api_error("overloaded_error"))

        events = []
        async for ev, payload in adapter.stream_message(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
        ):
            events.append((ev, payload))

        assert state["attempts"] == 2
        assert sleep_spy.await_count == 1
        # First overload delay should be drawn from _OVERLOAD_BACKOFF_SECONDS, not the
        # old 1s generic delay.
        assert sleep_spy.await_args.args[0] == mod._OVERLOAD_BACKOFF_SECONDS[0]

    @pytest.mark.asyncio
    async def test_rate_limit_respects_retry_after_header(self, monkeypatch):
        from app.services.chat.adapters import anthropic_adapter as mod
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        monkeypatch.setattr(mod.random, "uniform", lambda _lo, _hi: 1.0)
        sleep_spy = AsyncMock()
        monkeypatch.setattr(mod.asyncio, "sleep", sleep_spy)

        adapter = AnthropicAdapter(api_key="sk-test")
        err = _make_api_error("rate_limit_error", status=429, retry_after="7")
        _install_stream(adapter, err)

        async for _ in adapter.stream_message(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
        ):
            pass

        assert sleep_spy.await_count == 1
        assert sleep_spy.await_args.args[0] == pytest.approx(7.0)

    @pytest.mark.asyncio
    async def test_rate_limit_falls_back_when_header_absent(self, monkeypatch):
        from app.services.chat.adapters import anthropic_adapter as mod
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        monkeypatch.setattr(mod.random, "uniform", lambda _lo, _hi: 1.0)
        sleep_spy = AsyncMock()
        monkeypatch.setattr(mod.asyncio, "sleep", sleep_spy)

        adapter = AnthropicAdapter(api_key="sk-test")
        err = _make_api_error("rate_limit_error", status=429, retry_after=None)
        _install_stream(adapter, err)

        async for _ in adapter.stream_message(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
        ):
            pass

        assert sleep_spy.await_count == 1
        assert sleep_spy.await_args.args[0] == mod._RATE_LIMIT_BACKOFF_SECONDS[0]

    @pytest.mark.asyncio
    async def test_retry_after_capped_at_120s(self, monkeypatch):
        """A Retry-After larger than the cap must be clamped, not honored literally.

        The cap exists because the outer per-turn budget in chat.py is 300s; honoring
        a Retry-After of 3600 (or even 200) would leave no room for the stream attempt
        + tool execution on the other hops. We deliberately trade server guidance
        fidelity for turn liveness — the retry will likely re-hit the limiter, which
        is preferable to the user staring at a blank screen for an hour.
        """
        from app.services.chat.adapters import anthropic_adapter as mod
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        monkeypatch.setattr(mod.random, "uniform", lambda _lo, _hi: 1.0)
        # Generous budget so the deadline guard doesn't abort first.
        monkeypatch.setattr(mod, "_STREAM_TIMEOUT_SECONDS", 600)
        sleep_spy = AsyncMock()
        monkeypatch.setattr(mod.asyncio, "sleep", sleep_spy)

        adapter = AnthropicAdapter(api_key="sk-test")
        err = _make_api_error("rate_limit_error", status=429, retry_after="3600")
        _install_stream(adapter, err)

        async for _ in adapter.stream_message(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
        ):
            pass

        assert sleep_spy.await_count == 1
        assert sleep_spy.await_args.args[0] == mod._MAX_RETRY_AFTER_SECONDS

    @pytest.mark.asyncio
    async def test_retry_abandoned_when_budget_exhausted(self, monkeypatch):
        """If the next retry delay would exceed the remaining per-turn budget, raise
        instead of sleeping past the deadline."""
        import anthropic

        from app.services.chat.adapters import anthropic_adapter as mod
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        # 6s budget, guard keeps 5s slack → remaining-5 = ~1s < 10s first overload delay.
        monkeypatch.setattr(mod, "_STREAM_TIMEOUT_SECONDS", 6)
        monkeypatch.setattr(mod.random, "uniform", lambda _lo, _hi: 1.0)
        sleep_spy = AsyncMock()
        monkeypatch.setattr(mod.asyncio, "sleep", sleep_spy)

        adapter = AnthropicAdapter(api_key="sk-test")
        _install_stream(adapter, _make_api_error("overloaded_error"))

        with pytest.raises(anthropic.APIStatusError):
            async for _ in adapter.stream_message(
                model="claude-sonnet-4-6",
                max_tokens=100,
                system="sys",
                messages=[{"role": "user", "content": "hi"}],
            ):
                pass

        assert sleep_spy.await_count == 0

    @pytest.mark.asyncio
    async def test_jitter_is_applied(self, monkeypatch):
        """Real (non-mocked) jitter should land in the ±25% range around the nominal
        overload delay. Uses a seeded PRNG so the assertion is deterministic, but
        the actual `random.uniform` is exercised — this catches regressions that a
        tautological `assert uniform was called with (0.75, 1.25)` would miss.
        """
        import random

        from app.services.chat.adapters import anthropic_adapter as mod
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        random.seed(42)
        sleep_spy = AsyncMock()
        monkeypatch.setattr(mod.asyncio, "sleep", sleep_spy)

        adapter = AnthropicAdapter(api_key="sk-test")
        _install_stream(adapter, _make_api_error("overloaded_error"))

        async for _ in adapter.stream_message(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
        ):
            pass

        assert sleep_spy.await_count == 1
        nominal = mod._OVERLOAD_BACKOFF_SECONDS[0]
        delay = sleep_spy.await_args.args[0]
        assert mod._JITTER_MIN * nominal <= delay <= mod._JITTER_MAX * nominal
        # Also: jitter must not be the pathological no-op (exact nominal)
        assert delay != nominal, "jitter produced an exact nominal delay — PRNG unused?"

    @pytest.mark.asyncio
    async def test_api_error_uses_generic_backoff(self, monkeypatch):
        """api_error keeps the short (1, 2, 4)s schedule — longer delays are reserved
        for overload."""
        from app.services.chat.adapters import anthropic_adapter as mod
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        monkeypatch.setattr(mod.random, "uniform", lambda _lo, _hi: 1.0)
        sleep_spy = AsyncMock()
        monkeypatch.setattr(mod.asyncio, "sleep", sleep_spy)

        adapter = AnthropicAdapter(api_key="sk-test")
        _install_stream(adapter, _make_api_error("api_error", status=500))

        async for _ in adapter.stream_message(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
        ):
            pass

        assert sleep_spy.await_count == 1
        assert sleep_spy.await_args.args[0] == mod._GENERIC_BACKOFF_SECONDS[0]
        assert sleep_spy.await_args.args[0] <= 4.0

    @pytest.mark.asyncio
    async def test_does_not_retry_after_partial_output(self, monkeypatch):
        """Regression: once any text has streamed, an error from the ongoing stream
        must propagate to the caller — partial output can't be rewound."""
        import anthropic

        from app.services.chat.adapters import anthropic_adapter as mod
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock())

        state = {"attempts": 0}
        err = _make_api_error("overloaded_error")

        class _FakeStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            @property
            def text_stream(self):
                async def _gen():
                    yield "partial "
                    raise err

                return _gen()

            async def get_final_message(self):  # pragma: no cover - not reached
                return MagicMock()

        def _stream(**kwargs):
            state["attempts"] += 1
            return _FakeStream()

        adapter = AnthropicAdapter(api_key="sk-test")
        adapter._client = MagicMock()
        adapter._client.messages.stream = _stream

        with pytest.raises(anthropic.APIStatusError):
            async for _ in adapter.stream_message(
                model="claude-sonnet-4-6",
                max_tokens=100,
                system="sys",
                messages=[{"role": "user", "content": "hi"}],
            ):
                pass

        assert state["attempts"] == 1


# ---------------------------------------------------------------------------
# Plan Mode end-to-end plumbing — `tool_choice` must reach the SDK call site
# ---------------------------------------------------------------------------


class TestForceToolChoiceReachesAPI:
    """`force_tool_choice` returns the INTERNAL `{"type":"tool","name":...}` shape
    for all providers; each adapter's `_convert_tool_choice` (or `create_message`
    branch) translates that to the provider-native shape at the SDK call site.

    These tests mock the SDK call site and assert that the translated tool_choice
    actually reaches `kwargs`. Pure-shape unit tests at the `force_tool_choice`
    boundary alone are insufficient — they don't catch the case where the
    converter drops the dict on the floor (the original P2 bug).
    """

    @pytest.mark.asyncio
    async def test_openai_force_tool_choice_reaches_api_kwargs(self):
        """OpenAI: create_message must surface tool_choice as the OpenAI-native
        `{"type":"function","function":{"name":...}}` to chat.completions.create."""
        from app.services.chat.adapters.openai_adapter import OpenAIAdapter

        captured: dict = {}

        async def _create(**kwargs):
            captured.update(kwargs)
            mock_choice = MagicMock()
            mock_choice.message.content = "ok"
            mock_choice.message.tool_calls = None
            mock_response = MagicMock()
            mock_response.choices = [mock_choice]
            mock_response.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
            return mock_response

        adapter = OpenAIAdapter(api_key="sk-test")
        adapter._client = MagicMock()
        adapter._client.chat.completions.create = _create

        forcing = adapter.force_tool_choice("clarify")
        await adapter.create_message(
            model="gpt-4o",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "name": "clarify",
                    "description": "Ask a question",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            tool_choice=forcing,
        )

        assert "tool_choice" in captured, "tool_choice was dropped before reaching the OpenAI SDK"
        assert captured["tool_choice"] == {"type": "function", "function": {"name": "clarify"}}

    @pytest.mark.asyncio
    async def test_openai_stream_message_forwards_tool_choice(self):
        """OpenAI: stream_message must also forward tool_choice to the SDK call."""
        from app.services.chat.adapters.openai_adapter import OpenAIAdapter

        captured: dict = {}

        async def _empty_stream():
            return
            yield  # pragma: no cover - unreachable, makes this an async generator

        async def _create(**kwargs):
            captured.update(kwargs)
            return _empty_stream()

        adapter = OpenAIAdapter(api_key="sk-test")
        adapter._client = MagicMock()
        adapter._client.chat.completions.create = _create

        forcing = adapter.force_tool_choice("clarify")
        async for _ in adapter.stream_message(
            model="gpt-4o",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "name": "clarify",
                    "description": "Ask a question",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            tool_choice=forcing,
        ):
            pass

        assert "tool_choice" in captured, "tool_choice was dropped before reaching the OpenAI SDK in stream_message"
        assert captured["tool_choice"] == {"type": "function", "function": {"name": "clarify"}}

    @pytest.mark.asyncio
    async def test_gemini_force_tool_choice_reaches_api_kwargs(self):
        """Gemini: create_message must surface tool_choice as a ToolConfig with
        function_calling_config.mode='ANY' and allowed_function_names=[tool]."""
        from app.services.chat.adapters.gemini_adapter import GeminiAdapter

        captured: dict = {}

        async def _generate_content(**kwargs):
            captured.update(kwargs)
            mock_response = MagicMock()
            mock_response.candidates = []
            mock_response.usage_metadata = MagicMock(prompt_token_count=1, candidates_token_count=1)
            return mock_response

        adapter = GeminiAdapter(api_key="test-key")
        adapter._client = MagicMock()
        adapter._client.aio.models.generate_content = _generate_content

        forcing = adapter.force_tool_choice("clarify", model="gemini-1.5-pro")
        await adapter.create_message(
            model="gemini-1.5-pro",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "name": "clarify",
                    "description": "Ask a question",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            tool_choice=forcing,
        )

        config = captured.get("config")
        assert config is not None, "Gemini config was not built"
        assert config.tool_config is not None, "tool_choice was dropped before reaching the Gemini SDK"
        fcc = config.tool_config.function_calling_config
        assert fcc.mode == "ANY"
        assert list(fcc.allowed_function_names) == ["clarify"]

    @pytest.mark.asyncio
    async def test_gemini_stream_message_forwards_tool_choice(self):
        """Gemini: stream_message has no override; default falls through to
        create_message and must forward tool_choice (regression for the bug
        where the default in BaseLLMAdapter dropped tool_choice silently)."""
        from app.services.chat.adapters.gemini_adapter import GeminiAdapter

        captured: dict = {}

        async def _generate_content(**kwargs):
            captured.update(kwargs)
            mock_response = MagicMock()
            mock_response.candidates = []
            mock_response.usage_metadata = MagicMock(prompt_token_count=1, candidates_token_count=1)
            return mock_response

        adapter = GeminiAdapter(api_key="test-key")
        adapter._client = MagicMock()
        adapter._client.aio.models.generate_content = _generate_content

        forcing = adapter.force_tool_choice("clarify", model="gemini-1.5-pro")
        async for _ in adapter.stream_message(
            model="gemini-1.5-pro",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "name": "clarify",
                    "description": "Ask a question",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            tool_choice=forcing,
        ):
            pass

        config = captured.get("config")
        assert config is not None, "Gemini config was not built (stream_message didn't reach SDK)"
        assert config.tool_config is not None, (
            "tool_choice was dropped — default BaseLLMAdapter.stream_message must forward it to create_message"
        )
        fcc = config.tool_config.function_calling_config
        assert fcc.mode == "ANY"
        assert list(fcc.allowed_function_names) == ["clarify"]

    @pytest.mark.asyncio
    async def test_anthropic_stream_message_forwards_tool_choice(self):
        """Anthropic: regression guard — its stream_message override must keep
        forwarding tool_choice into the SDK kwargs."""
        from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter

        captured: dict = {}

        class _FakeStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            @property
            def text_stream(self):
                async def _gen():
                    if False:
                        yield ""

                return _gen()

            async def get_final_message(self):
                final = MagicMock()
                final.content = []
                final.usage = MagicMock(
                    input_tokens=1,
                    output_tokens=1,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                )
                return final

        def _stream(**kwargs):
            captured.update(kwargs)
            return _FakeStream()

        adapter = AnthropicAdapter(api_key="sk-test")
        adapter._client = MagicMock()
        adapter._client.messages.stream = _stream

        forcing = adapter.force_tool_choice("clarify")
        async for _ in adapter.stream_message(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "name": "clarify",
                    "description": "Ask a question",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            tool_choice=forcing,
        ):
            pass

        assert "tool_choice" in captured, "tool_choice was dropped before reaching the Anthropic SDK"
        assert captured["tool_choice"] == {"type": "tool", "name": "clarify"}
