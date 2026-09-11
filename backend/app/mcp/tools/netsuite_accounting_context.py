"""Read current accounting reference facts on one authorized NetSuite connection.

Reference facts are not accounting policy, balances, or permission to post. Nothing
is persisted as tenant knowledge: every call rechecks the connection and its scope.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select

from app.core.database import set_tenant_context
from app.core.dependencies import has_permission
from app.mcp.tools import netsuite_suiteql
from app.models.connection import Connection
from app.models.tenant import Tenant
from app.models.user import User


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: Literal["overview", "periods", "accounts", "policies"] = "overview"
    connection_id: uuid.UUID | None = None
    expected_account_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,255}$")
    calendar_year: int | None = Field(default=None, ge=1900, le=2199, strict=True)
    account_id: int | None = Field(default=None, gt=0, le=10**15, strict=True)
    limit: int = Field(default=100, ge=1, le=500, strict=True)

    @model_validator(mode="after")
    def validate_scope(self):
        if (self.connection_id is None) != (self.expected_account_id is None):
            raise ValueError("connection_id and expected_account_id must be supplied together")
        if self.account_id is not None and self.section != "accounts":
            raise ValueError("account_id is only valid for accounts")
        if self.calendar_year is not None and self.section != "periods":
            raise ValueError("calendar_year is only valid for periods")
        return self


def _account(value):
    return value.replace("_", "-").lower() if isinstance(value, str) else ""


async def _authorize(context):
    db = context.get("db")
    if db is None:
        return None
    try:
        tenant_id = uuid.UUID(str(context.get("tenant_id")))
        actor_id = uuid.UUID(str(context.get("actor_id")))
    except (ValueError, TypeError):
        return None
    await set_tenant_context(db, tenant_id)
    actor = await db.scalar(
        select(User.id)
        .join(Tenant, Tenant.id == User.tenant_id)
        .where(
            User.id == actor_id,
            User.tenant_id == tenant_id,
            User.is_active.is_(True),
            User.actor_type == "user",
            Tenant.is_active.is_(True),
        )
    )
    if actor is None or not await has_permission(db, actor_id, "connections.view"):
        return None
    return db, tenant_id, actor_id


async def _reference(query, scope, context, limit):
    result = await netsuite_suiteql.execute({"query": query, "limit": limit, "timeout_seconds": 30, **scope}, context)
    verified = {"connection_id": scope["connection_id"], "account_id": scope["expected_account_id"]}
    if not isinstance(result, dict) or result.get("error") or result.get("verified_connection_scope") != verified:
        return {"status": "unavailable", "reason": "reference_query_failed_or_scope_unverified"}
    columns, rows = result.get("columns"), result.get("rows")
    if (
        not isinstance(columns, list)
        or not all(isinstance(c, str) for c in columns)
        or len(set(columns)) != len(columns)
        or not isinstance(rows, list)
        or any(not isinstance(row, (list, tuple)) or len(row) != len(columns) for row in rows)
        or result.get("row_count") != len(rows)
    ):
        return {"status": "unavailable", "reason": "invalid_reference_result"}
    # FETCH FIRST can hide the underlying totalResults. Equality with the cap
    # is therefore inconclusive even if the transport says truncated=False.
    result_limit = result.get("limit", limit)
    if type(result_limit) is not int or result_limit <= 0:
        return {"status": "unavailable", "reason": "invalid_reference_result"}
    cap = min(limit, result_limit)
    complete = not result.get("truncated", False) and len(rows) < cap
    return {
        "status": "complete" if complete else "partial",
        "rows": [dict(zip(columns, row)) for row in rows],
        "coverage": "Records visible to this connection's role; an empty list is not proof of account-wide absence.",
    }


async def _policies(context, connection_id, account_id):
    from app.mcp.tools.transaction_ops_tools import _authorize as authorize_transactions
    from app.mcp.tools.transaction_ops_tools import _ToolError
    from app.models.transaction_ops import TransactionConfig
    from app.services.transaction_ops.accounting_profiles import sales_credit_profile

    try:
        db, tenant_id, _ = await authorize_transactions(context, create=False)
    except _ToolError:
        return {"status": "unavailable", "reason": "accounting_workflow_not_available"}
    configs = (
        await db.scalars(
            select(TransactionConfig)
            .where(
                TransactionConfig.tenant_id == tenant_id,
                TransactionConfig.netsuite_connection_id == connection_id,
                TransactionConfig.enabled.is_(True),
            )
            .order_by(TransactionConfig.id)
            .limit(101)
            .execution_options(populate_existing=True)
        )
    ).all()
    treatments = []
    for config in configs[:100]:
        if _account(config.netsuite_account_id) != account_id:
            continue
        try:
            profile = await sales_credit_profile(db, tenant_id, config)
            status = "configured" if profile else "not_configured"
        except (ValueError, TypeError):
            profile, status = None, "invalid_configuration"
        treatments.append(
            {
                "config_id": str(config.id),
                "name": config.name,
                "subsidiary_id": config.subsidiary_id,
                "sales_credit_status": status,
                "sales_credit_profile": profile,
            }
        )
    # The profile helper reads fresh connection state. A concurrent revocation
    # must be reported as unavailable, not as a missing business treatment.
    current = (
        await db.execute(
            select(Connection.metadata_json, Connection.status).where(
                Connection.id == connection_id,
                Connection.tenant_id == tenant_id,
                Connection.provider == "netsuite",
            )
        )
    ).one_or_none()
    if (
        current is None
        or current.status != "active"
        or _account((current.metadata_json or {}).get("account_id")) != account_id
    ):
        return {"status": "unavailable", "reason": "connection_scope_unavailable"}
    return {
        "status": "partial" if len(configs) > 100 else "complete",
        "configured_treatments": treatments,
        "limitations": (
            "These are configured supported corrections, not a complete accounting policy manual or approval. "
            "Missing configuration does not establish a policy prohibition. Never reuse a treatment across "
            "accounts, subsidiaries, currencies or books. Revalidate native case evidence before any proposal."
        ),
    }


async def execute(params: dict, context: dict | None = None, **kwargs) -> dict:
    auth = await _authorize(context or {})
    if auth is None:
        return {"success": False, "error": "permission_denied"}
    try:
        request = ContextRequest.model_validate(params)
    except ValidationError:
        return {"success": False, "error": "invalid_parameters"}
    db, tenant_id, actor_id = auth
    query = select(Connection).where(
        Connection.tenant_id == tenant_id, Connection.provider == "netsuite", Connection.status == "active"
    )
    if request.connection_id is not None:
        query = query.where(Connection.id == request.connection_id)
    connections = (await db.scalars(query.execution_options(populate_existing=True))).all()
    if len(connections) != 1:
        return {
            "success": False,
            "error": "connection_scope_required" if connections else "connection_unavailable",
            "connections": [
                {"connection_id": str(c.id), "label": c.label, "account_id": (c.metadata_json or {}).get("account_id")}
                for c in connections
            ],
        }
    connection = connections[0]
    account_id = _account((connection.metadata_json or {}).get("account_id"))
    if not re.fullmatch(r"[a-z0-9-]{1,255}", account_id) or (
        request.expected_account_id is not None and _account(request.expected_account_id) != account_id
    ):
        return {"success": False, "error": "connection_scope_mismatch"}
    scope = {"connection_id": str(connection.id), "expected_account_id": account_id}
    call_context = {**(context or {}), "db": db, "tenant_id": tenant_id, "actor_id": actor_id}
    sections = {}
    if request.section == "overview":
        sections["subsidiaries"] = await _reference(
            "SELECT s.id, s.name, s.parent, s.currency, c.symbol AS currency_code, "
            "s.fiscalcalendar, BUILTIN.DF(s.fiscalcalendar) AS fiscal_calendar_name, "
            "s.iselimination, s.isinactive FROM subsidiary s JOIN currency c ON c.id = s.currency ORDER BY s.id",
            scope,
            call_context,
            request.limit,
        )
        sections["books"] = await _reference(
            "SELECT id, name, isprimary, isconsolidated, status FROM accountingbook ORDER BY id",
            scope,
            call_context,
            request.limit,
        )
    elif request.section == "periods":
        year = request.calendar_year or datetime.now(timezone.utc).year
        sections["periods"] = await _reference(
            "SELECT id, periodname, TO_CHAR(startdate, 'YYYY-MM-DD') AS start_date, "
            "TO_CHAR(enddate, 'YYYY-MM-DD') AS end_date, isyear, isquarter, isposting, isadjust, "
            "closed, alllocked, aplocked, arlocked FROM accountingperiod "
            f"WHERE startdate < TO_DATE('{year + 1}-01-01', 'YYYY-MM-DD') "
            f"AND enddate >= TO_DATE('{year}-01-01', 'YYYY-MM-DD') ORDER BY startdate, id",
            scope,
            call_context,
            request.limit,
        )
        sections["calendar_year_window"] = year
    elif request.section == "accounts":
        where = f" WHERE id = {request.account_id}" if request.account_id is not None else ""
        sections["accounts"] = await _reference(
            "SELECT id, acctnumber, fullname, accttype, currency, subsidiary, isinactive, issummary, generalrate "
            f"FROM account{where} ORDER BY id",
            scope,
            call_context,
            request.limit,
        )
    else:
        sections["policies"] = await _policies(call_context, connection.id, account_id)
    available = any(
        isinstance(section, dict) and section.get("status") in {"complete", "partial"} for section in sections.values()
    )
    return {
        "success": available,
        **({} if available else {"error": "accounting_reference_unavailable"}),
        "kind": "accounting_reference",
        "scope": {"connection_id": str(connection.id), "account_id": account_id, "connection_label": connection.label},
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "sections": sections,
        "limitations": (
            "Current reference data, not financial balances or permission to post. Closed and locked are "
            "separate states; recheck the exact subsidiary/book/period before a correction. A calendar-year "
            "selection is not a fiscal-year definition. Use only verified, complete sections for exhaustive "
            "claims. Company policies such as revenue recognition, materiality and tax treatment require "
            "their own approved sources. No financial writes or tenant knowledge updates were performed."
        ),
    }
