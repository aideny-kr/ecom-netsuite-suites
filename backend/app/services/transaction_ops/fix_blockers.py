"""Why a fix was refused, who can unblock it, and on exactly which records (2026-10-27).

A refused correction used to reach the person as a list of "missing evidence": the guard knew exactly what
blocked it, but said only its code. On 27 Oct, 17 of 18 refused agent runs were source_not_final, and no
answer named the Solidus adjustment to void or finalize. So the blocker is computed here, from the same
evidence the guard read, never by the model: a card the person sees beside the answer, and a summary for
the model that names who acts but carries no amounts (the card shows them).

Only refusals with a known owner and action get a blocker; any other code keeps the tool's own guidance.
"""

from app.services.transaction_ops.credit_line_reallocation import OPEN_BY_DESIGN_ADJUSTMENTS, _adjustments

MAX_OBJECTS = 20

# code -> (title, owner, action). Plain words for the person who has to act.
_BLOCKERS = {
    "source_not_final": (
        "Fix blocked: Solidus can still change this order",
        "Solidus operations",
        "Void each open adjustment below if it repeats a change already made, or finalize it. "
        "Then ask again; the app never corrects NetSuite while Solidus can still change the order.",
    ),
    "period_locked": (
        "Fix blocked: the posting period is locked",
        "Finance",
        "The document's period is closed or locked. Ask for the correction in the open period, or have "
        "finance reopen the period.",
    ),
    "foreign_currency_unsupported": (
        "Fix blocked: foreign-currency document",
        "Aiden (policy)",
        "Corrections on foreign-currency documents are not enabled. Decide whether to enable them, or "
        "correct it by hand in NetSuite.",
    ),
    "tax_refund_items_not_configured": (
        "Fix blocked: no tax-refund item configured",
        "Configuration",
        "This subsidiary has no tax-refund item in its refund profile. Add the item and its tax account.",
    ),
    "credit_location_required": (
        "Fix blocked: no correction location",
        "Configuration",
        "The invoice has no location and the subsidiary has no correction location configured. Add one.",
    ),
    "invoice_paid_by_card": (
        "Fix blocked: paid by card",
        "Finance",
        "Solidus must record the refund; NetSuite then needs the credit and the customer refund together, "
        "booked by hand.",
    ),
    "netsuite_refund_already_recorded": (
        "Fix blocked: NetSuite already refunded this order",
        "Finance",
        "Do not refund again. Review the existing refund by hand.",
    ),
    "case_credit_exists": (
        "Fix blocked: the app's credit already exists",
        "A person",
        "This order already has the credit the app created. Any further correction is a person's call.",
    ),
}
KNOWN = frozenset(_BLOCKERS)

_APPLIES_TO = {"Spree::Order": "order", "Spree::LineItem": "line", "Spree::Shipment": "shipment"}


def _open(adjustment):
    return (
        isinstance(adjustment, dict)
        and "finalized" in adjustment
        and adjustment["finalized"] is not True
        and adjustment.get("source_type") not in OPEN_BY_DESIGN_ADJUSTMENTS
    )


def source_finality(source, *, require_paid=False):
    """Which finality checks the source fails, and its open adjustments as they were read.

    The same tests as the guards' (credit_line_reallocation.adjustments_final and the state checks beside
    it), reported instead of answered. Informational only: the guards decide, this explains.
    """
    source = source if isinstance(source, dict) else {}
    checks = []
    if source.get("state") != "complete":
        checks.append("order_not_complete")
    if source.get("requires_review") not in (False, None):
        checks.append("flagged_for_review")
    if not source.get("completed_at"):
        checks.append("not_completed")
    if require_paid and source.get("payment_state") != "paid":
        checks.append("payment_not_settled")
    found = [a for a in _adjustments(source) if _open(a)]
    if found:
        checks.append("open_adjustments")
    detail = {
        "checks": checks,
        "open_adjustments": [
            {
                "id": str(a.get("id")),
                "label": a.get("label"),
                "amount": None if a.get("amount") is None else str(a.get("amount")),
                "applies_to": _APPLIES_TO.get(a.get("adjustable_type"), a.get("adjustable_type")),
                "created_at": a.get("created_at"),
            }
            for a in found[:MAX_OBJECTS]
        ],
    }
    if len(found) > MAX_OBJECTS:
        detail["open_adjustments_not_listed"] = len(found) - MAX_OBJECTS
    return detail


