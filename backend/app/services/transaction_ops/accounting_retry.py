"""Read-only proof that a rejected existing-credit update had no effect.

This does not retry, reset a card, or change an operation identity. A new signed
human approval and the normal fresh preflight/atomic claim still own dispatch.
Other adapters and uncertain outcomes remain blocked until they have their own
proof of no effect.
"""

import json

from app.services.transaction_ops.accounting_recovery import evidence_digest
from app.services.transaction_ops.native_accounting_service import _stable
from app.services.transaction_ops.resolution_plan import fingerprint


async def rejected_credit_unchanged(db, tenant_id, message, proposal):
    so = message.structured_output or {}
    old = so.get("accounting_review") or {}
    claim = so.get("accounting_execution") or {}
    credit = (old.get("support") or {}).get("credit") or {}
    if (
        so.get("status") != "failed"
        or so.get("mutation_type") != "update"
        or claim.get("version") != 1
        or claim.get("confirmation_id") != str(message.id)
        or not claim.get("approved_by")
        or claim.get("receipt")
        or so.get("accounting_verification")
        or so.get("accounting_recheck")
        or claim.get("evidence_digest") != evidence_digest(so)
        or not credit.get("lastModifiedDate")
        or str(credit.get("id")) != old.get("record_id")
        or any(
            p.get("execution_transport") != "mcp_record_api"
            or p.get("kind") != "credit_tax_reallocation"
            or p.get("record_type") != "creditmemo"
            or p.get("tenant_id") != str(tenant_id)
            for p in (old, proposal)
        )
        or any(old.get(k) != proposal.get(k) for k in ("scope", "record_id", "connection_id", "connector_id", "source"))
        or _stable(old.get("support")) != _stable(proposal.get("support"))
        or _stable(old.get("protected_sales_order")) != _stable(proposal.get("protected_sales_order"))
    ):
        return None
    # Parse the stored NetSuite HTTP response, not an LLM interpretation of it.
    # A transport error, generic 'failed', or timeout is never sufficient.
    error = so.get("error")
    if not isinstance(error, str) or not error.startswith("HTTP 400: "):
        return None
    try:
        response = json.loads(error.removeprefix("HTTP 400: "))
        details = response.get("o:errorDetails")
        if response.get("status") != 400 or not isinstance(details, list) or not details:
            return None
        if any(not isinstance(d, dict) or d.get("o:errorCode") != "INVALID_VALUE" for d in details):
            return None
    except (ValueError, AttributeError):
        return None

    from app.services.transaction_ops.credit_api_correction import fresh

    try:
        _, _, _, current = await fresh(db, tenant_id, proposal)
    except Exception:
        # Failed/partial reads never release an execution reservation.
        return None
    if _stable(current) != _stable(old["support"]):
        return None
    return {
        "prior_confirmation_id": str(message.id),
        "prior_approved_by": claim["approved_by"],
        "prior_evidence_digest": claim["evidence_digest"],
        "rejection_digest": fingerprint(response),
        "unchanged_subledger_digest": fingerprint(_stable(current)),
        "record_id": old["record_id"],
        "last_modified": credit["lastModifiedDate"],
        "financial_writes": 0,
        "new_human_approval_required": True,
    }
