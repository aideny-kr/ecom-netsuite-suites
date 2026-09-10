"""Bounded, tenant-scoped NetSuite sales-order evidence; never customer-data writes.

SuiteQL establishes exact identity across every date/status, then the record API
provides transaction-currency amounts. Currency and period reads supply explicit
metadata, not inferred ISO codes or guesses about posting eligibility. OAuth may
refresh the existing app connection through its ordinary credential helper.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_context
from app.core.encryption import decrypt_credentials
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.services.netsuite_oauth_service import get_valid_token

MAX_RESPONSE_BYTES = 2_000_000
MAX_API_CALLS = 7  # identity + (record, currency, period) for at most two orders
READ_TIMEOUT_SECONDS = 120
_TIMEOUT = httpx.Timeout(connect=5, read=25, write=10, pool=5)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_ACCOUNT = re.compile(r"[0-9]+(?:[-_](?:SB[0-9]+|RP))?", re.IGNORECASE)
_RECORD_ID = re.compile(r"[0-9]{1,30}")

HEADER_FIELDS = frozenset(
    {
        "id",
        "tranId",
        "tranDate",
        "lastModifiedDate",
        "createdDate",
        "status",
        "orderStatus",
        "currency",
        "entity",
        "exchangeRate",
        "subsidiary",
        "subtotal",
        "taxTotal",
        "total",
        "shippingCost",
        "handlingCost",
        "discountTotal",
        "discountRate",
        "discountItem",
        "postingPeriod",
        "taxDetailsOverride",
        "taxRegOverride",
        "taxItem",
        "taxRate",
        "isTaxable",
        "custbody_fw_solidus_order_total",
        "custbody_fw_solidus_tax_amount",
        "shippingTax1Rate",
        "shippingTax2Rate",
    }
)
LINE_FIELDS = frozenset(
    {
        "line",
        "lineUniqueKey",
        "item",
        "quantity",
        "quantityBilled",
        "quantityFulfilled",
        "quantityCommitted",
        "rate",
        "amount",
        "grossAmt",
        "tax1Amt",
        "taxAmount",
        "taxRate1",
        "taxRate2",
        "taxCode",
        "taxDetailsReference",
        "isClosed",
        "isTaxable",
        "isDiscount",
        "itemType",
        "custcol_fw_solidus_line_id",
        "custcol_fw_inventory_unit_ids",
        "custcol_fw_line_parent_id",
        "custcol_fw_item_sku",
        "custcol_fw_original_ecom_sku",
        "custcol_fw_vat_amount",
        "custcol_fw_item_rate",
        "custcol_fw_line_rollup_amount",
    }
)
TAX_FIELDS = frozenset(
    {
        "line",
        "taxDetailsReference",
        "taxType",
        "taxCode",
        "taxBasis",
        "taxRate",
        "taxAmount",
        "netAmount",
        "grossAmount",
    }
)
CURRENCY_FIELDS = frozenset({"id", "symbol", "name", "currencyPrecision", "decimalPrecision", "isInactive"})
PERIOD_FIELDS = frozenset(
    {"id", "periodname", "closed", "alllocked", "arlocked", "aplocked", "isadjust", "startdate", "enddate"}
)


class NetSuiteEvidenceError(ValueError):
    """Safe error code only: upstream bodies, references and credentials never escape."""


def _account(value: Any) -> str:
    if not isinstance(value, str) or not _ACCOUNT.fullmatch(value):
        raise NetSuiteEvidenceError("invalid_account")
    return value.replace("_", "-").lower()


def _uuid(value: Any, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise NetSuiteEvidenceError(f"invalid_{label}") from None


def _id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    return text if _RECORD_ID.fullmatch(text) else None


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("non_finite_number")


def _project(source: dict, fields: frozenset[str]) -> dict:
    """Keep enumerated business fields; references retain only ID and display name."""
    result = {}
    for key in fields & source.keys():
        value = source[key]
        if isinstance(value, dict):
            keys = {"id"} if key == "entity" else {"id", "refName"}
            result[key] = {
                k: v for k, v in value.items() if k in keys and isinstance(v, (str, int)) and not isinstance(v, bool)
            }
        elif value is None or isinstance(value, (str, int, Decimal, bool)):
            result[key] = value
    return result


def _collection(body: dict) -> tuple[list[dict], bool]:
    items = body.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise NetSuiteEvidenceError("invalid_collection")
    count, total, more = body.get("count"), body.get("totalResults"), body.get("hasMore")
    complete = (
        type(count) is int
        and count == len(items)
        and type(total) is int
        and total == len(items)
        and more is False
        and not _has_next(body)
    )
    return items, complete


def _has_next(body: dict) -> bool:
    links = body.get("links", [])
    return isinstance(links, list) and any(isinstance(link, dict) and link.get("rel") == "next" for link in links)


def _sublist(record: dict, key: str, label: str, fields: frozenset[str], problems: list[str]) -> list[dict] | None:
    value = record.get(key)
    if key not in record and key == "taxDetails":
        return None  # Legacy tax accounts need not expose SuiteTax details; absence is not zero tax.
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        problems.append(f"{label}_not_expanded")
        return None
    items = value["items"]
    if any(not isinstance(item, dict) for item in items):
        problems.append(f"{label}_invalid")
        return None
    # Expanded record sublists ordinarily have items + links, no paging metadata.
    # If the server supplies pagination metadata it must agree with the expansion.
    if (
        _has_next(value)
        or ("hasMore" in value and value["hasMore"] is not False)
        or any(
            key in value and (type(value[key]) is not int or value[key] != len(items))
            for key in ("count", "totalResults")
        )
    ):
        problems.append(f"{label}_incomplete")
    return [_project(item, fields) for item in items]


class _Reader:
    def __init__(self, client: httpx.AsyncClient, base: str, token: str, *, max_api_calls=None):
        if max_api_calls is None:
            max_api_calls = MAX_API_CALLS
        if type(max_api_calls) is not int or not 1 <= max_api_calls <= 32:
            raise NetSuiteEvidenceError("invalid_read_budget")
        self.max_api_calls = max_api_calls
        self.client, self.base = client, base
        self.headers = {"Authorization": f"Bearer {token}", "Prefer": "transient"}
        self.calls = 0
        self.currencies: dict[str, dict] = {}
        self.periods: dict[str, dict] = {}

    async def request(self, method: str, path: str, *, params=None, body=None) -> dict:
        if self.calls >= self.max_api_calls:
            raise NetSuiteEvidenceError("api_call_budget")
        self.calls += 1
        try:
            async with self.client.stream(
                method,
                self.base + path,
                headers=self.headers,
                params=params,
                json=body,
                timeout=_TIMEOUT,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise NetSuiteEvidenceError(f"upstream_http_{response.status_code}")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise NetSuiteEvidenceError("response_budget")
                    content.extend(chunk)
                parsed = json.loads(
                    content, parse_float=Decimal, parse_constant=_invalid_constant, object_pairs_hook=_object
                )
        except NetSuiteEvidenceError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, RecursionError, UnicodeError, DecimalException):
            raise NetSuiteEvidenceError("invalid_upstream_response") from None
        if not isinstance(parsed, dict) or parsed.get("error") or parsed.get("o:errorDetails"):
            raise NetSuiteEvidenceError("invalid_upstream_response")
        return parsed

    async def currency(self, currency_id: str) -> dict:
        if currency_id not in self.currencies:
            raw = await self.request("GET", f"/record/v1/currency/{currency_id}")
            projected = _project(raw, CURRENCY_FIELDS)
            if _id(raw.get("id")) != currency_id:
                raise NetSuiteEvidenceError("currency_identity_mismatch")
            self.currencies[currency_id] = projected
        return self.currencies[currency_id]

    async def period(self, tran_date: Any) -> dict:
        try:
            if not isinstance(tran_date, str) or date.fromisoformat(tran_date).isoformat() != tran_date:
                raise ValueError
        except ValueError:
            return {"complete": False, "items": [], "reason": "invalid_transaction_date"}
        if tran_date not in self.periods:
            query = (
                "SELECT id, periodname, closed, alllocked, arlocked, aplocked, isadjust, "
                "TO_CHAR(startdate, 'YYYY-MM-DD') AS startdate, TO_CHAR(enddate, 'YYYY-MM-DD') AS enddate "
                f"FROM accountingperiod WHERE startdate <= TO_DATE('{tran_date}', 'YYYY-MM-DD') "
                f"AND enddate >= TO_DATE('{tran_date}', 'YYYY-MM-DD') AND isquarter = 'F' AND isyear = 'F'"
            )
            raw = await self.request("POST", "/query/v1/suiteql", params={"limit": 10, "offset": 0}, body={"q": query})
            items, complete = _collection(raw)
            self.periods[tran_date] = {"complete": complete, "items": [_project(row, PERIOD_FIELDS) for row in items]}
        return self.periods[tran_date]

    async def read(self, *, order_reference: str, reference_field: str, subsidiary_id: str) -> dict:
        query = (
            f"SELECT t.id, t.tranid, t.type, t.{reference_field} AS order_reference "
            f"FROM transaction t WHERE t.type = 'SalesOrd' AND t.{reference_field} = "
            "'" + order_reference.replace("'", "''") + "' ORDER BY t.id"
        )
        raw = await self.request("POST", "/query/v1/suiteql", params={"limit": 2, "offset": 0}, body={"q": query})
        matches, complete = _collection(raw)
        if len(matches) > 2:
            raise NetSuiteEvidenceError("identity_result_budget")
        orders = []
        seen = set()
        for match in matches:
            record_id = _id(match.get("id"))
            if (
                not record_id
                or record_id in seen
                or match.get("type") != "SalesOrd"
                or match.get("order_reference") != order_reference
            ):
                raise NetSuiteEvidenceError("invalid_identity_result")
            seen.add(record_id)
            record = await self.request(
                "GET", f"/record/v1/salesOrder/{record_id}", params={"expandSubResources": "true"}
            )
            problems: list[str] = []
            if _id(record.get("id")) != record_id:
                problems.append("record_identity_mismatch")
            header = _project(record, HEADER_FIELDS)
            # The order reference can change between the SuiteQL identity read
            # and this GET. Verify against the record we actually observed, and
            # retain the configured identity field even when it is custom.
            reference_keys = [key for key in record if key.lower() == reference_field.lower()]
            if len(reference_keys) != 1 or record[reference_keys[0]] != order_reference:
                problems.append("record_reference_mismatch")
            if len(reference_keys) == 1:
                header.update(_project(record, frozenset(reference_keys)))
            actual_sub = header.get("subsidiary")
            if not isinstance(actual_sub, dict) or not _id(actual_sub.get("id")):
                problems.append("missing_subsidiary")
            elif str(actual_sub["id"]) != subsidiary_id:
                problems.append("subsidiary_mismatch")
            currency = header.get("currency")
            currency_id = _id(currency.get("id")) if isinstance(currency, dict) else None
            if not currency_id:
                problems.append("missing_currency")
            version = header.get("lastModifiedDate")
            if not isinstance(version, str) or not version:
                problems.append("missing_version")
            header_complete = not problems
            lines = _sublist(record, "item", "lines", LINE_FIELDS, problems)
            tax = _sublist(record, "taxDetails", "tax_details", TAX_FIELDS, problems)
            metadata = await self.currency(currency_id) if currency_id else None
            period = await self.period(header.get("tranDate"))
            if not period["complete"]:
                problems.append("period_lookup_incomplete")
            orders.append(
                {
                    "record_type": "salesOrder",
                    "record_id": record_id,
                    "order_reference": match["order_reference"],
                    "header": header,
                    "lines": lines,
                    "tax_details": tax,
                    "currency_metadata": metadata,
                    "periods": period,
                    "version": version,
                    "header_complete": header_complete,
                    "complete": not problems,
                    "completeness_errors": problems,
                }
            )
        return {
            "provider": "netsuite",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "complete": complete and all(order["complete"] for order in orders),
            "orders": orders,
            "lookup": {
                "count": len(matches),
                "total_results": raw.get("totalResults"),
                "has_more": raw.get("hasMore"),
                "complete": complete,
            },
            "api_calls": self.calls,
        }


@asynccontextmanager
async def authenticated_reader(db, tenant_id, connection_id, account_id, *, client=None, max_api_calls=None):
    """Share selected-connection authorization across fixed native read services."""
    tenant, connection_uuid = _uuid(tenant_id, "tenant"), _uuid(connection_id, "connection")
    account = _account(account_id)
    if max_api_calls is None:
        max_api_calls = MAX_API_CALLS
    if type(max_api_calls) is not int or not 1 <= max_api_calls <= 32:
        raise NetSuiteEvidenceError("invalid_read_budget")
    await set_tenant_context(db, str(tenant))
    connection = (
        await db.execute(
            select(Connection).where(
                Connection.id == connection_uuid,
                Connection.tenant_id == tenant,
                Connection.provider == "netsuite",
                Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
            )
        )
    ).scalar_one_or_none()
    if (
        connection is None
        or connection.id != connection_uuid
        or connection.tenant_id != tenant
        or connection.provider != "netsuite"
        or connection.status not in ACTIVE_CONNECTION_STATUSES
    ):
        raise NetSuiteEvidenceError("invalid_connection")
    try:
        credentials = decrypt_credentials(connection.encrypted_credentials)
        stored_account = _account(credentials.get("account_id"))
    except Exception:
        raise NetSuiteEvidenceError("invalid_connection_account") from None
    if stored_account != account:
        raise NetSuiteEvidenceError("account_mismatch")
    try:
        # Existing helper commits credential rotation itself. Do not commit the
        # caller's other work on every read; SET LOCAL needs restoring either way.
        token = await asyncio.wait_for(get_valid_token(db, connection), timeout=45)
    except Exception:
        raise NetSuiteEvidenceError("authentication_failed") from None
    finally:
        await set_tenant_context(db, str(tenant))
    if not token:
        raise NetSuiteEvidenceError("authentication_failed")
    base = f"https://{account}.suitetalk.api.netsuite.com/services/rest"

    if client is not None:
        yield _Reader(client, base, token, max_api_calls=max_api_calls)
    else:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as owned:
            yield _Reader(owned, base, token, max_api_calls=max_api_calls)


async def read_netsuite_order(
    db: AsyncSession,
    tenant_id,
    connection_id,
    account_id: str,
    subsidiary_id: str,
    order_reference: str,
    reference_field: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Return explicit provider evidence for an exact reference, at most two records.

    `reference_field` must come from the configured integration mapping, never a
    guessed custom field. Missing/duplicate/cross-subsidiary matches stay visible.
    Read completeness is distinct from permission to mutate a closed/billed order.
    """
    tenant, connection_uuid = _uuid(tenant_id, "tenant"), _uuid(connection_id, "connection")
    account = _account(account_id)
    if not _id(subsidiary_id):
        raise NetSuiteEvidenceError("invalid_subsidiary")
    if not isinstance(reference_field, str) or not _IDENTIFIER.fullmatch(reference_field):
        raise NetSuiteEvidenceError("invalid_reference_field")
    if (
        not isinstance(order_reference, str)
        or not order_reference
        or len(order_reference) > 255
        or any(ord(c) < 32 for c in order_reference)
    ):
        raise NetSuiteEvidenceError("invalid_order_reference")
    try:
        async with asyncio.timeout(READ_TIMEOUT_SECONDS):
            async with authenticated_reader(db, tenant, connection_uuid, account, client=client) as worker:
                result = await worker.read(
                    order_reference=order_reference, reference_field=reference_field, subsidiary_id=str(subsidiary_id)
                )
    except TimeoutError:
        raise NetSuiteEvidenceError("read_timeout") from None
    result["scope"] = {
        "connection_id": str(connection_uuid),
        "account_id": account,
        "subsidiary_id": str(subsidiary_id),
        "reference_field": reference_field,
    }
    return result