def blocker(code, detail):
    """The refusal's owner, action and records, or None for a code with no known owner."""
    if code not in _BLOCKERS:
        return None
    title, owner, action = _BLOCKERS[code]
    detail = detail if isinstance(detail, dict) else {}
    objects = detail.get("open_adjustments") if code == "source_not_final" else None
    return {
        "code": code,
        "title": title,
        "owner": owner,
        "action": action,
        "checks": list(detail.get("checks") or []),
        "objects": list(objects) if isinstance(objects, list) else [],
        "objects_not_listed": detail.get("open_adjustments_not_listed", 0),
    }


def card(value, *, order_reference, currency, observed_at=None):
    """A result card listing the blocker's records, or None when it has none to show.

    The chat renders it like any result card: every amount comes from here, never from the model.
    """
    if not value or not value["objects"]:
        return None
    rows = [
        [o.get("id"), o.get("label"), o.get("applies_to"), o.get("amount"), (o.get("created_at") or "")[:10] or None]
        for o in value["objects"]
    ]
    return {
        "card_id": f"fix-blocker:{order_reference}:{value['code']}",
        "kind": "table",
        "result_ids": [],
        "title": value["title"],
        "source": "Solidus",
        "subtitle": f"{order_reference} · who acts: {value['owner']}",
        "as_of": observed_at,
        "scope": None,
        "queries": [],
        "columns": [
            {"key": "id", "label": "Adjustment", "format": "text", "align": "left"},
            {"key": "label", "label": "Label", "format": "text", "align": "left"},
            {"key": "applies_to", "label": "Applies to", "format": "text", "align": "left"},
            {"key": "amount", "label": "Amount", "format": "currency", "currency": currency, "align": "right"},
            {"key": "created", "label": "Created", "format": "date", "align": "left"},
        ],
        "rows": rows,
        "check": {"status": "warn", "text": value["action"]},
        "tiles": [],
        "top_n": MAX_OBJECTS,
        "collapsed": False,
        "truncated": bool(value.get("objects_not_listed")),
    }


def for_model(value, *, card_shown):
    """What the model is told: who acts and what to do, never the amounts or labels the card shows."""
    return {
        "blocked": value["code"],
        "who_acts": value["owner"],
        "what_to_do": value["action"],
        "open_records": len(value["objects"]) + int(value.get("objects_not_listed") or 0),
        "card_shown": card_shown,
        "say": "Tell the person in one or two sentences who must act and what to do. "
        + ("The card beside your answer lists the records and amounts; do not repeat them. " if card_shown else "")
        + "Do not try another write path for this refusal.",
    }


def evidence_fields(evidence, correction, *, order_reference, currency, observed_at=None):
    """The evidence tool's blocker, before any fix is tried: Solidus adjustments still open, no fix ready.

    The model reads only a producer view of the evidence, which leaves the source's adjustments out, so it
    could not say which one blocks the fix (R012251901, 27 Oct). Only open adjustments, the records the
    person can act on, make a blocker here; the fix tools still refuse every other failed check themselves.
    """
    source = evidence.get("source_refresh") if isinstance(evidence, dict) else None
    if correction is not None or not isinstance(source, dict):
        return {}
    finality = source_finality(source)
    if not finality["open_adjustments"]:
        return {}
    value = blocker("source_not_final", finality)
    shown = card(value, order_reference=order_reference, currency=currency, observed_at=observed_at)
    fields = {"fix_blocker": for_model(value, card_shown=shown is not None)}
    if shown is not None:
        fields["fix_blocker_card"] = shown
    return fields
