"""Human-triggered calendar reviews use backend-owned policy and durable runs."""

from datetime import date, datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select

from app.models.transaction_ops import TransactionRun
from app.models.user import User
from app.schemas.transaction_runs import ReviewSpan, RunCreate
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.normalization import TransactionMapping
from app.services.transaction_ops.periods import ReconciliationPolicy, review_window


class PeriodReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evaluation_key: UUID
    period: Literal["yesterday", "last_week", "last_month", "custom"]
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def dates_match_period(self):
        if self.period == "custom":
            if self.start_date is None or self.end_date is None:
                raise ValueError("Custom periods require start and end dates")
        elif self.start_date is not None or self.end_date is not None:
            raise ValueError("Preset periods determine their own dates")
        return self


def utc_now():
    return datetime.now(timezone.utc)


async def create_review(db, tenant_id, config_id, request, *, actor):
    await state._human(db, tenant_id, actor, "recon.run")
    config = await state.get_config(db, tenant_id, config_id)
    try:
        mapping = TransactionMapping.model_validate(config.mapping_json)
        policy = mapping.reconciliation_policy or ReconciliationPolicy()
        scope = review_window(
            request.period, utc_now(), policy.timezone_name, start_date=request.start_date, end_date=request.end_date
        )
    except ValueError:
        raise state.StateError("invalid_review_period", 422) from None
    span = ReviewSpan(id=request.evaluation_key, start=scope["window_start"], end=scope["window_end"])
    scope["window_end"] = min(span.end, span.start + timedelta(days=1))
    return await state.create_run(
        db,
        tenant_id,
        config_id,
        RunCreate(evaluation_key=str(request.evaluation_key), review=span, **scope),
        actor=actor,
    )


async def continue_review(db, tenant_id, run_id):
    previous = await state.get_run(db, tenant_id, run_id)
    if previous.status != "finished" or previous.termination_reason != "done" or not previous.params_json.get("review"):
        return None
    if not previous.progress_json.get("scan_complete") or not previous.progress_json.get("refund_scan_complete"):
        return None
    span = ReviewSpan.model_validate(previous.params_json["review"])
    start = datetime.fromisoformat(previous.params_json["window_end"])
    if start >= span.end:
        return None
    config = await state.get_config(db, tenant_id, previous.config_id, lock=True)
    if not config.enabled:
        await state._commit(db, tenant_id)
        return None
    contract = span.model_dump(mode="json")
    end = min(span.end, start + timedelta(days=1))
    request = RunCreate(
        origin=previous.origin,
        evaluation_key=f"review:{span.id}:{start.isoformat()}",
        window_start=start,
        window_end=end,
        window_basis="completed_at",
        review=span,
    )
    child = await db.scalar(
        select(TransactionRun).where(
            TransactionRun.tenant_id == tenant_id,
            TransactionRun.config_id == config.id,
            TransactionRun.params_json["review"] == contract,
            TransactionRun.params_json["window_start"].astext == request.model_dump(mode="json")["window_start"],
        )
    )
    if child:
        await state._commit(db, tenant_id)
        return child
    from app.services.transaction_ops.runner import enabled

    if not await enabled(db, tenant_id):
        await state._commit(db, tenant_id)
        return None
    active = await db.scalar(
        select(TransactionRun.id)
        .where(
            TransactionRun.tenant_id == tenant_id,
            TransactionRun.config_id == config.id,
            TransactionRun.status.in_(["pending", "running"]),
        )
        .limit(1)
    )
    if active:
        await state._commit(db, tenant_id)
        return None
    actor = await db.scalar(select(User).where(User.tenant_id == tenant_id, User.id == previous.initiated_by))
    try:
        await state._human(db, tenant_id, actor, "recon.run")
    except state.StateError:
        await state._audit(db, tenant_id, "run.continuation_blocked", previous, payload={"reason": "permission_denied"})
        await state._commit(db, tenant_id)
        return None
    return await state.create_run(db, tenant_id, config.id, request, actor=actor)


