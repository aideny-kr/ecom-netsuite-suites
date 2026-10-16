"""Saved refund exceptions, shared by Payment Matching and its read-only API.

Reuse the order engine's fixed calendar cohort and later compatible evidence.
Do not manufacture runs, coverage, observation timestamps or financial authority.
Historical order closure is preserved; it cannot suppress later refund evidence.
"""

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, Numeric, and_, case, cast, func, literal, or_, select, union_all

from app.models.transaction_ops import TransactionFinding as Finding
from app.models.transaction_ops import TransactionRun as Run
from app.schemas.transaction_runs import ReviewSpan
from app.services.transaction_ops.periods import ReconciliationPolicy
from app.services.transaction_ops.review_evidence import period_evidence, resolve_review_runs


def money(value):
    try:
        amount = Decimal(value) if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool) else None
        return amount if amount is not None and amount.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def refund_item(row):
    metric = row["metric"] or {}
    source, target = money(metric.get("source")), money(metric.get("target"))
    complete = row["refunds_complete"] is True and source is not None and target is not None
    with localcontext() as context:
        context.prec = 60
        delta = source - target if complete else None
    return {
        "finding_id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "order_reference": row["order_reference"],
        "entity": row["entity"],
        "currency": row["currency"],
        "source_amount": str(source) if source is not None else None,
        "netsuite_amount": str(target) if complete else None,
        "difference": str(delta) if delta is not None else None,
        "status": "not_verified" if not complete else "difference" if delta else "matched",
        "historical_order_reconciled": bool(row["case_reconciled"]),
        "observed_at": row["observed_at"],
    }


async def saved_refunds(db, tenant_id, payment_run, *, limit=25, offset=0, search=""):
    # Real stored scopes provide mapping/account/entity identity. The ephemeral
    # objects below are query scopes only, never scheduled work or persisted runs.
    # Only discovery generations intersecting this calendar envelope can own
    # the cohort. Later compatible evidence still refreshes its original rows.
    lower = datetime.combine(payment_run.date_from, time.min, timezone.utc) - timedelta(hours=14)
    upper = datetime.combine(payment_run.date_to + timedelta(days=1), time.min, timezone.utc) + timedelta(hours=14)
    query = (
        select(Run)
        .where(
            Run.tenant_id == tenant_id,
            cast(Run.params_json["window_start"].astext, DateTime(timezone=True)) < upper,
            cast(Run.params_json["window_end"].astext, DateTime(timezone=True)) > lower,
        )
        .distinct(Run.config_id)
        .order_by(Run.config_id, Run.created_at.desc(), Run.id.desc())
    )
    if payment_run.subsidiary_id:
        query = query.where(Run.config_snapshot["subsidiary_id"].astext == payment_run.subsidiary_id)
    stored = (await db.scalars(query)).all()
    roots = []
    spans = {}
    for actual in stored:
        snapshot = actual.config_snapshot
        if snapshot.get("record_type") != "salesorder":
            continue
        policy = ReconciliationPolicy.model_validate(
            snapshot.get("mapping_json", {}).get("reconciliation_policy") or {}
        )
        zone = ZoneInfo(policy.timezone_name)
        start = datetime.combine(payment_run.date_from, time.min, zone).astimezone(timezone.utc)
        end = datetime.combine(payment_run.date_to + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        rid = uuid5(NAMESPACE_URL, f"saved-refunds:{tenant_id}:{actual.config_id}:{start}:{end}")
        span = ReviewSpan(id=rid, start=start, end=end)
        roots.append(
            SimpleNamespace(
                id=rid,
                tenant_id=tenant_id,
                config_id=actual.config_id,
                config_snapshot=snapshot,
                origin="manual",
                params_json={
                    "review": span.model_dump(mode="json"),
                    "evidence_mode": "saved",
                    "window_basis": "updated_at",
                },
            )
        )
        spans[str(rid)] = policy.timezone_name
    empty = {
        "items": [],
        "total": 0,
        "has_next": False,
        "evidence_mode": "saved",
        "scope": "period_orders_and_refund_activity",
        "amount_basis": "cumulative_order_refunds",
        "coverage": "not_certified",
        "provider_reads": 0,
    }
    if not roots:
        return empty
    selection = await resolve_review_runs(db, tenant_id, roots, reference_search=search)
    selections = []
    for root in roots:
        latest, _ = await period_evidence(
            db, tenant_id, root.id, root=root, metadata_only=True, _run_selection=selection, reference_search=search
        )
        scope = root.config_snapshot
        identity = "|".join(
            str(scope.get(k) or "")
            for k in ("source_connection_id", "source_step_id", "netsuite_account_id", "subsidiary_id", "record_type")
        )
        selections.append(
            select(
                latest.c.id,
                latest.c.order_reference,
                latest.c.updated_at,
                latest.c.case_reconciled,
                literal(identity).label("scope"),
                literal(scope.get("subsidiary_id")).label("subsidiary"),
            )
        )
    candidates = union_all(*selections).cte("refund_candidates")
    winners = (
        select(candidates)
        .distinct(candidates.c.scope, candidates.c.order_reference)
        .order_by(
            candidates.c.scope, candidates.c.order_reference, candidates.c.updated_at.desc(), candidates.c.id.desc()
        )
        .cte("refund_winners")
    )
    report = Finding.report_json
    metric = report["balance"]["amounts"]["refunds"]

    def numeric(value):
        return case((value.op("~")(r"^-?[0-9]+(\.[0-9]+)?$"), cast(value, Numeric(60, 18))))

    source, target = numeric(metric["source"].astext), numeric(metric["target"].astext)
    complete = and_(
        report["refund_evidence"]["target"]["complete"].as_boolean().is_(True),
        report["refund_evidence"]["source"]["complete"].as_boolean().is_(True),
    )
    rows = (
        select(
            winners.c.id,
            Finding.run_id,
            winners.c.order_reference,
            winners.c.case_reconciled,
            winners.c.subsidiary.label("entity"),
            metric.label("metric"),
            report["balance"]["currency"].astext.label("currency"),
            complete.label("refunds_complete"),
            report["_observation"]["observed_at"].astext.label("observed_at"),
        )
        .join(Finding, and_(Finding.id == winners.c.id, Finding.tenant_id == tenant_id))
        .where(source > 0, or_(complete.is_not(True), target.is_(None), source != target))
    )
    if search:
        rows = rows.where(winners.c.order_reference.contains(search, autoescape=True))
    result = (
        (
            await db.execute(
                rows.add_columns(func.count().over().label("total"))
                .order_by(winners.c.order_reference)
                .limit(limit)
                .offset(offset)
            )
        )
        .mappings()
        .all()
    )
    total = (
        result[0]["total"]
        if result
        else (await db.scalar(select(func.count()).select_from(rows.subquery())) if offset else 0)
    )
    return {
        **empty,
        "items": [refund_item(r) for r in result],
        "total": total,
        "has_next": offset + len(result) < total,
        "timezones": sorted(set(spans.values())),
    }
