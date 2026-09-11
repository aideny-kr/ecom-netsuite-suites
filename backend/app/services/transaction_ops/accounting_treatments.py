"""Partition verified proposals by accounting treatment, not similar deltas."""

from app.services.transaction_ops.state_service import business_digest


def treatment_batches(members):
    batches = {}
    for member in members:
        card = member.get("card")
        if not card:
            continue
        proposal = card["accounting_review"]
        credit = proposal.get("kind") == "sales_adjustment_credit"
        treatment = {
            "kind": "sales_adjustment_credit" if credit else "invoice_tax",
            "scope": proposal["scope"],
            "connection_id": proposal.get("connection_id"),
            "connector_id": proposal.get("connector_id"),
            "currency": proposal.get("source", {}).get("currency") or proposal["before"].get("currency_code"),
            "accounting_book": proposal["accounting_book"],
            "ar_account": proposal["ar_account"],
            "offset_account": proposal.get("sales_adjustment_account") if credit else proposal["tax_account"],
            "period": {key: proposal["period"].get(key) for key in ("id", "closed", "arLocked", "allLocked")},
            "profile": proposal["profile"] if credit else {"tax_item_id": proposal["tax_item"].get("id")},
        }
        key = business_digest(treatment)
        if key not in batches:
            batches[key] = {
                "treatment_id": key,
                "label": "Sales Adjustments credit and invoice application" if credit else "Invoice tax correction",
                "treatment": treatment,
                "case_ids": [],
                "confirmation_ids": [],
            }
        batches[key]["case_ids"].append(member["case_id"])
        batches[key]["confirmation_ids"].append(member["confirmation_id"])
    return list(batches.values())


def investigation_batches(members):
    """Retain shared next steps for unresolved cases; these never authorize writes."""
    batches = {}
    for member in members:
        if member.get("card"):
            continue
        routes = member.get("investigation_routes") or [
            {
                "code": "supported_treatment_required",
                "next_step": "Review native subledger evidence and establish a supported accounting treatment.",
            }
        ]
        for route in routes:
            key = route["code"]
            if key not in batches:
                batches[key] = {"code": key, "next_step": route["next_step"], "case_ids": [], "executable": False}
            batches[key]["case_ids"].append(member["case_id"])
    return list(batches.values())
