"""Retry only known transient failures of fixed, read-only evidence operations."""

import asyncio

import httpx

from app.services.transaction_ops.metabase_reader import ReplicaReadError
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError
from app.services.transaction_ops.source_reader import SourceReadError
from app.services.transaction_ops.state_service import StateError

MAX_READ_RETRIES = 3


class ReadBudgetExhaustedError(Exception):
    pass


def transient_read_code(exc):
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
        return "provider_transport_failed"
    if isinstance(exc, SourceReadError) and exc.code in {"source_transport_failed", "source_rate_limited"}:
        return exc.code
    if isinstance(exc, ReplicaReadError) and exc.code == "replica_transport_failed":
        return exc.code
    if isinstance(exc, NetSuiteEvidenceError) and str(exc) in {
        "read_timeout",
        "upstream_http_429",
        "upstream_http_502",
        "upstream_http_503",
        "upstream_http_504",
    }:
        return "netsuite_" + str(exc)
    return None


async def read_with_recovery(factory, *, retry_calls, progress, reserve, save, remaining, sleep=asyncio.sleep):
    """The caller reserves the first read; every retry reserves its full cost.

    The persisted count spans continuations. Unknown failures, incomplete
    evidence, authentication failures and writes are never retried here.
    """
    while True:
        seconds = remaining()
        if seconds <= 0:
            raise TimeoutError
        try:
            async with asyncio.timeout(min(seconds, 170)):
                return await factory()
        except StateError:
            # Lease/tenant/state fencing is never a provider retry or a reason
            # for this helper to attempt another progress write.
            raise
        except Exception as exc:
            if remaining() <= 0:
                raise TimeoutError from None
            code = transient_read_code(exc)
            retries = progress.get("read_retry_count", 0)
            if not code or retry_calls <= 0 or type(retries) is not int or not 0 <= retries < MAX_READ_RETRIES:
                progress["last_read_error_code"] = code or "unclassified_read_failure"
                progress["last_read_error_type"] = type(exc).__name__[:80]
                await save()
                raise
            delay = 2**retries
            if remaining() <= delay:
                raise
            if not await reserve(retry_calls):
                raise ReadBudgetExhaustedError from None
            progress["read_retry_count"] = retries + 1
            progress["last_read_retry_code"] = code
            await save()
            await sleep(delay)
