"""Carry exact reconciliation evidence across runs; no approval or write authority."""

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.database import set_tenant_context
from app.models.transaction_ops import TransactionCase, TransactionCaseObservation, TransactionOperation


def _cleared(report, now):
    try:
        balance = report["balance"]
        comparison = report["comparison"]
        # Matched closed/refunded records need no monetary repair. Preserve the
        # separate write-readiness verdict; reconciliation never grants a write.
        state_only = (
            comparison["recommended_action"] == "human_review"
            and comparison["findings"]
            and all(f["code"] == "record_state_requires_review" for f in comparison["findings"])
            and not comparison.get("differences")
        )
        if (
            balance["status"] != "matched"
            or balance["missing_metrics"]
            or report.get("evidence_limits")
            or (not state_only and (comparison["recommended_action"] != "no_action" or comparison["findings"]))
            or len(report["targets"]) != 1
            or report["lookup"].get("complete") is not True
            or report["lookup"].get("authoritative") is not True
        ):
            return False
        for snapshot in (report["source"], report["targets"][0]):
            observed = datetime.fromisoformat(snapshot["observed_at"])
            if snapshot.get("authoritative") is not True or not timedelta(0) <= now - observed <= timedelta(minutes=15):
                return False
        for metric in ("order_total", "tax", "refunds"):
            values = balance["amounts"][metric]
            amounts = [Decimal(values[k]) for k in ("source", "target", "delta")]
            if not all(a.is_finite() for a in amounts) or amounts[0] < 0 or amounts[0] != amounts[1] or amounts[2] != 0:
                return False
        return True
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


async def observe_finding(db, tenant_id, run, finding, *, now):
    from app.services.transaction_ops.state_service import _audit, business_digest

    report = finding.report_json
    # Legacy diagnostic-only findings are not transaction comparison evidence.
    if not isinstance(report.get("balance"), dict) and not isinstance(report.get("comparison"), dict):
        return None
    config = run.config_snapshot
    scope = {
        key: config.get(key)
        for key in ("source_connection_id", "source_step_id", "netsuite_account_id", "subsidiary_id", "record_type")
    }
    scope["netsuite_account_id"] = str(scope["netsuite_account_id"]).replace("_", "-").lower()
    key = business_digest({**scope, "order_reference": finding.order_reference})
    cleared = _cleared(report, now)
    if cleared:
        entity_key = business_digest(
            {
                "account": scope["netsuite_account_id"],
                "subsidiary": scope["subsidiary_id"],
                "record_type": scope["record_type"],
                "order_reference": finding.order_reference,
            }
        )
        unresolved = await db.scalar(
            select(TransactionOperation.id)
            .where(
                TransactionOperation.tenant_id == tenant_id,
                TransactionOperation.entity_key == entity_key,
                TransactionOperation.status.in_(["executing", "unknown"]),
            )
            .limit(1)
        )
        cleared = unresolved is None
    case = await db.scalar(
        select(TransactionCase)
        .where(TransactionCase.tenant_id == tenant_id, TransactionCase.case_key == key)
        .with_for_update()
    )
    if case is None:
        if cleared:
            return None
        await db.execute(
            insert(TransactionCase)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                case_key=key,
                order_reference=finding.order_reference,
                scope_json=scope,
                status="open",
                first_observed_at=now,
                last_observed_at=now,
                latest_report_json=report,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "case_key"])
        )
        case = await db.scalar(
            select(TransactionCase)
            .where(TransactionCase.tenant_id == tenant_id, TransactionCase.case_key == key)
            .with_for_update()
        )
    observation_key = business_digest({"finding_id": finding.id, "report": report})
    result = await db.execute(
        insert(TransactionCaseObservation)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case.id,
            run_id=run.id,
            observation_key=observation_key,
            observed_at=now,
            report_json=report,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "observation_key"])
        .returning(TransactionCaseObservation.id)
    )
    if result.scalar_one_or_none() is None:
        return case
    if now >= case.last_observed_at:
        prior = case.status
        case.status = "reconciled" if cleared else "open"
        case.last_observed_at = now
        case.latest_report_json = report
        if prior != case.status:
            await _audit(
                db, tenant_id, "case.reconciled" if cleared else "case.reopened", case, payload={"run_id": str(run.id)}
            )
    await db.flush()
    return case


async def get_case(db, tenant_id, case_id):
    from app.services.transaction_ops.state_service import StateError

    await set_tenant_context(db, str(tenant_id))
    row = await db.scalar(
        select(TransactionCase).where(TransactionCase.tenant_id == tenant_id, TransactionCase.id == case_id)
    )
    if row is None:
        raise StateError("not_found", 404)
    return row


async def list_cases(db, tenant_id, *, status=None, limit=100, offset=0):
    await set_tenant_context(db, str(tenant_id))
    query = select(TransactionCase).where(TransactionCase.tenant_id == tenant_id)
    if status:
        query = query.where(TransactionCase.status == status)
    return list(
        (
            await db.scalars(
                query.order_by(TransactionCase.last_observed_at.desc(), TransactionCase.id)
                .offset(max(0, offset))
                .limit(min(100, max(1, limit)))
            )
        ).all()
    )


async def list_observations(db, tenant_id, case_id, *, limit=100, offset=0):
    await get_case(db, tenant_id, case_id)
    return list(
        (
            await db.scalars(
                select(TransactionCaseObservation)
                .where(TransactionCaseObservation.tenant_id == tenant_id, TransactionCaseObservation.case_id == case_id)
                .order_by(TransactionCaseObservation.observed_at.desc(), TransactionCaseObservation.id)
                .offset(max(0, offset))
                .limit(min(100, max(1, limit)))
            )
        ).all()
    )


async def investigate_case(db, tenant_id, case_id, evaluation_key, *, actor):
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import state_service as state

    case = await get_case(db, tenant_id, case_id)
    await state._human(db, tenant_id, actor, "recon.run")
    configs = await state.list_configs(db, tenant_id)
    matching = []
    for config in configs:
        scope = {
            key: str(getattr(config, key)) if getattr(config, key) is not None else None for key in case.scope_json
        }
        scope["netsuite_account_id"] = scope["netsuite_account_id"].replace("_", "-").lower()
        if scope == case.scope_json and config.enabled:
            matching.append(config)
    if len(matching) != 1:
        raise state.StateError("case_scope_unavailable", 422)
    return await state.create_run(
        db,
        tenant_id,
        matching[0].id,
        RunCreate(evaluation_key=str(evaluation_key), order_references=[case.order_reference]),
        actor=actor,
    )