async def review_status(db, tenant_id, run_id):
    root = await state.get_run(db, tenant_id, run_id)
    if not root.params_json.get("review"):
        raise state.StateError("not_a_period_review", 422)
    span = ReviewSpan.model_validate(root.params_json["review"])
    runs = list(
        (
            await db.scalars(
                select(TransactionRun)
                .where(
                    TransactionRun.tenant_id == tenant_id,
                    TransactionRun.config_id == root.config_id,
                    TransactionRun.params_json["review"] == span.model_dump(mode="json"),
                )
                .order_by(TransactionRun.created_at, TransactionRun.id)
                .limit(513)
            )
        ).all()
    )
    # Each daily slice has its own existing 16-part/24-hour execution budget.
    # Its final continuation carries cumulative counters, so never sum all parts.
    slices = {}
    for run in runs[:512]:
        params = run.params_json
        key = (datetime.fromisoformat(params["window_start"]), datetime.fromisoformat(params["window_end"]))
        previous = slices.get(key)
        part = run.progress_json.get("continuation_part", 1)
        if previous is None or part > previous.progress_json.get("continuation_part", 1):
            slices[key] = run
    completed_until = span.start
    completed_slices = 0
    for (start, end), run in sorted(slices.items()):
        if start != completed_until or run.termination_reason != "done" or not run.progress_json.get("scan_complete"):
            break
        if not run.progress_json.get("refund_scan_complete"):
            break
        completed_until = end
        completed_slices += 1
    complete = completed_until == span.end and len(runs) <= 512
    active = next((r for r in reversed(runs) if r.status in ("pending", "running")), None)
    return {
        "review_id": str(span.id),
        "period_start": span.model_dump(mode="json")["start"],
        "period_end": span.model_dump(mode="json")["end"],
        "completed_until": completed_until.isoformat().replace("+00:00", "Z"),
        "complete": complete,
        # A finished scan is neither a replica watermark nor an accounting sign-off.
        "completion_basis": "scan_coverage",
        "comparison_basis": "current_evidence_for_period_cohort",
        "source_freshness": "unverified",
        "financial_status": "not_certified",
        "status": "complete" if complete else "running" if active else "needs_attention",
        "completed_slices": completed_slices,
        "run_count": len(runs[:512]),
        "truncated": len(runs) > 512,
        "current_run_id": str(active.id) if active else str(runs[-1].id),
        "slices": [
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "run_id": str(run.id),
                "status": run.status,
                "termination_reason": run.termination_reason,
            }
            for (start, end), run in sorted(slices.items())
        ],
    }


async def review_results(db, tenant_id, run_id, *, limit=25, offset=0, status=None, search=""):
    """Latest evidence per order within one immutable review; never sum run counters."""
    from sqlalchemy import case, func

    from app.models.transaction_ops import TransactionFinding

    root = await state.get_run(db, tenant_id, run_id)
    if not root.params_json.get("review"):
        raise state.StateError("not_a_period_review", 422)
    span = ReviewSpan.model_validate(root.params_json["review"])
    f, r = TransactionFinding, TransactionRun
    latest = (
        select(f.id, f.run_id, f.order_reference, f.report_json, f.updated_at)
        .join(r, (f.tenant_id == r.tenant_id) & (f.run_id == r.id))
        .where(
            f.tenant_id == tenant_id,
            r.tenant_id == tenant_id,
            r.config_id == root.config_id,
            r.params_json["review"] == span.model_dump(mode="json"),
        )
        .distinct(f.order_reference)
        .order_by(f.order_reference, f.updated_at.desc(), f.id.desc())
        .subquery()
    )
    verdict = latest.c.report_json["balance"]["status"].astext
    category = case(
        (verdict == "matched", "matched"),
        (
            verdict.in_(["difference", "mismatch", "missing_in_netsuite", "ambiguous", "currency_mismatch"]),
            "needs_review",
        ),
        else_="not_verified",
    )
    counts = (
        (
            await db.execute(
                select(
                    func.count().label("checked"),
                    *[
                        func.count().filter(category == name).label(name)
                        for name in ("matched", "needs_review", "not_verified")
                    ],
                ).select_from(latest)
            )
        )
        .mappings()
        .one()
    )
    query = select(latest)
    if status:
        if status not in ("matched", "needs_review", "not_verified"):
            raise state.StateError("invalid_result_status", 422)
        query = query.where(category == status)
    if search:
        query = query.where(latest.c.order_reference.contains(search, autoescape=True))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        (
            await db.execute(
                query.order_by(latest.c.order_reference).limit(min(100, max(1, limit))).offset(max(0, offset))
            )
        )
        .mappings()
        .all()
    )
    items = []
    for row in rows:
        report = row["report_json"]
        items.append(
            {
                "id": str(row["id"]),
                "run_id": str(row["run_id"]),
                "order_reference": row["order_reference"],
                "observed_at": row["updated_at"].isoformat(),
                "balance": report.get("balance"),
                "case_id": report.get("case_id"),
                "action": (report.get("comparison") or {}).get("recommended_action"),
                "automation": report.get("automation"),
            }
        )
    return {
        "items": items,
        "total": total,
        "has_next": offset + len(items) < total,
        "summary": dict(counts),
        "scope": "period_orders_and_refund_activity",
        "review_id": str(span.id),
    }
