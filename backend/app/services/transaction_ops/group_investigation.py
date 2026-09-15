"""Compact, source-backed handoff from bulk preparation to the same agent."""

from decimal import Decimal, InvalidOperation


def representative_reads(investigation, definitions):
    """Bounded saved detail reads, never a new collector or executable proposal."""
    if not any(t.get("name") == "transaction_ops_accounting_evidence" for t in definitions or []):
        return []
    reads = []
    seen = set()
    for batch in investigation.get("batches", []):
        order = next((o for o in batch.get("orders", []) if o.get("case_id") and o.get("audit_id")), None)
        if order is None or str(order["case_id"]) in seen:
            continue
        seen.add(str(order["case_id"]))
        for section in ("source", "documents"):
            reads.append(
                {"case_id": str(order["case_id"]), "observation_id": str(order["audit_id"]), "section": section}
            )
        if len(seen) == 4:
            break
    return reads


def observation_preview(raw, limit=4000):
    """Keep source line/accounting facts ahead of verbose catalog metadata."""
    import json

    try:
        result = json.loads(raw)
        source = result.get("evidence")
        if result.get("section") == "source" and isinstance(source, dict):
            lines = source.get("line_items")
            if isinstance(lines, list):
                projected = []
                for line in lines:
                    if not isinstance(line, dict):
                        projected.append(line)
                        continue
                    item = {
                        k: line[k] for k in ("id", "variant_id", "parent_id", "quantity", "price", "total") if k in line
                    }
                    variant = line.get("variant") or {}
                    item["variant"] = {k: variant[k] for k in ("sku", "price") if k in variant}
                    if "adjustments" in line:
                        adjustments = line["adjustments"]
                        item["adjustments"] = (
                            [
                                {k: a[k] for k in ("id", "source_type", "source_id", "amount", "finalized") if k in a}
                                if isinstance(a, dict)
                                else a
                                for a in adjustments
                            ]
                            if isinstance(adjustments, list)
                            else adjustments
                        )
                    projected.append(item)
                source["line_items"] = projected
                source.pop("payments", None)
                result["projection"] = (
                    "Source line financial fields and payment totals/state retained; verbose catalog metadata and "
                    "individual payment details omitted. Full details remain in the saved observation."
                )
        raw = json.dumps(result, default=str, separators=(",", ":"))
    except (ValueError, TypeError, AttributeError):
        pass
    return raw if len(raw) <= limit else raw[:limit] + "\n[Preview truncated; missing detail remains unverified.]"


