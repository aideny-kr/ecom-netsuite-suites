"""Carry exact reconciliation evidence across runs; no approval or write authority."""

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.database import set_tenant_context
from app.models.transaction_ops import TransactionCase, TransactionCaseObservation, TransactionOperation
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.source_eligibility import eligible_reports, excluded_report


def refund_observation_times(report):
    """Retain financial read times even when a large report omits event details."""
    evidence = report.get("refund_evidence") or {}
    compacted = report.get("refund_observation_times") or []
    if not isinstance(evidence, dict) or not isinstance(compacted, list):
        raise ValueError("invalid_refund_observation_times")
    return [
        value["observed_at"] for value in evidence.values() if isinstance(value, dict) and "observed_at" in value
    ] + compacted


def latest_observation(report):
    """When the newest read of a report was made: the order, the NetSuite record and every refund
    read alike. Refund ledger reads land seconds after the order read, so a reference time that
    ignored them made them look "from the future" and failed every such case (2026-09-27)."""
    times = [datetime.fromisoformat(snapshot["observed_at"]) for snapshot in [report["source"], *report["targets"]]]
    times.extend(datetime.fromisoformat(value) for value in refund_observation_times(report))
    return max(times)


def observation_time(report, fallback):
    """Order evidence by its reads, not by when a slow job saved the result.

    Use the oldest read in the comparison. Legacy diagnostic reports without
    snapshot timestamps keep their existing persistence-time ordering.
    """
    try:
        snapshots = [report["source"], *report["targets"]]
        times = [datetime.fromisoformat(snapshot["observed_at"]) for snapshot in snapshots]
        times.extend(datetime.fromisoformat(value) for value in refund_observation_times(report))
        if all(value.utcoffset() is not None and value <= fallback for value in times):
            return min(times)
    except (KeyError, TypeError, ValueError):
        pass
    return fallback


def _cleared(report, now):
    if excluded_report(report):
        return False
    try:
        balance = report["balance"]
        # Financial matching is independent of repair readiness. Detail-only
        # limits and write restrictions remain in the report and planner.
        limits = report.get("evidence_limits")
        detail_only = isinstance(limits, dict) and limits.get("code") in {
            "detailed_evidence_unavailable",
            "evidence_size_limit",
        }
        if (
            balance["status"] != "matched"
            or balance["missing_metrics"]
            or (limits and not detail_only)
            or len(report["targets"]) != 1
            or report["lookup"].get("complete") is not True
            or report["lookup"].get("authoritative") is not True
        ):
            return False
        for snapshot in (report["source"], report["targets"][0]):
            observed = datetime.fromisoformat(snapshot["observed_at"])
            if snapshot.get("authoritative") is not True or not timedelta(0) <= now - observed <= timedelta(minutes=15):
                return False
        for value in refund_observation_times(report):
            observed = datetime.fromisoformat(value)
            if not timedelta(0) <= now - observed <= timedelta(minutes=15):
                return False
        for metric in ("order_total", "tax", "refunds"):
            values = balance["amounts"][metric]
            amounts = [Decimal(values[k]) for k in ("source", "target", "delta")]
            if not all(a.is_finite() for a in amounts) or amounts[0] < 0 or amounts[0] != amounts[1] or amounts[2] != 0:
                return False
        return True
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def _amount(value):
    try:
        return Decimal(str(value)) if value is not None else None
    except ArithmeticError:
        return value


def _solidus_refunds(report):
    source = (report.get("refund_evidence") or {}).get("source") or {}
    events = sorted(
        (str(event.get("id")), _amount(event.get("amount")))
        for event in source.get("events") or []
        if isinstance(event, dict)
    )
    total = (((report.get("balance") or {}).get("amounts") or {}).get("refunds") or {}).get("source")
    return events, _amount(total)


