"""Bounded NetSuite guard transport. No direct record PATCH or request retry.

The transaction executor supplies the current approved claim only after source
revalidation. This adapter independently compares the live server-side guard
snapshot and commits a one-use send reservation before its one external write.
"""

from __future__ import annotations

import asyncio
import json
import re
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
from app.services.transaction_ops.netsuite_create import CreateInputError, validate_create_preview
from app.services.transaction_ops.netsuite_reader import _account, _invalid_constant, _object
from app.services.transaction_ops.normalization import NetSuiteLegacyTaxMapping, TransactionMapping, _time

MAX_GUARD_READ_CALLS = 4  # Conservative allowance including ordinary OAuth refresh.
MAX_RESPONSE_BYTES = 2_000_000
READ_TIMEOUT_SECONDS = 60
DISPATCH_TIMEOUT_SECONDS = 120  # OAuth, final snapshot and one write; still capped by the operation deadline.
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
    params = {"action": "snapshot", "record_id": record_id, "reference_field": reference}
    identity_mode = config.mapping_json.get("line_identity_mode", "source_line_id")
    if identity_mode not in {"source_line_id", "inventory_units"}:
        raise NetSuiteActionError("guard_line_identity_unproven")
    if identity_mode == "inventory_units":
        params["line_identity_mode"] = identity_mode
    expected_profile = None
    if config.mapping_json.get("netsuite_legacy_tax") is not None:
        try:
            profile = NetSuiteLegacyTaxMapping.model_validate(config.mapping_json["netsuite_legacy_tax"])
            if (
                profile.account_id != _account(config.netsuite_account_id)
                or profile.subsidiary_id != config.subsidiary_id
            ):
                raise ValueError
        except ValueError:
            raise NetSuiteActionError("guard_profile_scope_mismatch") from None
        expected_profile = {"mode": profile.mode, "tax_code_id": profile.tax_code_id}
        params.update(tax_mode=profile.mode, tax_code_id=profile.tax_code_id)
    body = await _request(client, "GET", url, token, params=params)
    try:
        snapshot = _bounded_json(body.get("snapshot"))
        if (
            body.get("success") is not True
            or _account(body.get("account_id")) != _account(config.netsuite_account_id)
            or snapshot.get("record_id") != record_id
            or snapshot.get("subsidiary") != config.subsidiary_id
            or snapshot.get("reference_field") != reference
            or snapshot.get("tax_profile") != expected_profile
            or snapshot.get("line_identity_mode") != ("inventory_units" if identity_mode == "inventory_units" else None)
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


def _create_scope(config, payload):
    try:
        mapping = TransactionMapping.model_validate(config.mapping_json)
        profile = mapping.netsuite_legacy_tax
        if (
            mapping.netsuite_create is None
            or profile is None
            or mapping.reference_field != "tranid"
            or mapping.line_identity_mode != "inventory_units"
            or payload["account_id"] != _account(config.netsuite_account_id)
            or payload["subsidiary_id"] != config.subsidiary_id
            or payload["tax_profile"] != profile.model_dump(mode="json")
            or profile.account_id != payload["account_id"]
            or profile.subsidiary_id != payload["subsidiary_id"]
            or payload["inventory_mode"] != mapping.netsuite_create.inventory_mode
            or mapping.currency_minor_units.get(payload["currency"]["symbol"]) != payload["currency"]["precision"]
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise NetSuiteActionError("guard_create_scope_mismatch") from None


async def _create_preview(client, url, token, config, payload):
    _create_scope(config, payload)
    request = _bounded_json(
        {"schema_version": 1, "action": "preview_create", "account_id": config.netsuite_account_id, "input": payload}
    )
    body = await _request(client, "POST", url, token, payload=request)
    try:
        if body.get("success") is not True or _account(body.get("account_id")) != _account(config.netsuite_account_id):
            raise ValueError
        preview = validate_create_preview(payload, body.get("preview"))
    except (ValueError, TypeError):
        raise NetSuiteActionError("guard_create_preview_unavailable") from None
    return {
        "preview": preview,
        "create_enabled": body.get("create_enabled") is True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


async def read_create_preview(db, tenant_id, config, payload, *, client=None):
    """Unsaved native draft; caller must reserve MAX_GUARD_READ_CALLS first."""

    async def run(http):
        async with asyncio.timeout(READ_TIMEOUT_SECONDS):
            _create_scope(config, payload)
            url, token = await _load_guard_credentials(db, tenant_id, config)
            return await _create_preview(http, url, token, config, payload)

    try:
        if client is not None:
            return await run(client)
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as owned:
            return await run(owned)
    except TimeoutError:
        raise NetSuiteActionError("guard_read_timeout") from None


async def read_created_snapshot(db, tenant_id, config, record_id, approved_after, *, client=None):
    """Read attribution and actual creation fields without building another draft."""

    async def run(http):
        async with asyncio.timeout(READ_TIMEOUT_SECONDS):
            payload = approved_after["input"]
            _create_scope(config, payload)
            preview = validate_create_preview(payload, approved_after["preview"])
            if not isinstance(record_id, str) or not re.fullmatch(r"[1-9][0-9]{0,29}", record_id):
                raise NetSuiteActionError("guard_created_identity_unproven")
            url, token = await _load_guard_credentials(db, tenant_id, config)
            profile = {key: payload["tax_profile"][key] for key in ("mode", "tax_code_id")}
            params = {
                "action": "created_snapshot",
                "record_id": record_id,
                "line_count": str(len(payload["lines"])),
                "tax_mode": profile["mode"],
                "tax_code_id": profile["tax_code_id"],
                "inventory_mode": payload["inventory_mode"],
            }
            body = await _request(http, "GET", url, token, params=params)
            creation = _bounded_json(body.get("creation"))
            if (
                body.get("success") is not True
                or _account(body.get("account_id")) != _account(config.netsuite_account_id)
                or set(creation) != {"record_id", "version", "work_key", "tax_profile", "inventory_mode", "record"}
                or creation["record_id"] != record_id
                or creation["tax_profile"] != profile
                or creation["inventory_mode"] != payload["inventory_mode"]
                or not isinstance(creation["work_key"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", creation["work_key"])
            ):
                raise ValueError
            if not isinstance(creation["version"], str) or _time(creation["version"]) > datetime.now(timezone.utc):
                raise ValueError
            validate_create_preview(payload, {**preview, "record": creation["record"]})
            return {"creation": creation, "observed_at": datetime.now(timezone.utc).isoformat()}

    try:
        if client is not None:
            return await run(client)
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as owned:
            return await run(owned)
    except TimeoutError:
        raise NetSuiteActionError("guard_read_timeout") from None
    except (CreateInputError, ValueError, TypeError, KeyError):
        raise NetSuiteActionError("guard_created_snapshot_unavailable") from None


async def dispatch_netsuite_operation(db, tenant_id, claimed: ClaimedOperation, *, client=None):
    creating = claimed.action == "sync_missing_order"
    if (
        claimed.action not in {"correct_amounts", "sync_missing_order"}
        or (creating and claimed.target_record_id is not None)
        or (not creating and claimed.target_record_id is None)
    ):
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
        async with asyncio.timeout(min(DISPATCH_TIMEOUT_SECONDS, remaining)):
            url, token = await _load_guard_credentials(db, tenant_id, config)
            if creating:
                if set(claimed.after_json) != {"input", "preview"} or claimed.before_json != {
                    "missing": True,
                    "order_reference": claimed.after_json["input"]["order_reference"],
                }:
                    raise NetSuiteActionError("guard_create_intent_mismatch")
                fresh = await _create_preview(http, url, token, config, claimed.after_json["input"])
                if not fresh["create_enabled"]:
                    raise NetSuiteActionError("guard_create_disabled")
                if fresh["preview"] != claimed.after_json["preview"]:
                    raise NetSuiteActionError("guard_evidence_changed")
            else:
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
                "approval_expires_at": min(proposal.valid_until, permit.deadline_at)
                .astimezone(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "before": claimed.before_json,
                "after": claimed.after_json,
            }
            if creating:
                payload = _bounded_json(payload)
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
            record_id = response.get("record_id")
            identity_matches = (
                isinstance(record_id, str) and bool(re.fullmatch(r"[1-9][0-9]{0,29}", record_id))
                if creating
                else record_id == claimed.target_record_id
            )
            if (
                response.get("success") is True
                and response.get("status") == "saved"
                and identity_matches
                and response.get("work_key") == claimed.work_key
                and response.get("verified") is False
            ):
                return {"status": "accepted", "record_id": response["record_id"], "verified": False}
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
