"""Retry only known transient failures of fixed, read-only evidence operations."""

import asyncio
from datetime import datetime, timezone

import httpx

from app.services.transaction_ops.metabase_reader import ReplicaReadError
from app.services.transaction_ops.netsuite_reader import NetSuiteEvidenceError
from app.services.transaction_ops.source_reader import SourceReadError
from app.services.transaction_ops.state_service import StateError

MAX_READ_RETRIES = 3


class ReadBudgetExhaustedError(Exception):
    pass


def transient_read_code(exc):
    if isinstance(exc, TimeoutError):
        return "provider_read_timeout"
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
        return "provider_transport_failed"
    if isinstance(exc, SourceReadError) and exc.code in {"source_transport_failed", "source_rate_limited"}:
        return exc.code
    if isinstance(exc, ReplicaReadError) and exc.code == "replica_transport_failed":
        return exc.code
    if isinstance(exc, NetSuiteEvidenceError) and str(exc) in {
        "read_timeout",
        "read_transport_failed",
        "upstream_http_429",
        "upstream_http_502",
        "upstream_http_503",
        "upstream_http_504",
    }:
        return "netsuite_" + str(exc)
    return None


def safe_read_code(exc):
    """Only code-owned provider reasons can enter durable state; never error text."""
    transient = transient_read_code(exc)
    if transient:
        return transient
    if isinstance(exc, NetSuiteEvidenceError) and str(exc) in {
        "authentication_failed",
        "invalid_connection",
        "invalid_connection_account",
        "account_mismatch",
        "invalid_upstream_response",
        "response_budget",
        "api_call_budget",
        "invalid_read_budget",
        "identity_result_budget",
        "invalid_identity_result",
        "currency_identity_mismatch",
        "invalid_collection",
        "invalid_account",
        "invalid_tenant",
        "invalid_subsidiary",
        "invalid_reference_field",
        "invalid_order_reference",
        "upstream_http_400",
        "upstream_http_401",
        "upstream_http_403",
        "upstream_http_404",
        "upstream_http_500",
        "invalid_dependency_change_scope",
        "dependency_change_page_incomplete",
        "dependency_owner_scope_invalid",
        "dependency_owner_page_incomplete",
        "dependency_owner_identity_unproven",
        "dependency_owner_budget",
        "dependency_owner_depth",
        "dependency_checkpoint_invalid",
    }:
        return "netsuite_" + str(exc)
    return "unclassified_read_failure"


def read_failure(exc, progress, *, stage):
    pending = progress.get("pending_refs")
    reference = pending[0] if isinstance(pending, list) and pending else None
    return {
        "code": safe_read_code(exc),
        "retryable": transient_read_code(exc) is not None,
        "resolved": False,
        "stage": stage,
        "order_reference": reference
        if isinstance(reference, str) and len(reference) <= 255 and all(ord(c) >= 32 for c in reference)
        else None,
        "cursor": {
            key: value
            for key in ("page", "last_source_id", "refund_after_id", "destination_after_id")
            if type(value := progress.get(key)) is int and value >= 0
        },
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


async def read_with_recovery(
    factory, *, retry_calls, progress, reserve, save, remaining, sleep=asyncio.sleep, stage="unknown"
):
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
                result = await factory()
            previous = progress.get("last_read_failure") or {}
            pending = progress.get("pending_refs") or []
            if previous.get("stage") == stage and previous.get("order_reference") == (pending[0] if pending else None):
                progress["last_read_failure"] = {
                    **previous,
                    "resolved": True,
                    "recovered_at": datetime.now(timezone.utc).isoformat(),
                }
                for key in ("last_read_error_code", "last_read_error_type", "read_stop_reason"):
                    progress.pop(key, None)
            return result
        except StateError:
            # Lease/tenant/state fencing is never a provider retry or a reason
            # for this helper to attempt another progress write.
            raise
        except Exception as exc:
            if remaining() <= 0:
                raise TimeoutError from None
            code = transient_read_code(exc)
            retries = progress.get("read_retry_count", 0)
            progress["last_read_failure"] = read_failure(exc, progress, stage=stage)
            if not code or retry_calls <= 0 or type(retries) is not int or not 0 <= retries <= MAX_READ_RETRIES:
                progress["last_read_error_code"] = safe_read_code(exc)
                progress["last_read_error_type"] = type(exc).__name__[:80]
                await save()
                raise
            if retries == MAX_READ_RETRIES:
                # Keep the unread order at its checkpoint and use the existing
                # finite budget continuation. Do not reset the persisted retry
                # count or turn a transient outage into a restart from page one.
                progress["last_read_error_code"] = code
                progress["last_read_error_type"] = type(exc).__name__[:80]
                progress["read_stop_reason"] = "retry_limit"
                await save()
                raise ReadBudgetExhaustedError from None
            delay = 2**retries
            if remaining() <= delay:
                progress["read_stop_reason"] = "retry_deadline"
                await save()
                raise ReadBudgetExhaustedError from None
            # A failed reservation can atomically finish the run. Persist the
            # diagnostic while its lease is still writable, not after reserve.
            await save()
            if not await reserve(retry_calls):
                raise ReadBudgetExhaustedError from None
            progress["read_retry_count"] = retries + 1
            progress["last_read_retry_code"] = code
            await save()
            await sleep(delay)
