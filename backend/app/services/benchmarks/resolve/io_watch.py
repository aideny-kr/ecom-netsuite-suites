"""Every IO failure in a trial, observed where it happens instead of where a tool reports it.

Several read tools turn their own failures into normal-looking results (an empty search, a
"reference unavailable", a breakdown that "could not check"). Detecting each tool's failure
shape found one more every review round (6, 7, 8), so the benchmark watches the IO itself:
an HTTP request that raises, is cancelled, or answers 5xx/429, and a database error, during
a trial. A read whose call saw one is never recorded, and the trial is not comparable.

Watched: httpx (including response bodies streamed after ``send`` returns), requests and
aiohttp requests; database errors. A tool whose IO goes through anything else is not
offered in the benchmark (tape.READ_TOOLS), so the set of tools and the set of watched
paths stay the same set (review round 9).

Failure statuses: 5xx, 429, and the auth refusals 401/403/407 (the environment, not the
agent). Other 4xx are the agent's own mistakes (a bad query, a wrong id) and replay alike.

Model hosts are left to the token meter (meter.py): a rate-limited call that the SDK retried
successfully changed nothing the agent saw. Integrity errors are left alone because some
paths raise them on purpose (insert-or-skip).

Each trial also gets its own in-memory result cache (``trial_result_cache``), so the tools
that read earlier results (present, pivot, reference_previous_result) never reach Redis or
another session's results.
"""

from __future__ import annotations

from contextlib import contextmanager

MODEL_HOSTS = frozenset({"api.anthropic.com"})


def _failed(status: int) -> bool:
    return status >= 500 or status in {401, 403, 407, 429}


def _model_host(host) -> bool:
    return str(host or "") in MODEL_HOSTS


@contextmanager
def watching(counter):
    """Count IO failures on ``counter.io_failures`` while the block runs (one trial at a time)."""
    from urllib.parse import urlsplit

    import aiohttp
    import httpx
    import requests
    from sqlalchemy import event, exc
    from sqlalchemy.engine import Engine

    originals = {
        (httpx.AsyncClient, "send"): httpx.AsyncClient.send,
        (httpx.Client, "send"): httpx.Client.send,
        (requests.Session, "send"): requests.Session.send,
        (aiohttp.ClientSession, "_request"): aiohttp.ClientSession._request,
    }

    def fail():
        counter.io_failures += 1

    class WatchedAsyncBody(httpx.AsyncByteStream):
        """A body read after ``send`` returned: a failure while streaming it is an IO failure."""

        def __init__(self, inner):
            self._inner = inner

        async def __aiter__(self):
            try:
                async for chunk in self._inner:
                    yield chunk
            except GeneratorExit:  # the reader stopped early: not a failure
                raise
            except BaseException:
                fail()
                raise

        async def aclose(self):
            await self._inner.aclose()

    class WatchedSyncBody(httpx.SyncByteStream):
        def __init__(self, inner):
            self._inner = inner

        def __iter__(self):
            try:
                yield from self._inner
            except GeneratorExit:
                raise
            except BaseException:
                fail()
                raise

        def close(self):
            self._inner.close()

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
        if watched and isinstance(response.stream, httpx.AsyncByteStream):
            response.stream = WatchedAsyncBody(response.stream)
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
        if watched and isinstance(response.stream, httpx.SyncByteStream):
            response.stream = WatchedSyncBody(response.stream)
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

    async def aiohttp_request(self, method, str_or_url, *args, **kwargs):
        watched = not _model_host(getattr(aiohttp.client.URL(str(str_or_url)), "host", None))
        try:
            response = await originals[(aiohttp.ClientSession, "_request")](self, method, str_or_url, *args, **kwargs)
        except BaseException:
            if watched:
                fail()
            raise
        if watched and _failed(response.status):
            fail()
        return response

    def on_db_error(context):
        if not isinstance(context.sqlalchemy_exception, exc.IntegrityError):
            fail()

    replacements = {
        (httpx.AsyncClient, "send"): async_send,
        (httpx.Client, "send"): sync_send,
        (requests.Session, "send"): requests_send,
        (aiohttp.ClientSession, "_request"): aiohttp_request,
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


class TrialRedis:
    """The few hash commands the result cache uses, held in memory for one trial."""

    def __init__(self):
        self._hashes: dict[str, dict[str, str]] = {}

    def hset(self, key, field, value):
        self._hashes.setdefault(key, {})[field] = value
        return 1

    def hget(self, key, field):
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self._hashes.get(key, {}))

    def hdel(self, key, *fields):
        bucket = self._hashes.get(key, {})
        return sum(1 for field in fields if bucket.pop(field, None) is not None)

    def hlen(self, key):
        return len(self._hashes.get(key, {}))

    def expire(self, key, seconds):
        return key in self._hashes


@contextmanager
def trial_result_cache():
    """The chat result cache, in memory and private to this trial (review round 9)."""
    from app.services.chat import result_cache

    original, store = result_cache._get_redis, TrialRedis()
    result_cache._get_redis = lambda: store
    try:
        yield store
    finally:
        result_cache._get_redis = original
