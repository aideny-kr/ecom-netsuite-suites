"""Exact Celigo error evidence and one reserved administrative resolution.

Generic Celigo tools remain read-only. This module never invokes an import,
previews an import, changes retry data, starts a flow, or retries a request.
The executor owns financial verification and operation completion. A successful
PUT receipt is not proof that a transaction is correct or that an error closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import and_, select

from app.core.database import set_tenant_context
from app.core.encryption import decrypt_credentials
from app.models.celigo import CeligoFlow, CeligoFlowStep
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.models.tenant import Tenant
from app.schemas.transaction_runs import ClaimedOperation
from app.services.celigo.client import CELIGO_BASE_URLS
from app.services.celigo.graph import walk_script_refs
from app.services.celigo.repository import extract_flow_steps
from app.services.celigo.sanitizer import sanitize
from app.services.transaction_ops import state_service

MAX_READ_CALLS = 16
_MAX_BYTES = 2 * 1024 * 1024
_MAX_ERROR_PAGES = 3
_MAX_SCRIPTS = 8
_MAX_EXPORTS = 3
_READ_SECONDS = 90
_HTTP_TIMEOUT = httpx.Timeout(connect=5, read=20, write=10, pool=5)
_REMOTE_ID = re.compile(r"[0-9a-f]{24}\Z")
_REFERENCE = re.compile(r"R[0-9]{9}(?:-[A-Z0-9]+)?\Z")
_ACCOUNT = re.compile(r"[0-9]+(?:[_-](?:SB|RP)[0-9]+)?\Z", re.I)


class CeligoActionError(ValueError):
    """Fixed machine codes only; upstream messages and payloads stay private."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fail(code):
    raise CeligoActionError(code)


def _remote(value):
    if not isinstance(value, str) or not _REMOTE_ID.fullmatch(value):
        _fail("invalid_resource_identity")
    return value


def _opaque(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._~-]{0,511}", value):
        _fail("invalid_error_identity")
    return value


def _error_metadata(error):
    for key in ("code", "source"):
        value = error.get(key)
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value)):
            _fail("invalid_error_metadata")
    occurred = error.get("occurredAt")
    if occurred is not None:
        try:
            if (
                not isinstance(occurred, str)
                or len(occurred) > 64
                or datetime.fromisoformat(occurred.replace("Z", "+00:00")).utcoffset() is None
            ):
                _fail("invalid_error_metadata")
        except ValueError:
            _fail("invalid_error_metadata")


def _account(value):
    if not isinstance(value, str) or not _ACCOUNT.fullmatch(value):
        _fail("destination_account_unproven")
    return value.replace("_", "-").lower()


