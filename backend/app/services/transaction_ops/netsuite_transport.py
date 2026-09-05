"""Bounded NetSuite guard transport. No direct record PATCH or request retry.

The transaction executor supplies the current approved claim only after source
revalidation. This adapter independently compares the live server-side guard
snapshot and commits a one-use send reservation before its one external write.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import select

from app.core.database import set_tenant_context
from app.core.encryption import decrypt_credentials
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.transaction_ops import TransactionOperation
from app.schemas.transaction_runs import ClaimedOperation, _bounded_json
from app.services.netsuite_oauth_service import get_valid_token
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.netsuite_actions import NetSuiteActionError, validate_guard_url
from app.services.transaction_ops.netsuite_reader import _account, _invalid_constant, _object

MAX_GUARD_READ_CALLS = 4  # Conservative allowance including ordinary OAuth refresh.
MAX_RESPONSE_BYTES = 2_000_000
READ_TIMEOUT_SECONDS = 60
_TIMEOUT = httpx.Timeout(connect=5, read=25, write=10, pool=5)


async def _guard_connection(db, tenant_id, config):
    await set_tenant_context(db, str(tenant_id))
    connection = (
        await db.execute(
            select(Connection)
            .where(
                Connection.id == config.netsuite_connection_id,
                Connection.tenant_id == tenant_id,
                Connection.provider == "netsuite",
                Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if connection is None:
        raise NetSuiteActionError("guard_connection_unavailable")
    try:
        credentials = decrypt_credentials(connection.encrypted_credentials)
        if _account(credentials.get("account_id")) != _account(config.netsuite_account_id):
            raise ValueError
        url = validate_guard_url(
            (connection.metadata_json or {}).get("transaction_ops_guard_url"), config.netsuite_account_id
        )
    except Exception:
        raise NetSuiteActionError("guard_connection_unavailable") from None
    return connection, url


async def _load_guard_credentials(db, tenant_id, config):
    connection, url = await _guard_connection(db, tenant_id, config)
    try:
        token = await asyncio.wait_for(get_valid_token(db, connection), timeout=45)
    except Exception:
        raise NetSuiteActionError("guard_authentication_failed") from None
    finally:
        await set_tenant_context(db, str(tenant_id))
    if not isinstance(token, str) or not token:
        raise NetSuiteActionError("guard_authentication_failed")
    return url, token


async def _request(client, method, url, token, *, params=None, payload=None):
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query))
    query.update(params or {})
    url = urlunsplit(parsed._replace(query=urlencode(query)))
    try:
        async with client.stream(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=_TIMEOUT,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise NetSuiteActionError("guard_http_error")
            data = bytearray()
            async for part in response.aiter_bytes():
                if len(data) + len(part) > MAX_RESPONSE_BYTES:
                    raise NetSuiteActionError("guard_response_budget")
                data.extend(part)
            result = json.loads(data, parse_float=Decimal, parse_constant=_invalid_constant, object_pairs_hook=_object)
    except NetSuiteActionError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, RecursionError, UnicodeError, DecimalException):
        raise NetSuiteActionError("guard_response_unavailable") from None
    if not isinstance(result, dict) or type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        raise NetSuiteActionError("guard_contract_mismatch")
    return result


async def _snapshot(client, url, token, config, record_id):
    reference = config.mapping_json["reference_field"]
    body = await _request(
        client, "GET", url, token, params={"action": "snapshot", "record_id": record_id, "reference_field": reference}
    )
    try:
        snapshot = _bounded_json(body.get("snapshot"))
        if (
            body.get("success") is not True
            or _account(body.get("account_id")) != _account(config.netsuite_account_id)
            or snapshot.get("record_id") != record_id
            or snapshot.get("subsidiary") != config.subsidiary_id
            or snapshot.get("reference_field") != reference
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise NetSuiteActionError("guard_snapshot_unavailable") from None
    return {
        "snapshot": snapshot,
        "actions_enabled": body.get("actions_enabled") is True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


async def read_guard_snapshot(db, tenant_id, config, record_id, *, client=None):
    """Read capability/snapshot after the caller reserves MAX_GUARD_READ_CALLS."""

    async def run(http):
        async with asyncio.timeout(READ_TIMEOUT_SECONDS):
            url, token = await _load_guard_credentials(db, tenant_id, config)
            return await _snapshot(http, url, token, config, record_id)

    try:
        if client is not None:
            return await run(client)
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as owned:
            return await run(owned)
    except TimeoutError:
        raise NetSuiteActionError("guard_read_timeout") from None


async def dispatch_netsuite_operation(db, tenant_id, claimed: ClaimedOperation, *, client=None):
    if claimed.action != "correct_amounts" or claimed.target_record_id is None:
        raise NetSuiteActionError("unsupported_guard_action")
    await set_tenant_context(db, str(tenant_id))
    previous = (
        await db.execute(
            select(TransactionOperation)
            .where(
                TransactionOperation.id == claimed.operation_id,
                TransactionOperation.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if previous is None or previous.proposal_id != claimed.proposal_id or previous.work_key != claimed.work_key:
        raise NetSuiteActionError("claimed_operation_mismatch")
    if (previous.result_json or {}).get("dispatch_reserved") is True:
        return {"status": "unknown", "code": "dispatch_already_reserved", "verified": False}
    permit = await state.reserve_operation_budget(db, tenant_id, claimed.operation_id, api_calls=MAX_GUARD_READ_CALLS)
    if permit is None:
        raise NetSuiteActionError("operation_budget_exhausted")
    proposal = await state.get_proposal(db, tenant_id, claimed.proposal_id)
    config = await state.get_config(db, tenant_id, claimed.config_id)
    reserved = False

    async def run(http):
        nonlocal reserved
        remaining = (permit.deadline_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise NetSuiteActionError("operation_budget_exhausted")
        async with asyncio.timeout(min(READ_TIMEOUT_SECONDS, remaining)):
            url, token = await _load_guard_credentials(db, tenant_id, config)
            fresh = await _snapshot(http, url, token, config, claimed.target_record_id)
            if not fresh["actions_enabled"]:
                raise NetSuiteActionError("guard_disabled")
            if fresh["snapshot"] != claimed.before_json:
                raise NetSuiteActionError("guard_evidence_changed")
            # Recheck local credential ownership/lifecycle after the network
            # read, and the current human/config/flags at the dispatch choke.
            _, current_url = await _guard_connection(db, tenant_id, config)
            if current_url != url:
                raise NetSuiteActionError("guard_connection_changed")
            payload = {
                "schema_version": 1,
                "action": claimed.action,
                "account_id": claimed.netsuite_account_id,
                "work_key": claimed.work_key,
                "approval_expires_at": min(proposal.valid_until, permit.deadline_at).isoformat(),
                "before": claimed.before_json,
                "after": claimed.after_json,
            }
            fingerprint = state.business_digest({"method": "POST", "url": url, "payload": payload})
            if not await state.reserve_operation_dispatch(
                db, tenant_id, claimed, provider="netsuite", payload_fingerprint=fingerprint
            ):
                return {"status": "unknown", "code": "dispatch_already_reserved", "verified": False}
            reserved = True
            response = await _request(http, "POST", url, token, payload=payload)
            if (
                response.get("success") is False
                and response.get("status") == "rejected"
                and response.get("verified") is False
            ):
                return {"status": "failed", "code": "guard_rejected", "verified": False}
            if (
                response.get("success") is True
                and response.get("status") == "saved"
                and response.get("record_id") == claimed.target_record_id
                and response.get("work_key") == claimed.work_key
                and response.get("verified") is False
            ):
                return {"status": "accepted", "record_id": claimed.target_record_id, "verified": False}
            return {"status": "unknown", "code": "provider_write_outcome_unknown", "verified": False}

    try:
        if client is not None:
            return await run(client)
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as owned:
            return await run(owned)
    except (NetSuiteActionError, TimeoutError) as error:
        if reserved:
            return {"status": "unknown", "code": "provider_write_outcome_unknown", "verified": False}
        if isinstance(error, NetSuiteActionError):
            raise
        raise NetSuiteActionError("guard_read_timeout") from None