def difference(left, right):
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        return format((a - b).normalize(), "f") if a.is_finite() and b.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def summarize(evidence):
    source = evidence.get("source_refresh") or {}
    sections = evidence.get("sections") or {}
    documents = sections.get("posting_documents") or []
    invoice = documents[0] if len(documents) == 1 else {}
    reasons = []
    if not source:
        reasons.append("Current source evidence unavailable.")
    if len(documents) != 1 or invoice.get("record_type") != "invoice":
        reasons.append("A unique linked invoice has not been established.")
    if source and invoice:
        if difference(source.get("included_tax_total"), source.get("tax_total")) != "0":
            reasons.append("The included-VAT correction does not cover this source tax basis.")
        if difference(source.get("total"), source.get("tax_total")) != difference(
            invoice.get("total"), invoice.get("taxTotal")
        ):
            reasons.append("Net amounts also differ; changing only the tax rate cannot reconcile this order.")
        lines = source.get("line_items")
        order_adjustments = source.get("adjustments")
        collections = [order_adjustments] + [
            line.get("adjustments") if isinstance(line, dict) else None
            for line in (lines if isinstance(lines, list) else [])
        ]
        if not isinstance(lines, list) or any(
            not isinstance(values, list) or any(not isinstance(a, dict) for a in values) for values in collections
        ):
            reasons.append("Source adjustment detail is incomplete; missing values do not establish absence.")
        adjustments = [a for values in collections if isinstance(values, list) for a in values if isinstance(a, dict)]
        if any(a.get("finalized") is not True for a in adjustments):
            reasons.append(
                "Source includes adjustments marked unfinalized. Verify source revision and integration semantics; "
                "this recalculation flag alone does not establish accounting authority."
            )
        if order_adjustments == []:
            reasons.append("No order-level source adjustment supports the configured sales-discount/credit recipe.")
        if difference(invoice.get("amountPaid"), 0) not in (None, "0"):
            reasons.append("Payment is recorded; inspect credits and applications before choosing the treatment.")
    reasons.extend(evidence.get("blockers") or [])
    return {
        "observed_at": evidence.get("observed_at"),
        "audit_id": evidence.get("audit_id"),
        "line_changes": (evidence.get("line_comparison") or {}).get("changes", [])[:2],
        "line_change_count": len((evidence.get("line_comparison") or {}).get("changes") or []),
        "line_identity_gaps": len((evidence.get("line_comparison") or {}).get("unverified") or []),
        "deferred_sections": evidence.get("deferred_sections") or [],
        "scope": (evidence.get("resolution_assessment") or {}).get("facts", {}).get("scope"),
        "source": {
            k: source.get(k) for k in ("total", "tax_total", "item_total", "ship_total", "currency", "updated_at")
        },
        "invoice": {
            k: invoice.get(k)
            for k in ("id", "total", "taxTotal", "subtotal", "amountPaid", "amountRemaining", "postingPeriod")
        },
        "variance": {
            "total": difference(source.get("total"), invoice.get("total")),
            "tax": difference(source.get("tax_total"), invoice.get("taxTotal")),
        },
        "reasons": reasons,
        "record_links": evidence.get("record_links") or [],
        "historical_refunds": {
            "authority": "Historical leads only; refresh documents/applications before treatment.",
            "request_link_count": len((sections.get("historical_refunds") or {}).get("request_links") or []),
            "record_leads": [
                {k: link.get(k) for k in ("credit_memo_id", "refund_id", "amount")}
                for link in ((sections.get("historical_refunds") or {}).get("request_links") or [])[:3]
            ],
        },
    }


def handoff(selection, members, reference_hits=0):
    # Group by observed facts/eligibility, never by matching deltas alone. Keep
    # every member so no unsupported order silently disappears from the result.
    batches = {}
    for member in members:
        evidence = member.get("investigation_evidence") or {}
        scope = evidence.get("scope") or {}
        key = (
            str(scope),
            tuple(evidence.get("reasons") or [member.get("reason")]),
            (evidence.get("source") or {}).get("currency"),
        )
        if key not in batches:
            batches[key] = {"reasons": evidence.get("reasons") or [member.get("reason")], "scope": scope, "orders": []}
        batches[key]["orders"].append(
            {
                "case_id": member["case_id"],
                "order_reference": member.get("order_reference"),
                **{k: v for k, v in evidence.items() if k not in {"reasons", "scope"}},
            }
        )
    return {
        "status": "investigation_required",
        "group_id": selection["group_id"],
        "scope": selection["scope"],
        "case_count": len(members),
        "eligible": 0,
        "financial_writes": 0,
        "reference_reads_reused": reference_hits,
        "batches": [
            {**batch, "batch_number": i, "case_count": len(batch["orders"])}
            for i, batch in enumerate(batches.values(), 1)
        ],
        "next_action": (
            "Continue this investigation now with the existing evidence. Do not call the group or full case-evidence "
            "collector again merely to rediscover these facts. To inspect saved detail, call "
            "transaction_ops_accounting_evidence with case_id, observation_id=audit_id and section "
            "(source/documents/applications/assessment). This makes no upstream calls. "
            "Use targeted connected read tools for missing evidence, "
            "batching exact record IDs within the same connection/subsidiary/currency. Start with one representative "
            "per distinct cause; do not assume its treatment applies to other orders. Consult accounting references "
            "only for a specific unresolved treatment. Keep the final answer under 350 words: use the supplied "
            "batch_number and case_count exactly (batches do not overlap), a short explanation per batch, and "
            "provided record/audit links for representatives. Do not enumerate every order ID; all orders and "
            "their deltas remain in the scoped evidence. Prepare an exact supported approval card only after "
            "establishing eligibility; "
            "if the adapter cannot express the treatment, identify that specific capability gap and required evidence. "
            "A null candidate does not prove the order is correct. "
            "Source totals are ecommerce values, not native NetSuite sales-order values. Deferred GL, application "
            "or period sections are unverified: never claim balanced postings or settlement from missing evidence. "
            "Report the actual period flags; a lock alone does not prove the period is closed or that the connected "
            "role cannot make an approved correction. "
            "No empty approval card, no financial writes, no blanket "
            "claim of success. Do not ask the user to authorize investigation already requested."
        ),
    }


