"""Tests for stream_message deadline timeout in AnthropicAdapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.chat.adapters.anthropic_adapter import AnthropicAdapter


class _AsyncIterator:
    """Helper to make a list into a proper async iterator."""

    def __init__(self, items):
        self._items = list(items)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


def _build_mock_stream(text_chunks: list[str], final_message=None):
    """Build a mock Anthropic streaming context manager."""
    stream_cm = AsyncMock()
    stream_obj = AsyncMock()
    stream_obj.text_stream = _AsyncIterator(text_chunks)

    if final_message is None:
        # Build a minimal final message
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "".join(text_chunks)

        usage = MagicMock()
        usage.input_tokens = 10
        usage.output_tokens = 5
        usage.cache_creation_input_tokens = 0
        usage.cache_read_input_tokens = 0

        msg = MagicMock()
        msg.content = [text_block]
        msg.usage = usage
        final_message = msg

    stream_obj.get_final_message = AsyncMock(return_value=final_message)

    # Make the context manager work: async with client.messages.stream(...) as stream
    stream_cm.__aenter__ = AsyncMock(return_value=stream_obj)
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    return stream_cm


@pytest.mark.asyncio
async def test_stream_deadline_exceeded_during_text():
    """When time exceeds the deadline during text streaming, no 'response' event is yielded."""
    adapter = AnthropicAdapter(api_key="test-key")

    mock_stream = _build_mock_stream(["hello", " world", " more"])

    # Patch the client's messages.stream to return our mock
    adapter._client = MagicMock()
    adapter._client.messages = MagicMock()
    adapter._client.messages.stream = MagicMock(return_value=mock_stream)

    # Patch time.monotonic: first call sets deadline, second call is past deadline
    call_count = 0
    base_time = 1000.0

    def fake_monotonic():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # This is the deadline setup: deadline = base_time + _STREAM_TIMEOUT_SECONDS
            return base_time
        # All subsequent calls: past deadline
        return base_time + 200  # Well past 120s

    with patch("app.services.chat.adapters.anthropic_adapter.time") as mock_time:
        mock_time.monotonic = fake_monotonic

        events = []
        async for event_type, payload in adapter.stream_message(
            model="test-model",
            max_tokens=100,
            system="test system",
            messages=[{"role": "user", "content": "hi"}],
        ):
            events.append((event_type, payload))

    # First text chunk should be yielded (check happens after yield)
    # but crucially, no "response" event should exist
    assert not any(et == "response" for et, _ in events), "Expected no 'response' event when deadline exceeded"


@pytest.mark.asyncio
async def test_stream_deadline_exceeded_before_final_message():
    """When deadline is exceeded right before get_final_message, no 'response' event."""
    adapter = AnthropicAdapter(api_key="test-key")

    mock_stream = _build_mock_stream(["hello"])

    adapter._client = MagicMock()
    adapter._client.messages = MagicMock()
    adapter._client.messages.stream = MagicMock(return_value=mock_stream)

    # Time: first call sets deadline, text iteration calls are within deadline,
    # but the check before get_final_message is past deadline
    call_count = 0
    base_time = 1000.0

    def fake_monotonic():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return base_time  # deadline setup
        if call_count == 2:
            return base_time + 10  # during text iteration — within deadline
        # Third call (before get_final_message): past deadline
        return base_time + 200

    with patch("app.services.chat.adapters.anthropic_adapter.time") as mock_time:
        mock_time.monotonic = fake_monotonic

        events = []
        async for event_type, payload in adapter.stream_message(
            model="test-model",
            max_tokens=100,
            system="test system",
            messages=[{"role": "user", "content": "hi"}],
        ):
            events.append((event_type, payload))

    # Should have the text event but no response event
    text_events = [p for et, p in events if et == "text"]
    assert len(text_events) >= 1, "Should have at least one text event"
    assert not any(et == "response" for et, _ in events), (
        "Expected no 'response' event when deadline exceeded before final_message"
    )


@pytest.mark.asyncio
async def test_stream_completes_within_deadline():
    """Normal stream within deadline should yield both text and response events."""
    adapter = AnthropicAdapter(api_key="test-key")

    mock_stream = _build_mock_stream(["hello", " world"])

    adapter._client = MagicMock()
    adapter._client.messages = MagicMock()
    adapter._client.messages.stream = MagicMock(return_value=mock_stream)

    # All time checks well within deadline
    with patch("app.services.chat.adapters.anthropic_adapter.time") as mock_time:
        mock_time.monotonic = MagicMock(return_value=1000.0)

        events = []
        async for event_type, payload in adapter.stream_message(
            model="test-model",
            max_tokens=100,
            system="test system",
            messages=[{"role": "user", "content": "hi"}],
        ):
            events.append((event_type, payload))

    text_events = [p for et, p in events if et == "text"]
    response_events = [p for et, p in events if et == "response"]
    assert len(text_events) == 2
    assert len(response_events) == 1, "Should have exactly one 'response' event"


@pytest.mark.asyncio
async def test_stream_timeout_constant_exists():
    """The module-level timeout constant should be defined and reasonable."""
    from app.services.chat.adapters.anthropic_adapter import _STREAM_TIMEOUT_SECONDS

    assert isinstance(_STREAM_TIMEOUT_SECONDS, (int, float))
    assert _STREAM_TIMEOUT_SECONDS > 0
    # Must fit worst-case overload backoff (10 + 30 + 60) plus slack for the
    # actual stream attempt, while staying under the 300s outer chat-turn budget.
    assert _STREAM_TIMEOUT_SECONDS == 180  # 3 minutes
