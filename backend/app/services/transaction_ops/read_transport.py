"""Reuse a collection's HTTP connections, never its credentials or responses."""

from contextlib import contextmanager
from contextvars import ContextVar

import httpx

_client = ContextVar("netsuite_collection_client", default=None)


@contextmanager
def collection_transport(client):
    token = _client.set(client)
    try:
        yield
    finally:
        _client.reset(token)


def current_transport():
    return _client.get()


class CollectionTransport:
    def __init__(self):
        self.clients = {}

    async def get(self, scope, timeout):
        if scope not in self.clients:
            # A credential rotation gets a new client/cookie jar. Bound rotations
            # as well as ordinary connections for a long-running collection.
            if len(self.clients) >= 4:
                oldest = next(iter(self.clients))
                await self.clients.pop(oldest).aclose()
            self.clients[scope] = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        return self.clients[scope]

    async def get_public(self, scope, base_url):
        from app.services.public_http import PublicHTTPTransport

        key = ("public_source", *scope, base_url)
        if key not in self.clients:
            if len(self.clients) >= 4:
                oldest = next(iter(self.clients))
                await self.clients.pop(oldest).aclose()
            # Orders can be more than five seconds apart. Keep the verified
            # connection warm across intervening ERP reads and checkpoints.
            upstream = httpx.AsyncHTTPTransport(
                retries=0,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=60),
            )
            self.clients[key] = httpx.AsyncClient(
                transport=PublicHTTPTransport(base_url, transport=upstream),
                trust_env=False,
                follow_redirects=False,
            )
        return self.clients[key]

    async def aclose(self):
        try:
            for client in self.clients.values():
                await client.aclose()
        finally:
            self.clients.clear()
