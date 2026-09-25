"""Inc exception interpretation and independent verification; never posting authority."""

import math
import re

from app.schemas.transaction_ops import _decimal
from app.services.transaction_ops.accounting_review import metric_assessment

CRITERIA = {
    "existing_credit_alignment": (
        "A verified existing commercial credit makes the net posting total equal the source "
        "total; tax and refunds agree, but the sales-order total still differs. Review sales-"
        "order alignment, never create another credit."
    ),
    "missing_order": (
        "An authoritative complete exact lookup found zero destination orders. Missing metric "
        "amounts follow from the absent order."
    ),
    "identity_currency_review": (
        "Multiple destination orders or conflicting known source/destination currencies prevent "
        "like-for-like comparison."
    ),
    "evidence_incomplete": (
        "There is one destination order, but at least one of order total, tax or completed "
        "refunds is missing, unverified, or internally inconsistent. This takes precedence over "
        "classifying the known differences."
    ),
    "tax_difference": (
        "All three metrics are known in the same currency. Tax differs, completed refunds agree, "
        "and the order-total difference is zero or exactly the tax difference. This is a pattern,"
        " not proof of tax root cause or legal treatment."
    ),
    "refund_difference": "All metrics are known in one currency. Only completed refunds differ.",
    "amount_difference": (
        "All metrics are known in one currency. Only order total differs, and no verified "
        "existing credit explains the net posting total."
    ),
    "mixed_difference": (
        "All metrics are known in one currency; multiple differences do not fit tax_difference or"
        " existing_credit_alignment. Do not add tax and order-total differences together."
    ),
    "no_amount_difference": (
        "All three metrics are known in the same currency and source equals destination for each."
        " This does not certify settlement, current freshness, or close the case."
    ),
    "needs_review": "The supplied observations do not support any other category confidently.",
}
NEXT_STEP = {
    "existing_credit_alignment": (
        "Review sales-order alignment against the existing applied credit; do not duplicate the credit."
    ),
    "missing_order": "Refresh the exact lookup and validate import eligibility before proposing a missing-order sync.",
    "identity_currency_review": "Resolve exact order identity and transaction currency.",
    "evidence_incomplete": "Collect the missing evidence while retaining independently known differences.",
    "tax_difference": "Investigate transaction tax evidence and mapping; preserve the visible variance.",
    "refund_difference": "Investigate completed refund documents and application links.",
    "amount_difference": "Investigate order adjustments and linked posting documents.",
    "mixed_difference": "Investigate each differing metric separately; do not sum gross and tax variances.",
    "no_amount_difference": "No difference in these cached metrics; retain existing case and settlement controls.",
    "needs_review": "Escalate this case for investigation.",
}
QUESTIONS = {
    "route": {
        "type": "choice",
        "instructions": (
            "Select the best classification and next investigation route for this cached Inc observation. "
            "Use the evidence, not a guessed cause. All amounts are transaction-currency decimal strings. "
            "Order total includes tax; refunds are a separate comparison. Existing applied credits "
            "must not be duplicated. "
            "These are historical observations, not current posting authority. Classify known metric "
            "evidence separately "
            "from detailed repair readiness. Choose needs_review if none fits."
        ),
        "criteria": CRITERIA,
    }
}


VERSION = "inc-exceptions-v1"