async def read_observation(db, tenant_id, actor_id, case_id, params, correlation_id):
    """Already authorized exact-case audit read, with no executable candidate."""
    from uuid import UUID

    from sqlalchemy import select

    from app.models.audit import AuditEvent
    from app.services.audit_service import log_event

    section = params.get("section", "assessment")
    if section not in {"source", "documents", "applications", "assessment"}:
        raise ValueError("invalid_observation_section")
    event = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.id == UUID(str(params["observation_id"])),
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.resource_type == "transaction_case",
            AuditEvent.resource_id == str(case_id),
            AuditEvent.action == "accounting.evidence.observed",
        )
    )
    if event is None:
        raise ValueError("accounting_observation_unavailable")
    evidence = (event.payload or {}).get("evidence") or {}
    sections = evidence.get("sections") or {}
    document_section = {
        k: sections.get(k)
        for k in (
            "sales_order",
            "linked_documents",
            "posting_documents",
            "gl",
            "historical_refunds",
            "related_refund_documents",
        )
    }
    document_section["line_comparison"] = evidence.get("line_comparison")
    document_section["source_revision_deltas"] = evidence.get("source_revision_deltas")
    data = {
        "source": evidence.get("source_refresh"),
        "documents": document_section,
        "applications": {
            k: sections.get(k)
            for k in ("deposits", "invoice_applications", "historical_refunds", "related_refund_documents")
        },
        "assessment": evidence.get("resolution_assessment"),
    }[section]
    await log_event(
        db,
        tenant_id,
        category="transaction_ops",
        action="accounting.observation.reused",
        actor_id=actor_id,
        resource_type="transaction_case",
        resource_id=str(case_id),
        correlation_id=correlation_id,
        payload={"observation_id": str(event.id), "section": section, "financial_writes": 0, "native_api_calls": 0},
    )
    return {
        "success": True,
        "case_id": str(case_id),
        "observation_id": str(event.id),
        "observed_at": evidence.get("observed_at"),
        "section": section,
        "evidence": data,
        "financial_writes": 0,
        "native_api_calls": 0,
        "authority": "Historical observation for investigation only; not fresh evidence or an executable proposal. "
        "Missing sections remain unverified. Refresh affected records before preparing/approving writes.",
    }


def unsupported_source_recipe(source):
    """Only defer deep preparation reads when ALL current adapters are impossible.

    Tax correction requires positive INCLUDED tax and zero additional tax.
    Sales credit, invoice discount, and SO alignment require finalized ORDER
    adjustments (via source_adjustment_basis). No order adjustments plus positive
    additional tax therefore excludes all four. Missing/ambiguous values default
    to full evidence. Extend this predicate when adding a new treatment adapter.
    """
    try:
        included = Decimal(str(source["included_tax_total"]))
        additional = Decimal(str(source["additional_tax_total"]))
        return (
            source.get("adjustments") == []
            and included.is_finite()
            and additional.is_finite()
            and included == 0
            and additional > 0
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False
