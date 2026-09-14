"""Small, explicitly scoped order evidence projection shared by tables and CSV."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import String, case, cast, func, or_, select

from app.models.canonical import Order
from app.models.transaction_ops import TransactionFinding as Finding
from app.models.transaction_ops import TransactionRun as Run

BALANCE_STATUSES = {"matched", "missing_in_netsuite", "difference", "ambiguous", "currency_mismatch", "incomplete"}
FILTER_STATUSES = {"matched", "missing_in_netsuite", "needs_review", "not_verified"}


def latest_order_evidence(tenant_id):
    # Never join by order number alone: connections can expose identical references.
    # A new source version or evidence older than a daily cycle requires a recheck.
    stale = or_(
        Finding.created_at < datetime.now(timezone.utc) - timedelta(days=1),
        Order.source_updated_at > Finding.created_at,
    )
    status = Finding.report_json["balance"]["status"].astext
    return (
        select(
            func.jsonb_build_object(
                "finding_id",
                Finding.id,
                "run_id",
                Finding.run_id,
                "checked_at",
                Finding.created_at,
                "status",
                case((stale, "not_verified"), (status.in_(BALANCE_STATUSES), status), else_="not_verified"),
                "stale",
                case((stale, True), else_=False),
                "balance",
                Finding.report_json["balance"],
            )
        )
        .join(Run, (Run.id == Finding.run_id) & (Run.tenant_id == tenant_id))
        .where(
            Finding.tenant_id == tenant_id,
            Finding.order_reference == Order.order_number,
            Run.config_snapshot["source_connection_id"].astext == cast(Order.source_connection_id, String),
            Finding.report_json["source"]["record_id"].astext == Order.source_id,
            Finding.report_json["balance"]["currency"].astext == Order.currency,
        )
        .order_by(Finding.created_at.desc(), Finding.id.desc())
        .limit(1)
        .correlate(Order)
        .scalar_subquery()
    )


def reconciliation_predicate(tenant_id, value):
    if value not in FILTER_STATUSES:
        raise ValueError("Invalid reconciliation status")
    status = func.coalesce(latest_order_evidence(tenant_id).op("->>")("status"), "not_verified")
    if value == "needs_review":
        return status.in_(("difference", "ambiguous", "currency_mismatch"))
    if value == "not_verified":
        return status.in_(("not_verified", "incomplete"))
    return status == value