def _digest(value):
    def exact(item, depth=0):
        if depth > 24:
            _fail("response_too_deep")
        if isinstance(item, Decimal):
            if not item.is_finite():
                _fail("invalid_provider_json")
            return str(item)
        if isinstance(item, dict):
            return {key: exact(val, depth + 1) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [exact(val, depth + 1) for val in item]
        return item

    return hashlib.sha256(
        json.dumps(exact(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_provider_json")
        result[key] = value
    return result


def _constant(_):
    _fail("invalid_provider_json")


class _Transport:
    def __init__(self, client, token, region):
        self.client, self.token = client, token
        self.base = CELIGO_BASE_URLS[region]
        self.calls = 0

    async def request(self, method, path, *, body=None):
        if self.calls >= MAX_READ_CALLS + (method == "PUT"):
            _fail("provider_read_budget_exhausted")
        self.calls += 1
        try:
            async with self.client.stream(
                method,
                self.base + path,
                headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"},
                json=body,
                timeout=_HTTP_TIMEOUT,
                follow_redirects=False,
            ) as response:
                status = response.status_code
                data = bytearray()
                async for part in response.aiter_bytes():
                    if len(data) + len(part) > _MAX_BYTES:
                        _fail("provider_response_too_large")
                    data.extend(part)
            if method == "PUT":
                return status, None
            if status != 200:
                _fail("provider_read_failed")
            result = json.loads(data, parse_float=Decimal, parse_constant=_constant, object_pairs_hook=_object)
            if not isinstance(result, dict):
                _fail("invalid_provider_json")
            _digest(result)  # Deliberate depth validation before recursive consumers.
            return status, result
        except CeligoActionError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, RecursionError, UnicodeError, DecimalException):
            _fail("provider_request_failed")

    async def get(self, kind, identifier):
        _, result = await self.request("GET", f"/v1/{kind}/{_remote(identifier)}")
        if result.get("_id") != identifier:
            _fail("provider_identity_changed")
        return result


async def _load_scope(db, tenant_id, target_step_id):
    try:
        tenant_id, target_step_id = UUID(str(tenant_id)), UUID(str(target_step_id))
    except (ValueError, TypeError):
        _fail("scope_unavailable")
    await set_tenant_context(db, str(tenant_id))
    result = await db.execute(
        select(CeligoFlowStep, CeligoFlow, Connection)
        .join(
            CeligoFlow,
            and_(
                CeligoFlow.id == CeligoFlowStep.flow_id,
                CeligoFlow.tenant_id == tenant_id,
                CeligoFlow.celigo_connection_id == CeligoFlowStep.celigo_connection_id,
            ),
        )
        .join(
            Connection,
            and_(
                Connection.id == CeligoFlowStep.celigo_connection_id,
                Connection.tenant_id == tenant_id,
                Connection.provider == "celigo",
                Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            ),
        )
        .join(Tenant, and_(Tenant.id == tenant_id, Tenant.is_active.is_(True)))
        .where(CeligoFlowStep.id == target_step_id, CeligoFlowStep.tenant_id == tenant_id)
    )
    rows = result.one_or_none()
    if rows is None:
        _fail("scope_unavailable")
    step, flow, connection = rows
    if (
        step.id != target_step_id
        or any(row.tenant_id != tenant_id for row in rows)
        or step.flow_id != flow.id
        or step.celigo_connection_id != connection.id
        or flow.celigo_connection_id != connection.id
        or connection.provider != "celigo"
        or connection.status not in ACTIVE_CONNECTION_STATUSES
    ):
        _fail("scope_unavailable")
    if (
        step.role != "processor"
        or step.record_type != "salesorder"
        or step.operation not in {"add", "update", "addupdate"}
    ):
        _fail("unsupported_target")
    region = (connection.metadata_json or {}).get("region", "us")
    if region not in CELIGO_BASE_URLS:
        _fail("unsupported_region")
    try:
        credential = decrypt_credentials(connection.encrypted_credentials)
        token = credential.get("token")
    except (InvalidToken, ValueError, TypeError, AttributeError):
        _fail("credentials_unavailable")
    if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
        _fail("credentials_unavailable")
    return step, flow, connection, token, region


def _subsidiary(import_doc):
    fields = (import_doc.get("netsuite_da") or {}).get("mapping", {}).get("fields", [])
    fields = [field for field in fields if isinstance(field, dict) and field.get("generate") == "subsidiary"]
    if len(fields) != 1:
        return None
    field = fields[0]
    value = field.get("hardCodedValue")
    if field.get("extract") or field.get("lookupName") or field.get("conditional"):
        return None
    return (
        str(value) if not isinstance(value, bool) and isinstance(value, (str, int)) and str(value).isdigit() else None
    )


async def _configuration(http, step, flow):
    live_flow = await http.get("flows", flow.celigo_id)
    if live_flow.get("disabled") is not False:
        _fail("inactive_flow")
    positions = [
        item
        for item in extract_flow_steps(sanitize("flow", live_flow))
        if item.celigo_id == step.celigo_id and item.role == "processor" and item.branch_id == step.branch_id
    ]
    if len(positions) != 1:
        _fail("target_not_in_flow")
    imported = await http.get("imports", step.celigo_id)
    ns = imported.get("netsuite_da") or {}
    if (
        imported.get("adaptorType") != "NetSuiteDistributedImport"
        or ns.get("recordType") != "salesorder"
        or ns.get("operation") != step.operation
    ):
        _fail("unsupported_target")
    destination_id = _remote(imported.get("_connectionId"))
    if destination_id != step.connection_celigo_id:
        _fail("destination_connection_changed")
    destination = await http.get("connections", destination_id)
    if destination.get("type") != "netsuite" or destination.get("offline") is not False:
        _fail("inactive_destination")
    ns_connection = destination.get("netsuite") or {}
    account = _account(ns_connection.get("account"))  # Live verified connection identity field.
    for key in ("accountId", "account_id"):
        if key in ns_connection and _account(ns_connection[key]) != account:
            _fail("destination_account_unproven")
    resources = [("flow", live_flow), ("import", imported)]
    generators = live_flow.get("pageGenerators")
    if not isinstance(generators, list) or not generators or len(generators) > _MAX_EXPORTS:
        _fail("unsupported_source_dependencies")
    exports = sorted({_remote(item.get("_exportId")) for item in generators if isinstance(item, dict)})
    if len(exports) != len(generators):
        _fail("unsupported_source_dependencies")
    for identifier in exports:
        resources.append(("export", await http.get("exports", identifier)))
    scripts = sorted({ref.script_id for kind, doc in resources for ref in walk_script_refs(sanitize(kind, doc))})
    if len(scripts) > _MAX_SCRIPTS:
        _fail("unsupported_script_dependencies")
    script_hashes = []
    for identifier in scripts:
        doc = await http.get("scripts", identifier)
        if not isinstance(doc.get("content"), str):
            _fail("script_content_unavailable")
        script_hashes.append({"id": identifier, "fingerprint": _digest(doc)})
    # Only hashes leave this function. Resource captures and script source can
    # contain PII/secrets; never expose or log their raw response objects.
    snapshot = {
        "resources": [{"kind": kind, "id": doc["_id"], "fingerprint": _digest(doc)} for kind, doc in resources],
        "scripts": script_hashes,
        "destination": {
            "id": destination_id,
            "account_id": account,
            "type": destination["type"],
            "offline": destination["offline"],
        },
        "branch_id": step.branch_id,
    }
    return (
        {
            "account_id": account,
            "subsidiary_id": _subsidiary(imported),
            "record_type": "salesorder",
            "operation": ns["operation"],
            "destination_connection_id": destination_id,
        },
        _digest(snapshot),
        exports,
    )


def _next_page(value, path, base):
    if not isinstance(value, str) or not value or len(value) > 2048:
        _fail("invalid_pagination")
    parsed, origin = urlsplit(value), urlsplit(base)
    if parsed.fragment or parsed.username or parsed.password or parsed.path != path:
        _fail("invalid_pagination")
    if parsed.netloc and (parsed.scheme != origin.scheme or parsed.netloc != origin.netloc):
        _fail("invalid_pagination")
    if parsed.scheme and not parsed.netloc:
        _fail("invalid_pagination")
    return parsed.path + ("?" + parsed.query if parsed.query else "")


async def _read(http, step, flow, connection, reference, error_id):
    destination, config_fingerprint, exports = await _configuration(http, step, flow)
    scope = {
        "connection_id": str(connection.id),
        "target_step_id": str(step.id),
        "flow_id": flow.celigo_id,
        "import_id": step.celigo_id,
        "branch_id": step.branch_id,
        **destination,
    }
    evidence = {
        "provider": "celigo",
        "order_reference": reference,
        "scope": scope,
        "config_fingerprint": config_fingerprint,
        "complete": False,
        "error": None,
    }
    path = f"/v1/flows/{flow.celigo_id}/{step.celigo_id}/errors"
    next_path, seen, found = path, set(), None
    for _ in range(_MAX_ERROR_PAGES):
        if next_path in seen:
            _fail("invalid_pagination")
        seen.add(next_path)
        _, page = await http.request("GET", next_path)
        errors = page.get("errors")
        if not isinstance(errors, list) or any(not isinstance(error, dict) for error in errors):
            _fail("invalid_error_page")
        matches = (
            [error for error in errors if error.get("errorId") == error_id]
            if error_id
            else [error for error in errors if error.get("traceKey") == reference]
        )
        if len(matches) > 1:
            _fail("ambiguous_error")
        if matches:
            found = matches[0]
            break
        next_value = page.get("nextPageURL")
        if not next_value:
            break
        next_path = _next_page(next_value, path, http.base)
    if found is not None:
        _error_metadata(found)
        identifier, retry_key = _opaque(found.get("errorId")), _opaque(found.get("retryDataKey"))
        _, envelope = await http.request(
            "GET", f"/v1/flows/{flow.celigo_id}/{step.celigo_id}/{quote(retry_key, safe='')}/data"
        )
        if envelope.get("retryDataKey") != retry_key or envelope.get("pgExportId") not in exports:
            _fail("retry_identity_changed")
        if envelope.get("stage") != "page_processor_import":
            _fail("unsupported_retry_stage")
        if (
            envelope.get("oneToMany") is not False
            or envelope.get("childIndex") is not None
            or envelope.get("pathToMany")
        ):
            _fail("unsupported_retry_shape")
        data = envelope.get("data")
        if not isinstance(data, dict) or data.get("number") != reference:
            _fail("error_reference_unproven")
        if found.get("traceKey") not in (None, reference) or found.get("resolved") is True:
            _fail("error_reference_unproven")
        evidence.update(
            complete=True,
            error={
                "error_id": identifier,
                "retry_data_key": retry_key,
                "code": found.get("code"),
                "source": found.get("source"),
                "occurred_at": found.get("occurredAt"),
                "kind": "duplicate_transaction" if found.get("code") == "DUP_RCRD" else "unknown",
                "stage": envelope["stage"],
                "retry_fingerprint": _digest(envelope),
                "error_fingerprint": _digest(found),
            },
        )
    evidence["fingerprint"] = _digest(evidence)
    evidence["observed_at"] = datetime.now(timezone.utc).isoformat()
    evidence["api_calls"] = http.calls
    return evidence


async def read_celigo_error_evidence(db, tenant_id, target_step_id, order_reference, *, error_id=None, client=None):
    if not isinstance(order_reference, str) or not _REFERENCE.fullmatch(order_reference) or len(order_reference) > 100:
        _fail("invalid_order_reference")
    if error_id is not None:
        _opaque(error_id)
    async with asyncio.timeout(_READ_SECONDS):
        step, flow, connection, token, region = await _load_scope(db, tenant_id, target_step_id)
        if client is not None:
            return await _read(_Transport(client, token, region), step, flow, connection, order_reference, error_id)
        async with httpx.AsyncClient() as owned:
            return await _read(_Transport(owned, token, region), step, flow, connection, order_reference, error_id)


async def dispatch_celigo_resolution(db, tenant_id, claimed: ClaimedOperation, fresh_evidence, *, client=None):
    """Reserve once, send one exact resolve PUT, return an unverified receipt."""
    if claimed.action != "resolve_celigo_error" or not claimed.target_record_id or claimed.record_type != "salesorder":
        _fail("unsupported_resolution_intent")
    proposal = await state_service.get_proposal(db, tenant_id, claimed.proposal_id)
    config = await state_service.get_config(db, tenant_id, claimed.config_id)
    if (
        proposal.tenant_id != tenant_id
        or config.tenant_id != tenant_id
        or proposal.config_id != claimed.config_id
        or proposal.status != "approved"
        or not config.enabled
        or not config.target_step_id
        or datetime.now(timezone.utc) >= proposal.valid_until
    ):
        _fail("approval_unavailable")
    approved = (proposal.evidence_json or {}).get("celigo")
    if (
        not isinstance(approved, dict)
        or not isinstance(fresh_evidence, dict)
        or approved.get("fingerprint") != fresh_evidence.get("fingerprint")
    ):
        _fail("provider_evidence_changed")
    error_id = (approved.get("error") or {}).get("error_id")
    if claimed.before_json != {"celigo_error_id": error_id, "celigo_error_state": "open"} or claimed.after_json != {
        "celigo_error_id": error_id,
        "celigo_error_state": "resolved",
    }:
        _fail("unsupported_resolution_intent")
    owns = client is None
    client = client or httpx.AsyncClient()
    try:
        live = await read_celigo_error_evidence(
            db, tenant_id, config.target_step_id, proposal.order_reference, error_id=error_id, client=client
        )
        if not live["complete"] or live["fingerprint"] != approved.get("fingerprint"):
            _fail("provider_evidence_changed")
        scope, error = live["scope"], live["error"]
        if (
            scope["account_id"] != _account(claimed.netsuite_account_id)
            or scope["subsidiary_id"] != claimed.subsidiary_id
            or scope["record_type"] != claimed.record_type
            or error["kind"] != "duplicate_transaction"
        ):
            _fail("resolution_scope_unproven")
        path = f"/v1/flows/{_remote(scope['flow_id'])}/{_remote(scope['import_id'])}/resolved"
        body = {"errors": [_opaque(error["error_id"])]}
        fingerprint = _digest(
            {"method": "PUT", "path": path, "body": body, "provider_fingerprint": live["fingerprint"]}
        )
        # Final credential/scope load occurs before the durable reservation.
        _, _, _, token, region = await _load_scope(db, tenant_id, config.target_step_id)
        reserved = await state_service.reserve_operation_dispatch(
            db, tenant_id, claimed, provider="celigo", payload_fingerprint=fingerprint
        )
        if not reserved:
            _fail("dispatch_already_reserved")
        try:
            status, _ = await _Transport(client, token, region).request("PUT", path, body=body)
        except (CeligoActionError, TimeoutError):
            return {"status": "unknown", "verified": False, "payload_fingerprint": fingerprint}
        # Any non-204 result is conservatively uncertain. The caller must use
        # read-only reconciliation; no failure status permits an automatic retry.
        return {
            "status": "accepted" if status == 204 else "unknown",
            "verified": False,
            "http_status": status,
            "payload_fingerprint": fingerprint,
        }
    finally:
        if owns:
            await client.aclose()
