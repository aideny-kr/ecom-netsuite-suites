"""Bounded Framework reads using the tenant's saved Celigo HTTP connection.

The Framework credential stays in Celigo. Each operation spends at most two
Celigo requests: verify the live saved connection, then preview a fresh, fixed
GET definition. Never reuse an export's scripts, hooks, mappings or delta state.
No request/response body, headers or upstream exception text leaves this module.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from urllib.parse import quote

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials
from app.models.celigo import CeligoFlow, CeligoFlowStep
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.services.celigo.client import CELIGO_BASE_URLS
from app.services.transaction_ops.source_projection import ProjectionError, project_order

_FRAMEWORK_BASE = "https://private-direct-access.frame.work/api/"
_ORDER_REFERENCE = re.compile(r"R[0-9]{9}(?:-[A-Z0-9]+)?\Z")
_REMOTE_ID = re.compile(r"[a-f0-9]{24}\Z")
_MAX_BYTES = 2 * 1024 * 1024
_MAX_CALLS = 2
_OPERATION_TIMEOUT = 40
_HTTP_TIMEOUT = httpx.Timeout(connect=5, read=20, write=10, pool=5)
_INCOMPLETE_MARKERS = (
    "truncated",
    "isTruncated",
    "sampled",
    "isSampled",
    "hasMoreData",
    "dataLimitReached",
    "limitReached",
)


class SourceReadError(Exception):
    """Only fixed machine codes are exposed; never include upstream exception text."""

    def __init__(self, code: str, http_status: int = 502):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _check_envelope(body: dict, *, nullable_errors: bool = False) -> None:
    if not isinstance(body, dict):
        raise SourceReadError("invalid_source_response")
    if "errors" in body and body["errors"] != [] and not (nullable_errors and body["errors"] is None):
        raise SourceReadError("source_reported_errors")
    if any(body.get(marker) not in (None, False, 0) for marker in _INCOMPLETE_MARKERS):
        raise SourceReadError("incomplete_preview")


def _json_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate_json_key")
        obj[key] = value
    return obj


def _invalid_constant(_):
    raise ValueError("invalid_json_number")


class _Transport:
    def __init__(self, client: httpx.AsyncClient, token: str, region: str):
        self.client = client
        self.token = token
        self.base = CELIGO_BASE_URLS[region]
        self.calls = 0

    async def request(self, method: str, path: str, *, body: dict | None = None) -> dict:
        if self.calls >= _MAX_CALLS:
            raise SourceReadError("source_api_budget_exhausted")
        self.calls += 1
        async with self.client.stream(
            method,
            f"{self.base}{path}",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            json=body,
            timeout=_HTTP_TIMEOUT,
            follow_redirects=False,
        ) as response:
            if response.status_code == 429:
                raise SourceReadError("source_rate_limited", 429)
            if response.status_code in (401, 403):
                raise SourceReadError("source_authentication_failed")
            if response.status_code != 200:
                raise SourceReadError("source_http_error")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > _MAX_BYTES:
                    raise SourceReadError("response_too_large")
                data.extend(chunk)
        try:
            result = json.loads(
                data, parse_float=Decimal, parse_constant=_invalid_constant, object_pairs_hook=_json_pairs
            )
        except (ValueError, UnicodeError, RecursionError, DecimalException):
            raise SourceReadError("invalid_source_response") from None
        _check_envelope(result)
        return result


async def _load_source(db: AsyncSession, tenant_id: uuid.UUID, step_id: uuid.UUID):
    result = await db.execute(
        select(CeligoFlowStep, Connection)
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
        .where(CeligoFlowStep.id == step_id, CeligoFlowStep.tenant_id == tenant_id)
    )
    pair = result.one_or_none()
    if pair is None:
        raise SourceReadError("source_not_found", 404)
    step, connection = pair
    if not isinstance(step.connection_celigo_id, str) or not _REMOTE_ID.fullmatch(step.connection_celigo_id):
        raise SourceReadError("unsupported_source", 422)
    region = (connection.metadata_json or {}).get("region", "us")
    if region not in CELIGO_BASE_URLS:
        raise SourceReadError("unsupported_source_region", 422)
    try:
        credentials = decrypt_credentials(connection.encrypted_credentials)
        token = credentials.get("token")
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise SourceReadError("source_credentials_unavailable") from None
    if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
        raise SourceReadError("source_credentials_unavailable")
    return step, connection, token, region


async def _preview(db, tenant_id, step_id, relative_uri, *, client):
    step, connection, token, region = await _load_source(db, tenant_id, step_id)
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=False)
    transport = _Transport(http, token, region)
    try:
        async with asyncio.timeout(_OPERATION_TIMEOUT):
            live = await transport.request("GET", f"/v1/connections/{step.connection_celigo_id}")
            live_http = live.get("http")
            if (
                live.get("_id") != step.connection_celigo_id
                or live.get("type") != "http"
                or not isinstance(live_http, dict)
                or live_http.get("baseURI") != _FRAMEWORK_BASE
            ):
                raise SourceReadError("unsupported_source", 422)
            result = await transport.request(
                "POST",
                "/v1/exports/preview",
                body={
                    "name": "Transaction evidence read",
                    "_connectionId": step.connection_celigo_id,
                    "type": "test",
                    "test": {"limit": 1},
                    "http": {"method": "GET", "relativeURI": relative_uri, "followRedirects": False},
                },
            )
    except (httpx.HTTPError, TimeoutError):
        raise SourceReadError("source_transport_failed") from None
    finally:
        if owns_client:
            await http.aclose()
    stages = result.get("stages", [])
    if not isinstance(stages, list):
        raise SourceReadError("invalid_source_response")
    for stage in stages:
        # ExportPreviewResponse documents null for a clean stage. Top-level
        # response errors and source-record errors remain list-only.
        _check_envelope(stage, nullable_errors=True)
    data = result.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise SourceReadError("incomplete_preview")
    _check_envelope(data[0])
    provenance = {
        "source": "framework",
        "celigo_step_id": str(step.id),
        "connection_id": str(connection.id),
        "read_at": datetime.now(timezone.utc).isoformat(),
    }
    return data[0], provenance


def _project_orders(orders):
    try:
        return [project_order(order) for order in orders]
    except ProjectionError:
        raise SourceReadError("invalid_business_evidence") from None


async def read_framework_order(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    step_id: uuid.UUID,
    order_reference: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Read one exact order; a missing/error response never proves absence."""
    if (
        not isinstance(order_reference, str)
        or len(order_reference) > 100
        or not _ORDER_REFERENCE.fullmatch(order_reference)
    ):
        raise SourceReadError("invalid_order_reference", 422)
    order, provenance = await _preview(db, tenant_id, step_id, f"orders/{order_reference}", client=client)
    if order.get("number") != order_reference:
        raise SourceReadError("order_identity_mismatch")
    return {
        **provenance,
        "scope": "order",
        "orders": _project_orders([order]),
        "page_complete": True,
        "window_complete": False,
        "next_page": None,
    }


