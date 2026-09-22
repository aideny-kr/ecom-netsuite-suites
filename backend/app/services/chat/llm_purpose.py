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

    A generator's body sees its own purpose (the label, or a nested ``llm_purpose``
    it opened) while it runs, cleanup included; the caller sees its own between
    yields. A ``with`` spanning the ``async for`` would leak the label into the caller,
    stay set after an early break, and raise on reset when an abandoned generator is
    closed from another task's context. So each step sets the body's purpose, records
    what the body left, and resets before the value is yielded."""

    def wrap(fn):
        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def gen(*args, **kwargs):
                agen = fn(*args, **kwargs)
                inside = label
                try:
                    while True:
                        token = _purpose.set(inside)
                        try:
                            item = await agen.__anext__()
                        except StopAsyncIteration:
                            return
                        finally:
                            inside = _purpose.get()
                            _purpose.reset(token)
                        yield item
                finally:
                    token = _purpose.set(inside)
                    try:
                        await agen.aclose()
                    finally:
                        _purpose.reset(token)

            return gen

        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            with llm_purpose(label):
                return await fn(*args, **kwargs)

        return inner

    return wrap
