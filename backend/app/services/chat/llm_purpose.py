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
    """Decorator form for async functions or async generators whose whole body is one purpose."""
    import functools
    import inspect

    def wrap(fn):
        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def gen(*args, **kwargs):
                with llm_purpose(label):
                    async for item in fn(*args, **kwargs):
                        yield item

            return gen

        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            with llm_purpose(label):
                return await fn(*args, **kwargs)

        return inner

    return wrap