async def read_framework_orders_page(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    step_id: uuid.UUID,
    updated_since: datetime,
    page: int = 1,
    page_size: int = 20,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """One bounded page. Only a single-page window can be complete in one call.

    Page-based source reads are not an immutable snapshot: callers traversing a
    changing window must handle records moving between pages before proving absence.
    """
    if (
        type(page) is not int
        or not 1 <= page <= 100000
        or type(page_size) is not int
        or not 1 <= page_size <= 20
        or not isinstance(updated_since, datetime)
        or updated_since.utcoffset() is None
    ):
        raise SourceReadError("invalid_page_request", 422)
    since = updated_since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    uri = (
        f"sync/orders?q[updated_at_gteq]={quote(since, safe='')}&q[completed_at_not_null]=1"
        f"&page={page}&per_page={page_size}&q[s]=id"
    )
    envelope, provenance = await _preview(db, tenant_id, step_id, uri, client=client)
    orders = envelope.get("orders")
    values = {key: envelope.get(key) for key in ("current_page", "pages", "per_page", "total_count", "count")}
    if any(type(value) is not int or value < 0 for value in values.values()) or not isinstance(orders, list):
        raise SourceReadError("incomplete_page")
    total, pages = values["total_count"], values["pages"]
    expected_pages = (total + page_size - 1) // page_size
    expected_count = min(page_size, max(0, total - (page - 1) * page_size))
    if (
        values["current_page"] != page
        or values["per_page"] != page_size
        or pages not in ({0, 1} if total == 0 else {expected_pages})
        or page > max(1, pages)
        or len(orders) != expected_count
        or values["count"] != expected_count
    ):
        raise SourceReadError("incomplete_page")
    references = set()
    for order in orders:
        reference = order.get("number") if isinstance(order, dict) else None
        if not isinstance(reference, str) or not _ORDER_REFERENCE.fullmatch(reference) or reference in references:
            raise SourceReadError("incomplete_page")
        _check_envelope(order)
        references.add(reference)
    return {
        **provenance,
        "scope": "updated_orders",
        "updated_since": since,
        "orders": _project_orders(orders),
        "page": page,
        "page_size": page_size,
        "total_count": total,
        "pages": pages,
        "page_complete": True,
        "window_complete": page == 1 and pages <= 1,
        "next_page": page + 1 if page < pages else None,
    }