def _netsuite_credits(report):
    target = (report.get("refund_evidence") or {}).get("target") or {}

    def documents(key):
        return sorted(
            (
                str(item.get("credit_memo_id")),
                str(item.get("refund_id")),
                _amount(item.get("amount")),
                _amount(item.get("tax_amount")),
            )
            for item in target.get(key) or []
            if isinstance(item, dict)
        )

    return (
        documents("tax_adjustments"),
        documents("request_links"),
        _amount(target.get("amount")),
        target.get("refund_count"),
    )


def _netsuite_records(report):
    return {
        str(target.get("record_id")): target.get("updated_at")
        for target in report.get("targets") or []
        if isinstance(target, dict)
    }


def changes_since(reconciled, report):
    """What really changed in Solidus or NetSuite between a case's reconciling report and a later one.

    Only what was read counts: the Solidus order and its refunds, which NetSuite records were read and
    when they were last edited, and the NetSuite credits and refunds with their amounts. The same
    records compared another way are no change. Checked against every reconciled case that reopened
    on Framework in the 14 days to 2026-09-30: 63 of 66 had no change, and the other 3 each had a
    real Solidus change.
    """
    changes = []
    before, after = reconciled.get("source") or {}, report.get("source") or {}
    if (before.get("record_id"), before.get("updated_at")) != (after.get("record_id"), after.get("updated_at")):
        changes.append("solidus_order_updated")
    if _solidus_refunds(reconciled) != _solidus_refunds(report):
        changes.append("solidus_refunds_changed")
    before, after = _netsuite_records(reconciled), _netsuite_records(report)
    if set(before) != set(after):
        changes.append("netsuite_records_changed")
    elif before != after:
        changes.append("netsuite_record_updated")
    if _netsuite_credits(reconciled) != _netsuite_credits(report):
        changes.append("netsuite_credits_changed")
    return changes


def settle_observation(case, report, *, cleared, now):
    """Apply a newer observation to its case and return the audit actions it causes.

    A reconciled case stays reconciled (decided 2026-09-30): only a clearing observation may refresh
    its evidence. Any other later observation leaves the status and the reconciling evidence as they
    are and is recorded as kept, or as changed after reconciliation when a record really changed.
    The database refuses any update that would reopen one (migration 117).
    """
    if case.status == "reconciled" and not cleared:
        changes = changes_since(case.latest_report_json, report)
        if changes:
            return [("case.changed_after_reconciliation", {"changes": changes})]
        return [("case.kept_reconciled", {})]
    prior = case.status
    case.status = "reconciled" if cleared else "open"
    case.last_observed_at = now
    case.latest_report_json = report
    return [("case.reconciled", {})] if prior != case.status else []


async def changed_after_reconciliation(db, tenant_id, *, limit=100):
    """Reconciled cases whose Solidus order or NetSuite records changed after reconciliation.

    They stay reconciled; this is the list a person reviews, newest change first, one row per case.
    """
    from sqlalchemy import String, cast

    from app.models.audit import AuditEvent

    await set_tenant_context(db, str(tenant_id))
    latest = (
        select(AuditEvent.resource_id, AuditEvent.payload, AuditEvent.timestamp)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.action == "transaction_ops.case.changed_after_reconciliation",
            AuditEvent.resource_type == TransactionCase.__tablename__,
        )
        .distinct(AuditEvent.resource_id)
        .order_by(AuditEvent.resource_id, AuditEvent.timestamp.desc())
        .subquery()
    )
    rows = await db.execute(
        select(TransactionCase, latest.c.payload, latest.c.timestamp)
        .join(latest, cast(TransactionCase.id, String) == latest.c.resource_id)
        .where(TransactionCase.tenant_id == tenant_id, TransactionCase.status == "reconciled")
        .order_by(latest.c.timestamp.desc(), TransactionCase.id)
        .limit(min(500, max(1, limit)))
    )
    return {
        "cases": [
            {
                "case_id": str(case.id),
                "order_reference": case.order_reference,
                "scope": case.scope_json,
                "status": case.status,
                "changes": list((payload or {}).get("changes") or []),
                "observation_id": (payload or {}).get("observation_id"),
                "changed_at": changed_at.isoformat(),
            }
            for case, payload, changed_at in rows
        ]
    }


