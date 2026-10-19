"""Every model token a trial spends, counted where every call passes: the Anthropic SDK.

Summing usage at the call sites we knew about missed calls twice (review round 4: entity
resolution; round 5: the confidence check, on its own client). Every model call in the app
goes through the SDK's ``messages.create`` or ``messages.stream``, so the meter sits there
for the whole trial. A call whose usage cannot be read is counted as unmetered, which makes
the trial not comparable instead of quietly cheaper: a raw ``stream=True`` response, a call
that raised or was cancelled (its tokens may still have been spent), and a stream that ended
before its final usage arrived (review round 6).

Embedding calls (OpenAI ``embeddings.create``, made by retrieval during setup) are metered
into ``embedding_tokens``, kept apart because G5 is defined in Claude model tokens.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class ModelMeter:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    calls: int = 0
    unmetered: int = 0
    embedding_tokens: int = 0

    def add(self, usage) -> None:
        self.calls += 1
        if usage is None:
            self.unmetered += 1
            return
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_tokens += int(getattr(usage, "cache_creation_input_tokens", 0) or 0) + int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )

    def add_embedding(self, usage) -> None:
        self.calls += 1
        tokens = getattr(usage, "total_tokens", None) if usage is not None else None
        if tokens is None:
            self.unmetered += 1
            return
        self.embedding_tokens += int(tokens)


def _final_usage(stream):
    """The stream's usage once its final event arrived; None (unknown) before that."""
    try:
        snapshot = stream.current_message_snapshot
    except Exception:  # noqa: BLE001 - no snapshot means the usage is unknown, never zero
        return None
    # message_delta sets stop_reason and the final output count; without it the snapshot
    # still holds message_start's placeholder output tokens.
    return snapshot.usage if getattr(snapshot, "stop_reason", None) is not None else None


class _AsyncStream:
    def __init__(self, manager, meter):
        self._manager, self._meter, self._stream = manager, meter, None

    async def __aenter__(self):
        try:
            self._stream = await self._manager.__aenter__()
        except BaseException:
            self._meter.add(None)  # the request may have been spent before it failed
            raise
        return self._stream

    async def __aexit__(self, exc_type, exc, tb):
        self._meter.add(None if exc_type is not None else _final_usage(self._stream))
        return await self._manager.__aexit__(exc_type, exc, tb)


class _SyncStream:
    def __init__(self, manager, meter):
        self._manager, self._meter, self._stream = manager, meter, None

    def __enter__(self):
        try:
            self._stream = self._manager.__enter__()
        except BaseException:
            self._meter.add(None)
            raise
        return self._stream

    def __exit__(self, exc_type, exc, tb):
        self._meter.add(None if exc_type is not None else _final_usage(self._stream))
        return self._manager.__exit__(exc_type, exc, tb)


def _usage_of(response, kwargs):
    # A raw stream's usage arrives in events this wrapper does not read: unmetered.
    return None if kwargs.get("stream") else getattr(response, "usage", None)


def _wrap(cls, meter, is_async):
    create, stream = cls.create, cls.stream
    if is_async:

        async def metered_create(self, *args, **kwargs):
            try:
                response = await create(self, *args, **kwargs)
            except BaseException:
                meter.add(None)  # raised or cancelled: tokens may have been spent, count unknown
                raise
            meter.add(_usage_of(response, kwargs))
            return response

        def metered_stream(self, *args, **kwargs):
            return _AsyncStream(stream(self, *args, **kwargs), meter)

    else:

        def metered_create(self, *args, **kwargs):
            try:
                response = create(self, *args, **kwargs)
            except BaseException:
                meter.add(None)
                raise
            meter.add(_usage_of(response, kwargs))
            return response

        def metered_stream(self, *args, **kwargs):
            return _SyncStream(stream(self, *args, **kwargs), meter)

    cls.create, cls.stream = metered_create, metered_stream
    return cls, create, stream


def _wrap_embeddings(cls, meter, is_async):
    create = cls.create
    if is_async:

        async def metered_create(self, *args, **kwargs):
            try:
                response = await create(self, *args, **kwargs)
            except BaseException:
                meter.add_embedding(None)
                raise
            meter.add_embedding(getattr(response, "usage", None))
            return response

    else:

        def metered_create(self, *args, **kwargs):
            try:
                response = create(self, *args, **kwargs)
            except BaseException:
                meter.add_embedding(None)
                raise
            meter.add_embedding(getattr(response, "usage", None))
            return response

    cls.create = metered_create
    return cls, create, None


@contextmanager
def metered(meter: ModelMeter):
    """Count every Anthropic message call and OpenAI embedding call made while the block runs.

    The SDK classes are patched process-wide for the block, which is safe because trials run
    one at a time; the originals are restored on exit, even after an exception."""
    from anthropic.resources import messages as plain
    from anthropic.resources.beta import messages as beta
    from openai.resources import embeddings

    saved = [
        _wrap(cls, meter, is_async)
        for cls, is_async in (
            (plain.AsyncMessages, True),
            (beta.AsyncMessages, True),
            (plain.Messages, False),
            (beta.Messages, False),
        )
    ] + [
        _wrap_embeddings(cls, meter, is_async)
        for cls, is_async in ((embeddings.AsyncEmbeddings, True), (embeddings.Embeddings, False))
    ]
    try:
        yield meter
    finally:
        for cls, create, stream in saved:
            cls.create = create
            if stream is not None:
                cls.stream = stream
