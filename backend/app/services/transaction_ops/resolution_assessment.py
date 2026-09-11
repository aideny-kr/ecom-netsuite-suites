"""Explain supported treatment selection without expanding financial write authority."""

from decimal import Decimal, InvalidOperation

from app.services.transaction_ops.state_service import business_digest


def _money(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def assess(evidence, report, review, correction=None, references=None):
    sections = evidence.get("sections") or {}
    documents = sections.get("posting_documents") or []
    invoice = documents[0] if len(documents) == 1 else {}
    applications = sections.get("invoice_applications") or {}
    total, paid, remaining = (_money(invoice.get(k)) for k in ("total", "amountPaid", "amountRemaining"))
    application_complete = applications.get("complete") is True
    links = applications.get("links")
    fully_unpaid = (
        total is not None and total > 0 and paid == 0 and remaining == total and application_complete and links == []
    )
    payment_state = "fully_unpaid_no_applications" if fully_unpaid else "not_established"
    if paid is not None and remaining is not None and paid > 0:
        payment_state = "partially_paid" if remaining > 0 else "payment_observed_no_remaining_receivable"
    selected = (correction.get("kind") or "invoice_tax") if correction else None
    existing_resolution = evidence.get("commercial_credit_resolution")
    facts = {
        "scope": review.get("scope"),
        "posting_document_count_observed": len(documents),
        "invoice_id": invoice.get("id"),
        "invoice_total": invoice.get("total"),
        "invoice_paid": invoice.get("amountPaid"),
        "invoice_remaining": invoice.get("amountRemaining"),
        "payment_state": payment_state,
        "application_evidence_complete": application_complete,
        "application_count": len(links) if isinstance(links, list) else None,
        "root_cause": (evidence.get("assessment") or {}).get("root_cause", "not_verified"),
    }
    options = [
        {
            "kind": "verify_existing_correction",
            "status": "verification_required",
            "reason": "Inspect linked credits/discounts and complete current reconciliation before another posting.",
        },
        {
            "kind": "apply_existing_credit_or_deposit",
            "status": "investigation_required",
            "reason": "Verify the unallocated balance and intended application; never create cash or duplicate credit.",
        },
        {
            "kind": "invoice_sales_adjustment",
            "status": "not_established",
            "reason": "Requires the configured adapter's finalized commercial basis, unpaid/no-application state, "
            "zero tax/shipping, posting item and open original period.",
        },
        {
            "kind": "sales_adjustment_credit",
            "status": "not_established",
            "reason": "Requires verified source adjustment, payment/application state, "
            "duplicate checks and the configured credit adapter.",
        },
        {
            "kind": "invoice_tax",
            "status": "not_established",
            "reason": "Requires a supported exact tax correction with finalized source basis "
            "and native account/period controls; no inferred statutory rate.",
        },
        {
            "kind": "integration_repair",
            "status": "investigation_required",
            "reason": "Prove missing/incorrect integration output and replay semantics before an exact sync proposal.",
        },
    ]
    if fully_unpaid:
        options[3].update(
            status="not_selected",
            reason="The configured unpaid-invoice workflow evaluates a direct discount; "
            "unsupported unpaid cases do not fall back to credit creation.",
        )
    for option in options:
        if option["kind"] == selected:
            option.update(status="supported_exact_proposal", reason=correction["approval_basis"])
    result = {
        "version": 1,
        "status": "ready_for_human_approval" if correction else "investigation_required",
        "selected_treatment": selected,
        "facts": facts,
        "alternatives": options,
        "observed_comparison_status": (report.get("balance") or {}).get("status"),
        "existing_correction_observed": bool(existing_resolution),
        "references": references or [],
        "unresolved_evidence": list(evidence.get("blockers") or []),
        "approval_required": True,
        "financial_write_authorized": False,
        "verification_required": [
            "fresh_source_and_native_preflight",
            "native_record_and_gl_readback",
            "application_and_remaining_balance",
            "full_order_tax_refund_reconciliation",
        ],
        "settlement": "Cash/processor settlement requires separate evidence.",
        "interpretation": "A supported proposal satisfies its adapter's scoped eligibility; it is not certification "
        "of all accounting policy, tax legality or the historical integration cause. "
        "Alternatives marked unestablished require evidence, not a guessed write.",
    }
    if correction:
        result["eligibility_basis"] = {
            "scope": correction.get("scope"),
            "profile": correction.get("profile"),
            "kind": selected,
            "period": correction.get("period"),
            "accounting_book": correction.get("accounting_book"),
        }
    result["assessment_fingerprint"] = business_digest(result)
    return result


async def reference_provenance(db, tenant_id, case_id):
    from sqlalchemy import select

    from app.models.audit import AuditEvent

    events = await db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.resource_type == "transaction_case",
            AuditEvent.resource_id == str(case_id),
            AuditEvent.action == "accounting.reference.observed",
        )
        .order_by(AuditEvent.timestamp.desc(), AuditEvent.id.desc())
        .limit(3)
    )
    return [
        {
            "audit_id": str(event.id),
            "observed_at": (event.payload or {}).get("observed_at"),
            "topic": (event.payload or {}).get("topic"),
            "sources": [
                {k: source.get(k) for k in ("url", "title", "evidence_kind", "document_sha256")}
                for source in (event.payload or {}).get("sources", [])[:2]
            ],
            "authority": "Historical reference observation; not account evidence or approval.",
        }
        for event in events
    ]