def _amount(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        return str(_decimal(value))
    except (ValueError, TypeError, ArithmeticError):
        return None


def _currency(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) else None


def project(report):
    """Send bounded scalar evidence, never names, IDs, narratives or source instructions."""
    b = report.get("balance") or {}
    lookup = report.get("lookup") or {}
    targets = report.get("targets") or []
    posting = b.get("posting_reconciliation") or {}
    return {
        "scope": "Framework Inc; transaction currency; stored observation; no posting authority",
        "source_currency": _currency(b.get("currency")),
        "destination_currency": _currency(b.get("target_currency")),
        "exact_destination_count": min(len(targets), 2),
        "lookup_complete": lookup.get("complete") is True,
        "lookup_authoritative": lookup.get("authoritative") is True,
        "metrics": {
            metric: {
                key: _amount((b.get("amounts", {}).get(metric) or {}).get(key)) for key in ("source", "target", "delta")
            }
            for metric in ("order_total", "tax", "refunds")
        },
        "unverified_metrics": [
            key for key in ("order_total", "tax", "refunds") if key in (b.get("missing_metrics") or [])
        ],
        "existing_credits": [
            {
                "verified": a.get("status") == "existing_credit_verified",
                "application_verified": a.get("invoice_application_status") == "verified",
                **{key: _amount(a.get(key)) for key in ("credit_amount", "tax_amount", "remaining_variance")},
            }
            for a in (b.get("adjustments") or [])[:8]
            if isinstance(a, dict) and a.get("kind") == "applied_commercial_credit"
        ],
        "posting_amounts": {
            "applied_credit_basis": posting.get("basis") == "applied_commercial_credit",
            **{key: _amount(posting.get(key)) for key in ("source", "net_posting_total", "delta")},
        },
    }


def existing_check(report):
    """Independent second check uses the shipped Decimal metric validator, never Jev's answer."""
    b = report.get("balance", {})
    lookup = report.get("lookup", {})
    targets = report.get("targets", [])
    if not targets:
        return (
            "missing_order"
            if b.get("status") == "missing_in_netsuite"
            and lookup.get("complete") is True
            and lookup.get("authoritative") is True
            else "needs_review"
        )
    if len(targets) != 1 or (b.get("currency") and b.get("target_currency") and b["currency"] != b["target_currency"]):
        return "identity_currency_review"
    assessment = metric_assessment(report)
    metrics = assessment["metrics"]
    if any(m["status"] == "not_verified" for m in metrics.values()):
        return "evidence_incomplete"
    changed = {k for k, v in metrics.items() if v["status"] == "difference"}
    posting = b.get("posting_reconciliation") or {}
    if (
        changed == {"order_total"}
        and posting.get("basis") == "applied_commercial_credit"
        and posting.get("status") == "matched"
    ):
        try:
            if (
                _decimal(posting["source"]) == _decimal(posting["net_posting_total"])
                and _decimal(posting["delta"]) == 0
                and any(
                    a.get("kind") == "applied_commercial_credit"
                    and a.get("status") == "existing_credit_verified"
                    and a.get("invoice_application_status") == "verified"
                    for a in b.get("adjustments", [])
                )
            ):
                return "existing_credit_alignment"
        except (KeyError, ValueError, ArithmeticError):
            pass
    if not changed:
        return "no_amount_difference"
    if changed == {"tax"} or (
        changed == {"tax", "order_total"} and assessment["gross_difference_equals_tax_difference"]
    ):
        return "tax_difference"
    if changed == {"refunds"}:
        return "refund_difference"
    if changed == {"order_total"}:
        return "amount_difference"
    return "mixed_difference"


def verify(report, answer, minimum_confidence=0.8):
    checked = existing_check(report)
    if answer is None:
        return {
            "accepted": False,
            "checked_route": checked,
            "applied_route": "needs_review",
            "reason": "provider_unavailable",
        }
    proposed = answer.get("choice")
    confidence = answer.get("confidence", 0)
    if proposed != checked:
        return {
            "accepted": False,
            "checked_route": checked,
            "applied_route": "needs_review",
            "reason": "verifier_disagreement",
        }
    if (
        type(confidence) not in (int, float)
        or not math.isfinite(confidence)
        or not minimum_confidence <= confidence <= 1
        or proposed == "needs_review"
    ):
        return {
            "accepted": False,
            "checked_route": checked,
            "applied_route": "needs_review",
            "reason": "low_confidence_or_abstention",
        }
    return {
        "accepted": True,
        "checked_route": checked,
        "applied_route": proposed,
        "reason": "verified",
    }
