"""A label for WHY a provider call is being made, read by the adapters' usage line.

Per-turn token totals cannot say which call in a turn paid 93K fresh tokens; the
adapter can, if it knows what the call was for. Callers wrap the call site::

    with llm_purpose("request_routing"):
        await adapter.create_message(...)

A context variable, not a parameter: the adapter protocol has several
implementations and many callers, and the label is observability, not behaviour.
Unlabelled calls say so, which is itself the signal to go and label them.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect

_purpose: contextvars.ContextVar[str] = contextvars.ContextVar("llm_purpose", default="unlabelled")


def current_purpose() -> str:
    return _purpose.get()


@contextlib.contextmanager
def llm_purpose(label: str):
    token = _purpose.set(label)
    try:
        yield
    finally:
        _purpose.reset(token)


def with_llm_purpose(label: str):
    """Decorator form for async functions or async generators whose whole body is one purpose.

    For a generator the label is set around each step and reset before the value is
    yielded: a ``with`` spanning the ``async for`` would leak the label into the caller
    between yields, stay set if the caller broke out early, and raise on reset when an
    abandoned generator is closed from another task's context."""

    def wrap(fn):
        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def gen(*args, **kwargs):
                agen = fn(*args, **kwargs)
                try:
                    while True:
                        token = _purpose.set(label)
                        try:
                            item = await agen.__anext__()
                        except StopAsyncIteration:
                            return
                        finally:
                            _purpose.reset(token)
                        yield item
                finally:
                    await agen.aclose()

            return gen

        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            with llm_purpose(label):
                return await fn(*args, **kwargs)

        return inner

    return wrap