def case_scope(run):
    config = run.config_snapshot
    scope = {
        key: config.get(key)
        for key in ("source_connection_id", "source_step_id", "netsuite_account_id", "subsidiary_id", "record_type")
    }
    scope["netsuite_account_id"] = str(scope["netsuite_account_id"]).replace("_", "-").lower()
    return scope


async def observe_finding(db, tenant_id, run, finding, *, now):
    from app.services.transaction_ops.state_service import _audit, business_digest

    report = finding.report_json
    # Legacy diagnostic-only findings are not transaction comparison evidence.
    if not isinstance(report.get("balance"), dict) and not isinstance(report.get("comparison"), dict):
        return None
    scope = case_scope(run)
    key = business_digest({**scope, "order_reference": finding.order_reference})
    excluded = excluded_report(report)
    cleared = not excluded and _cleared(report, now)
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
                TransactionOperation.status.in_(state.IN_FLIGHT),
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
        if cleared or excluded:
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
    observation_id = result.scalar_one_or_none()
    if observation_id is None:
        return case
    observed = observation_time(report, now)
    became_current = now >= case.last_observed_at and observed >= observation_time(
        case.latest_report_json, case.last_observed_at
    )
    await _audit(
        db,
        tenant_id,
        "case.evaluated",
        case,
        payload={
            "run_id": str(run.id),
            "observation_id": str(observation_id),
            "reconciliation_verified": cleared,
            "source_eligible": not excluded,
            "observed_at": now.isoformat(),
            "verification_scope": "order_total_tax_refunds",
            "evidence_observed_at": observed.isoformat(),
            "became_current": became_current,
        },
    )
    if became_current:
        for action, payload in settle_observation(case, report, cleared=cleared, now=now):
            await _audit(
                db,
                tenant_id,
                action,
                case,
                payload={"run_id": str(run.id), "observation_id": str(observation_id), **payload},
            )
        if excluded:
            await _audit(
                db,
                tenant_id,
                "case.excluded",
                case,
                payload={"run_id": str(run.id), **report["source_eligibility"]},
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
    query = select(TransactionCase).where(
        TransactionCase.tenant_id == tenant_id, eligible_reports(TransactionCase.latest_report_json)
    )
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


async def investigate_cases(db, tenant_id, case_ids, evaluation_key, *, actor):
    """Bulk read/proposal work only. Ownership is checked before the first queued run."""
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import state_service as state

    await state._human(db, tenant_id, actor, "recon.run")
    if not 1 <= len(case_ids) <= 50 or len(set(case_ids)) != len(case_ids):
        raise state.StateError("invalid_case_selection", 422)
    selected = [await get_case(db, tenant_id, identifier) for identifier in case_ids]
    configs = await state.list_configs(db, tenant_id)
    groups, blocked = {}, []
    for case in selected:
        matching = []
        for config in configs:
            scope = {
                key: str(getattr(config, key)) if getattr(config, key) is not None else None for key in case.scope_json
            }
            scope["netsuite_account_id"] = scope["netsuite_account_id"].replace("_", "-").lower()
            if scope == case.scope_json and config.enabled:
                matching.append(config)
        if len(matching) != 1:
            blocked.append({"case_id": case.id, "code": "case_scope_unavailable"})
        else:
            groups.setdefault(matching[0].id, []).append(case)
    runs = []
    for config_id, cases in groups.items():
        try:
            run = await state.create_run(
                db,
                tenant_id,
                config_id,
                RunCreate(
                    evaluation_key=f"cases:{evaluation_key}:{config_id}",
                    order_references=[c.order_reference for c in cases],
                ),
                actor=actor,
            )
            runs.append({"id": run.id, "config_id": config_id, "case_ids": [c.id for c in cases]})
        except state.StateError as exc:
            blocked.extend({"case_id": c.id, "code": exc.code} for c in cases)
    return {"runs": runs, "blocked": blocked}
