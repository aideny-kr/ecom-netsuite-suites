"""Source accounting eligibility, independent of individual payment attempts."""

from sqlalchemy import func

FAILED_PAYMENT = "source_payment_failed"


def payment_failed(order):
    # A failed historical attempt does not disqualify an order now paid.
    return order.get("payment_state") == "failed"


def excluded_report(report):
    return (report.get("source_eligibility") or {}).get("eligible") is False


def eligible_reports(column):
    """Legacy observations remain eligible until an explicit source exclusion."""
    return func.coalesce(column["source_eligibility"]["eligible"].astext, "true") != "false"


def exclusion_report(envelope):
    order = envelope["orders"][0]
    return {
        "order_reference": order["number"],
        "source_eligibility": {
            "eligible": False,
            "reason": FAILED_PAYMENT,
            "payment_state": "failed",
            "message": "Excluded from reconciliation and corrections because the source order payment failed.",
        },
        # Preserve identity/currency so a fresh exclusion supersedes compatible
        # historical findings without rewriting or broadening their cohort.
        "source": {
            "record_id": str(order["id"]),
            "currency": order["currency"],
            "updated_at": order.get("updated_at"),
            "observed_at": envelope["read_at"],
        },
        "targets": [],
        "balance": {"status": "excluded", "currency": order["currency"], "amounts": {}},
    }
