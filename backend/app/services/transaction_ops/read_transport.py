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

    async def aclose(self):
        try:
            for client in self.clients.values():
                await client.aclose()
        finally:
            self.clients.clear()
