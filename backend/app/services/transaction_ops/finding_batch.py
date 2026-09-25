"""Set-based persistence for non-cleared review findings; caller owns the lease.

No commits or provider calls. The state service commits this work together with
the contiguous cursor. Financially cleared and excluded reports retain the
single writer's operation checks and special lifecycle rules.
"""

from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.audit import AuditEvent
from app.models.transaction_netsuite_dependency import TransactionNetSuiteDependency
from app.models.transaction_ops import TransactionCase, TransactionCaseObservation, TransactionFinding
from app.schemas.transaction_runs import FindingReport
from app.services.transaction_ops.case_service import _cleared, case_scope, observation_time
from app.services.transaction_ops.dependency_index import observed_dependencies
from app.services.transaction_ops.netsuite_reader import _account
from app.services.transaction_ops.source_eligibility import excluded_report


def eligible(reports, now):
    return all(
        isinstance(report.get("balance"), dict) and not excluded_report(report) and not _cleared(report, now)
        for report in reports
    )


async def persist(db, tenant_id, run, reports, *, now):
    from app.services.transaction_ops.state_service import business_digest

    scope = case_scope(run)
    prepared = {}
    for raw in reports:
        request = FindingReport(order_reference=raw["order_reference"], report_json=raw)
        report = {key: value for key, value in request.report_json.items() if key not in {"case_id", "_observation"}}
        report["_observation"] = {"final": True, "observed_at": observation_time(report, now).isoformat()}
        reference = request.order_reference
        prepared[reference] = (business_digest({**scope, "order_reference": reference}), report)
    # Insertion and locking use the same reference order for overlapping runs.
    references = sorted(prepared)
    await db.execute(
        insert(TransactionCase)
        .values(
            [
                dict(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    case_key=prepared[ref][0],
                    order_reference=ref,
                    scope_json=scope,
                    status="open",
                    first_observed_at=now,
                    last_observed_at=now,
                    latest_report_json=prepared[ref][1],
                )
                for ref in references
            ]
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "case_key"])
    )
    cases = {
        row.case_key: row
        for row in await db.scalars(
            select(TransactionCase)
            .where(
                TransactionCase.tenant_id == tenant_id,
                TransactionCase.case_key.in_([prepared[ref][0] for ref in references]),
            )
            .order_by(TransactionCase.order_reference, TransactionCase.case_key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    statement = insert(TransactionFinding).values(
        [
            dict(
                tenant_id=tenant_id,
                run_id=run.id,
                order_reference=ref,
                report_json={**prepared[ref][1], "case_id": str(cases[prepared[ref][0]].id)},
            )
            for ref in references
        ]
    )
    findings = list(
        await db.scalars(
            statement.on_conflict_do_update(
                index_elements=["tenant_id", "run_id", "order_reference"],
                set_={"report_json": statement.excluded.report_json, "updated_at": now},
            )
            .returning(TransactionFinding)
            .execution_options(populate_existing=True)
        )
    )
    dependencies, observations, observation_cases = [], [], {}
    for finding in findings:
        key, report = prepared[finding.order_reference]
        case = cases[key]
        observation_key = business_digest({"finding_id": finding.id, "report": report})
        observations.append(
            dict(
                id=uuid4(),
                tenant_id=tenant_id,
                case_id=case.id,
                run_id=run.id,
                observation_key=observation_key,
                observed_at=now,
                report_json=report,
            )
        )
        observation_cases[observation_key] = (case, report)
        for kind, identifier in sorted(observed_dependencies(report, run.config_snapshot, finding.order_reference)):
            dependencies.append(
                dict(
                    tenant_id=tenant_id,
                    run_id=run.id,
                    order_reference=finding.order_reference,
                    connection_id=UUID(run.config_snapshot["netsuite_connection_id"]),
                    account_id=_account(run.config_snapshot["netsuite_account_id"]),
                    record_type=kind,
                    record_id=identifier,
                )
            )
    for start in range(0, len(dependencies), 1000):
        await db.execute(
            insert(TransactionNetSuiteDependency)
            .values(dependencies[start : start + 1000])
            .on_conflict_do_nothing(constraint="uq_tx_ns_dependency")
        )
    inserted = await db.execute(
        insert(TransactionCaseObservation)
        .values(observations)
        .on_conflict_do_nothing(index_elements=["tenant_id", "observation_key"])
        .returning(TransactionCaseObservation.observation_key, TransactionCaseObservation.id)
    )
    events = []
    correlation = structlog.contextvars.get_contextvars().get("correlation_id")

    def audit(case, action, payload):
        events.append(
            AuditEvent(
                tenant_id=tenant_id,
                category="transaction_ops",
                action="transaction_ops." + action,
                actor_type="system",
                resource_type=case.__tablename__,
                resource_id=str(case.id),
                correlation_id=correlation,
                payload={**payload, "run_id": str(run.id)},
                status="success",
            )
        )

    for key, observation_id in inserted:
        case, report = observation_cases[key]
        observed = observation_time(report, now)
        current = now >= case.last_observed_at and observed >= observation_time(
            case.latest_report_json, case.last_observed_at
        )
        audit(
            case,
            "case.evaluated",
            dict(
                observation_id=str(observation_id),
                reconciliation_verified=False,
                source_eligible=True,
                observed_at=now.isoformat(),
                verification_scope="order_total_tax_refunds",
                evidence_observed_at=observed.isoformat(),
                became_current=current,
            ),
        )
        if current:
            prior = case.status
            case.status, case.last_observed_at, case.latest_report_json = "open", now, report
            if prior != "open":
                audit(case, "case.reopened", {})
    db.add_all(events)
    await db.flush()
    return findings
