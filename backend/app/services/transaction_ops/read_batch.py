"""Request-local coalescing of reference reads during group preparation only.

Never cache transactions, queries, credentials, failures, or write preflights.
Every reader still authenticates and verifies its tenant/connection/account.
"""

import asyncio
import copy
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar

_REFERENCE = re.compile(r"/record/v1/(?:currency|salesTaxItem|accountingPeriod)/[0-9]{1,30}")
_batch = ContextVar("accounting_reference_read_batch", default=None)


class ReferenceReads:
    def __init__(self):
        self.values = {}
        self.locks = {}
        self.hits = 0

    async def read(self, key, fetch):
        async with self.locks.setdefault(key, asyncio.Lock()):
            cached = self.values.get(key)
            if cached and time.monotonic() - cached[0] < 60:
                self.hits += 1
                return copy.deepcopy(cached[1])
            value = await fetch()
            self.values[key] = (time.monotonic(), copy.deepcopy(value))
            return value


@contextmanager
def reference_read_batch():
    batch = ReferenceReads()
    token = _batch.set(batch)
    try:
        yield batch
    finally:
        _batch.reset(token)


async def reference_read(scope, method, path, params, body, fetch):
    batch = _batch.get()
    if batch is None or scope is None or method != "GET" or params or body or not _REFERENCE.fullmatch(path):
        return await fetch()
    return await batch.read((*scope, path), fetch)
