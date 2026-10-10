"""Every IO failure in a trial, observed where it happens instead of where a tool reports it.

Several read tools turn their own failures into normal-looking results (an empty search, a
"reference unavailable", a breakdown that "could not check"). Detecting each tool's failure
shape found one more every review round (6, 7, 8), so the benchmark watches the IO itself:
an HTTP request that raises, is cancelled, or answers 5xx/429, and a database error, during
a trial. A read whose call saw one is never recorded, and the trial is not comparable.

Model hosts are left to the token meter (meter.py): a rate-limited call that the SDK retried
successfully changed nothing the agent saw. Integrity errors are left alone because some
paths raise them on purpose (insert-or-skip).
"""

from __future__ import annotations

from contextlib import contextmanager

MODEL_HOSTS = frozenset({"api.anthropic.com"})


def _failed(status: int) -> bool:
    return status >= 500 or status == 429


def _model_host(host) -> bool:
    return str(host or "") in MODEL_HOSTS


@contextmanager
def watching(counter):
    """Count IO failures on ``counter.io_failures`` while the block runs (one trial at a time)."""
    from urllib.parse import urlsplit

    import httpx
    import requests
    from sqlalchemy import event, exc
    from sqlalchemy.engine import Engine

    originals = {
        (httpx.AsyncClient, "send"): httpx.AsyncClient.send,
        (httpx.Client, "send"): httpx.Client.send,
        (requests.Session, "send"): requests.Session.send,
    }

    def fail():
        counter.io_failures += 1

    async def async_send(self, request, *args, **kwargs):
        watched = not _model_host(request.url.host)
        try:
            response = await originals[(httpx.AsyncClient, "send")](self, request, *args, **kwargs)
        except BaseException:
            if watched:
                fail()
            raise
        if watched and _failed(response.status_code):
            fail()
        return response

    def sync_send(self, request, *args, **kwargs):
        watched = not _model_host(request.url.host)
        try:
            response = originals[(httpx.Client, "send")](self, request, *args, **kwargs)
        except BaseException:
            if watched:
                fail()
            raise
        if watched and _failed(response.status_code):
            fail()
        return response

    def requests_send(self, request, *args, **kwargs):
        watched = not _model_host(urlsplit(request.url).hostname)
        try:
            response = originals[(requests.Session, "send")](self, request, *args, **kwargs)
        except BaseException:
            if watched:
                fail()
            raise
        if watched and _failed(response.status_code):
            fail()
        return response

    def on_db_error(context):
        if not isinstance(context.sqlalchemy_exception, exc.IntegrityError):
            fail()

    replacements = {
        (httpx.AsyncClient, "send"): async_send,
        (httpx.Client, "send"): sync_send,
        (requests.Session, "send"): requests_send,
    }
    for (owner, name), fn in replacements.items():
        setattr(owner, name, fn)
    event.listen(Engine, "handle_error", on_db_error)
    try:
        yield counter
    finally:
        event.remove(Engine, "handle_error", on_db_error)
        for (owner, name), fn in originals.items():
            setattr(owner, name, fn)
