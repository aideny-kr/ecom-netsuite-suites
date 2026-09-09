"""Group every open case by scoped symptoms, without asserting a shared cause.

SQL aggregates before pagination, so groups include cases beyond the current UI
page. Group IDs are selectors, never authorization or frozen approval identities.
"""

import re

from sqlalchemy import String, case, cast, func, select

from app.core.database import set_tenant_context
from app.models.transaction_ops import TransactionCase as Case
from app.services.transaction_ops.state_service import StateError

METRICS = ("order_total", "tax", "refunds")
USAGE = (
    "Groups share an observed issue pattern, not a verified root cause. Refresh each case, "
    "verify a shared cause and prepare exact supported proposals before bulk human approval. "
    "Each execution, approver and independent verification must retain its own audit record. "
    "Group membership can change; approval must bind exact proposals, never a group ID."
)


def _signature():
    report = Case.latest_report_json
    balance = report["balance"]
    columns = [
        Case.scope_json.label("scope"),
        balance["status"].astext.label("status"),
        balance["currency"].astext.label("currency"),
        balance["target_currency"].astext.label("target_currency"),
        balance["missing_metrics"].label("missing_metrics"),
        report["targets"][0]["status"].astext.label("target_state"),
    ]
    for metric in METRICS:
        value = balance["amounts"][metric]["delta"]
        text = value.astext
        # No float conversion or unsafe cast on malformed stored evidence.
        valid = (func.jsonb_typeof(value) == "string") & text.op("~")(
            r"^-?[0-9]{1,24}(\.[0-9]{1,12})?([eE][+-]?[0-9]{1,2})?$"
        )
        zero = text.op("~")(r"^-?0+(\.0+)?([eE][+-]?[0-9]{1,2})?$")
        columns.append(
            case(
                (valid & zero, "zero"),
                (valid & text.startswith("-"), "negative"),
                (valid, "positive"),
                else_="unknown",
            ).label(metric)
        )
    for kind in ("tax_reversal", "credit_memo"):
        columns.append(func.coalesce(balance["adjustments"].contains([{"kind": kind}]), False).label(kind))
    # MD5 is a compact non-security locator. Explicit tenant predicates and RLS
    # enforce access, including when callers supply another tenant's group ID.
    identifier = func.md5(cast(func.jsonb_build_array(Case.tenant_id, *columns), String)).label("group_id")
    return columns, identifier


def _pattern(row):
    status = row["status"]
    if status == "missing_in_netsuite":
        return "Missing orders"
    if status in {"ambiguous", "currency_mismatch"}:
        return "Order identity or currency requires review"
    if (
        status not in {"difference", "mismatch"}
        or row["missing_metrics"]
        or not row["currency"]
        or row["currency"] != row["target_currency"]
        or any(row[key] == "unknown" for key in METRICS)
    ):
        return "Incomplete comparison evidence"
    changed = [label for key, label in zip(METRICS, ("Order", "Tax", "Refund")) if row[key] != "zero"]
    if not changed:
        return "Recheck required"
    suffix = " after credits" if row["tax_reversal"] or row["credit_memo"] else ""
    return " + ".join(changed) + " differences" + suffix


async def list_groups(db, tenant_id, *, limit=50, offset=0):
    await set_tenant_context(db, str(tenant_id))
    columns, identifier = _signature()
    grouped = (
        select(
            *columns,
            identifier,
            func.count().label("case_count"),
            func.max(Case.last_observed_at).label("last_observed_at"),
        )
        .where(Case.tenant_id == tenant_id, Case.status == "open")
        .group_by(Case.tenant_id, *columns)
        .subquery()
    )
    limit = min(50, max(1, limit))
    rows = (
        (
            await db.execute(
                select(grouped)
                .order_by(grouped.c.case_count.desc(), grouped.c.group_id)
                .offset(max(0, offset))
                .limit(limit + 1)
            )
        )
        .mappings()
        .all()
    )
    groups = []
    for row in rows[:limit]:
        groups.append(
            {
                **dict(row),
                "pattern": _pattern(row),
                "cause_verified": False,
                "last_observed_at": row["last_observed_at"].isoformat(),
            }
        )
    return {"groups": groups, "has_next": len(rows) > limit, "offset": offset, "usage": USAGE}


async def group_members(db, tenant_id, group_id, *, limit=50, offset=0):
    if not isinstance(group_id, str) or not re.fullmatch(r"[0-9a-f]{32}", group_id):
        raise StateError("invalid_group_id", 422)
    await set_tenant_context(db, str(tenant_id))
    _, identifier = _signature()
    limit = min(50, max(1, limit))
    rows = (
        await db.execute(
            select(Case.id, Case.order_reference, Case.last_observed_at)
            .where(Case.tenant_id == tenant_id, Case.status == "open", identifier == group_id)
            .order_by(Case.id)
            .offset(max(0, offset))
            .limit(limit + 1)
        )
    ).all()
    return {
        "group_id": group_id,
        "cases": [
            {
                "case_id": str(row.id),
                "order_reference": row.order_reference,
                "last_observed_at": row.last_observed_at.isoformat(),
            }
            for row in rows[:limit]
        ],
        "has_next": len(rows) > limit,
        "offset": offset,
        "usage": USAGE,
    }
