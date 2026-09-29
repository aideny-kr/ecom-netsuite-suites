"""Small, explicitly scoped order evidence projection shared by tables and CSV."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import String, and_, case, cast, func, or_, select

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


def reconciliation_predicate(tenant_id, value, *, source_connection_id=None):
    if value not in FILTER_STATUSES:
        raise ValueError("Invalid reconciliation status")

    # Counts must not execute a report-reading correlated subquery for every
    # imported order. Only evidence within the freshness window can contribute
    # a verified status; older evidence and no evidence both mean not_verified.
    # Select the latest recent reading once per complete evidence identity.
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    report = func.coalesce(Finding.review_metadata_json, Finding.report_json)
    connection = Run.config_snapshot["source_connection_id"].astext
    record = report["source"]["record_id"].astext
    currency = report["balance"]["currency"].astext
    identity = (Finding.order_reference, connection, record, currency)
    readings = (
        select(
            Finding.order_reference.label("reference"),
            connection.label("connection"),
            record.label("record"),
            currency.label("currency"),
            Finding.created_at.label("checked_at"),
            report["balance"]["status"].astext.label("status"),
        )
        .join(Run, and_(Run.id == Finding.run_id, Run.tenant_id == tenant_id))
        .where(Finding.tenant_id == tenant_id, Finding.created_at >= cutoff)
        .distinct(*identity)
        .order_by(*identity, Finding.created_at.desc(), Finding.id.desc())
    )
    if source_connection_id is not None:
        readings = readings.where(connection == str(source_connection_id))
    latest = readings.cte("recent_order_evidence")
    status = case(
        (Order.source_updated_at > latest.c.checked_at, "not_verified"),
        (latest.c.status.in_(BALANCE_STATUSES), latest.c.status),
        else_="not_verified",
    )
    if value == "needs_review":
        matching = status.in_(("difference", "ambiguous", "currency_mismatch"))
    elif value == "not_verified":
        matching = status.in_(("not_verified", "incomplete"))
    else:
        matching = status == value
    orders = (
        select(Order.id)
        .outerjoin(
            latest,
            and_(
                latest.c.reference == Order.order_number,
                latest.c.connection == cast(Order.source_connection_id, String),
                latest.c.record == Order.source_id,
                latest.c.currency == Order.currency,
            ),
        )
        .where(Order.tenant_id == tenant_id, matching)
        .correlate(None)
    )
    return Order.id.in_(orders)
