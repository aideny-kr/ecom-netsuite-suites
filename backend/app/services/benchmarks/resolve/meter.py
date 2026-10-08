"""Every model token a trial spends, counted where every call passes: the Anthropic SDK.

Summing usage at the call sites we knew about missed calls twice (review round 4: entity
resolution; round 5: the confidence check, on its own client). Every model call in the app
goes through the SDK's ``messages.create`` or ``messages.stream``, so the meter sits there
for the whole trial. A call whose usage cannot be read (a raw ``stream=True`` response, a
stream that ended before its first event) is counted as unmetered, which makes the trial
not comparable instead of quietly cheaper.
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


def _snapshot_usage(stream):
    try:
        return stream.current_message_snapshot.usage
    except Exception:  # noqa: BLE001 - no snapshot means the usage is unknown, never zero
        return None


class _AsyncStream:
    def __init__(self, manager, meter):
        self._manager, self._meter, self._stream = manager, meter, None

    async def __aenter__(self):
        self._stream = await self._manager.__aenter__()
        return self._stream

    async def __aexit__(self, *exc):
        self._meter.add(_snapshot_usage(self._stream) if self._stream is not None else None)
        return await self._manager.__aexit__(*exc)


class _SyncStream:
    def __init__(self, manager, meter):
        self._manager, self._meter, self._stream = manager, meter, None

    def __enter__(self):
        self._stream = self._manager.__enter__()
        return self._stream

    def __exit__(self, *exc):
        self._meter.add(_snapshot_usage(self._stream) if self._stream is not None else None)
        return self._manager.__exit__(*exc)


def _usage_of(response, kwargs):
    # A raw stream's usage arrives in events this wrapper does not read: unmetered.
    return None if kwargs.get("stream") else getattr(response, "usage", None)


def _wrap(cls, meter, is_async):
    create, stream = cls.create, cls.stream
    if is_async:

        async def metered_create(self, *args, **kwargs):
            response = await create(self, *args, **kwargs)
            meter.add(_usage_of(response, kwargs))
            return response

        def metered_stream(self, *args, **kwargs):
            return _AsyncStream(stream(self, *args, **kwargs), meter)

    else:

        def metered_create(self, *args, **kwargs):
            response = create(self, *args, **kwargs)
            meter.add(_usage_of(response, kwargs))
            return response

        def metered_stream(self, *args, **kwargs):
            return _SyncStream(stream(self, *args, **kwargs), meter)

    cls.create, cls.stream = metered_create, metered_stream
    return cls, create, stream


@contextmanager
def metered(meter: ModelMeter):
    """Count every Anthropic message call made while the block runs (trials run one at a time)."""
    from anthropic.resources import messages as plain
    from anthropic.resources.beta import messages as beta

    saved = [
        _wrap(cls, meter, is_async)
        for cls, is_async in (
            (plain.AsyncMessages, True),
            (beta.AsyncMessages, True),
            (plain.Messages, False),
            (beta.Messages, False),
        )
    ]
    try:
        yield meter
    finally:
        for cls, create, stream in saved:
            cls.create, cls.stream = create, stream
