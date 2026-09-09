"""Group every open case by scoped symptoms, without asserting a shared cause.

SQL aggregates before pagination, so groups include cases beyond the current UI
page. Group IDs are selectors, never authorization or frozen approval identities.
"""

import json
import re
from uuid import UUID

from sqlalchemy import String, case, cast, func, select, union_all

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


async def _source(db, tenant_id, review_run_ids=None, status=None, search=""):
    from app.services.transaction_ops.review_evidence import period_evidence, result_category

    if status not in (None, "matched", "needs_review", "not_verified"):
        raise StateError("invalid_result_status", 422)
    if not isinstance(search, str) or len(search) > 200:
        raise StateError("invalid_group_scope", 422)
    if review_run_ids is None:
        if status or search:
            raise StateError("review_scope_required", 422)
        return select(
            Case.id,
            Case.tenant_id,
            Case.order_reference,
            Case.scope_json,
            Case.latest_report_json,
            Case.last_observed_at,
        ).where(Case.tenant_id == tenant_id, Case.status == "open").subquery(), "all_open"
    if not isinstance(review_run_ids, list) or not 1 <= len(review_run_ids) <= 20:
        raise StateError("invalid_group_scope", 422)
    try:
        run_ids = sorted({str(UUID(str(value))) for value in review_run_ids})
    except (ValueError, TypeError, AttributeError):
        raise StateError("invalid_group_scope", 422) from None
    status = status or "needs_review"
    queries = []
    for run_id in run_ids:
        latest, _ = await period_evidence(db, tenant_id, UUID(run_id))
        query = (
            select(
                Case.id,
                Case.tenant_id,
                Case.order_reference,
                Case.scope_json,
                latest.c.report_json.label("latest_report_json"),
                latest.c.updated_at.label("last_observed_at"),
            )
            .select_from(latest)
            .join(
                Case,
                (cast(Case.id, String) == latest.c.report_json["case_id"].astext)
                & (Case.tenant_id == tenant_id)
                & (Case.order_reference == latest.c.order_reference),
            )
        )
        query = query.where(result_category(latest) == status)
        if search:
            query = query.where(latest.c.order_reference.contains(search, autoescape=True))
        queries.append(query)
    combined = union_all(*queries).subquery()
    source = (
        select(combined).distinct(combined.c.id).order_by(combined.c.id, combined.c.last_observed_at.desc()).subquery()
    )
    # Bind the selector to the cohort and filters: dropping scope in a later
    # agent call must return no members, never expand to historical cases.
    scope_key = json.dumps([run_ids, status, search], separators=(",", ":"))
    return source, scope_key


def _signature(source, scope_key):
    report = source.c.latest_report_json
    balance = report["balance"]
    columns = [
        source.c.scope_json.label("scope"),
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
    identifier = func.md5(
        cast(func.jsonb_build_array(source.c.tenant_id, cast(scope_key, String), *columns), String)
    ).label("group_id")
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


async def list_groups(db, tenant_id, *, limit=50, offset=0, review_run_ids=None, status=None, search=""):
    await set_tenant_context(db, str(tenant_id))
    source, scope_key = await _source(db, tenant_id, review_run_ids, status, search)
    columns, identifier = _signature(source, scope_key)
    grouped = (
        select(
            *columns,
            identifier,
            func.count().label("case_count"),
            func.max(source.c.last_observed_at).label("last_observed_at"),
        )
        .group_by(source.c.tenant_id, *columns)
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


async def group_members(db, tenant_id, group_id, *, limit=50, offset=0, review_run_ids=None, status=None, search=""):
    if not isinstance(group_id, str) or not re.fullmatch(r"[0-9a-f]{32}", group_id):
        raise StateError("invalid_group_id", 422)
    await set_tenant_context(db, str(tenant_id))
    source, scope_key = await _source(db, tenant_id, review_run_ids, status, search)
    _, identifier = _signature(source, scope_key)
    limit = min(50, max(1, limit))
    rows = (
        await db.execute(
            select(source.c.id, source.c.order_reference, source.c.last_observed_at)
            .where(identifier == group_id)
            .order_by(source.c.id)
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
