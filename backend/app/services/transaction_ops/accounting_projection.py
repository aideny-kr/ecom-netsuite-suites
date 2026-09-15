"""Attach audited read-only accounting explanations to the exact reviewed evidence."""

from sqlalchemy import select

from app.models.audit import AuditEvent
from app.services.transaction_ops.state_service import business_digest


def comparison_fingerprint(report):
    # case_id is added to findings after observing a case. All financial facts,
    # identities and observation times remain part of the binding.
    return business_digest({k: v for k, v in report.items() if k != "case_id"})


def project_report(report, observation):
    if not observation or observation["fingerprint"] != comparison_fingerprint(report):
        return report
    posting = observation.get("posting")
    if not isinstance(posting, dict) or posting.get("basis") != "verified_source_revision_and_owned_credit_refund":
        return report
    # Preserve the underlying verdict and amounts: an outstanding sales-order
    # amendment must not disappear merely because posting totals reconcile.
    return {
        **report,
        "balance": {
            **report["balance"],
            "posting_reconciliation": {**posting, "audit_id": str(observation["id"])},
        },
    }


async def project_rows(db, tenant_id, rows):
    """One bounded batch read; no per-order remote queries or model calls."""
    ids = list({str(r["report_json"].get("case_id")) for r in rows if r["report_json"].get("case_id")})
    observations = {}
    for start in range(0, len(ids), 500):
        evidence = AuditEvent.payload["evidence"]
        result = await db.execute(
            select(
                AuditEvent.id,
                AuditEvent.resource_id,
                evidence["comparison_fingerprint"].astext.label("fingerprint"),
                evidence["posting_balance"].label("posting"),
            )
            .where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.resource_type == "transaction_case",
                AuditEvent.resource_id.in_(ids[start : start + 500]),
                AuditEvent.action == "accounting.evidence.observed",
            )
            .distinct(AuditEvent.resource_id)
            .order_by(
                AuditEvent.resource_id,
                AuditEvent.timestamp.desc(),
                evidence["completed_at"].astext.desc().nulls_last(),
                AuditEvent.id.desc(),
            )
        )
        observations.update({r["resource_id"]: r for r in result.mappings()})
    return [
        {
            **row,
            "report_json": project_report(row["report_json"], observations.get(str(row["report_json"].get("case_id")))),
        }
        for row in rows
    ]
