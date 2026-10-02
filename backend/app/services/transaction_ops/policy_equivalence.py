"""A narrow, deterministic as-observed proof; never current coverage or write authority.

The only supported delta is refund tax-reversal reason membership. Membership
outside the symmetric difference takes the identical collector/verifier branch.
The proof deliberately refuses absent, truncated or malformed request evidence.
"""

import re
from copy import deepcopy
from datetime import datetime
from decimal import Decimal

from app.services.transaction_ops.evidence_contract import VERSION
from app.services.transaction_ops.refund_adjustments import RefundAdjustmentProfile

RULE_VERSION = 2  # 2: a saved order-total difference without an invoice-credit read is affected
SCOPE_KEYS = (
    "source_connection_id",
    "source_step_id",
    "netsuite_connection_id",
    "netsuite_account_id",
    "subsidiary_id",
    "record_type",
    "target_step_id",
    "evidence_contract_version",
    "destination_discovery_version",
)
_ID = re.compile(r"^[0-9]{1,30}$")


def changed_reasons(before, after):
    """Raise on anything other than the declared policy change (no normalization guesses)."""
    if any(before.get(key) != after.get(key) for key in SCOPE_KEYS):
        raise ValueError("policy_scope_changed")
    if before.get("evidence_contract_version") != VERSION:
        raise ValueError("policy_contract_unsupported")
    left, right = deepcopy(before.get("mapping_json")), deepcopy(after.get("mapping_json"))
    try:
        old = RefundAdjustmentProfile.model_validate(left["refund_adjustments"])
        new = RefundAdjustmentProfile.model_validate(right["refund_adjustments"])
        a = left["refund_adjustments"].pop("tax_reversal_reason_ids")
        b = right["refund_adjustments"].pop("tax_reversal_reason_ids")
    except (TypeError, KeyError, ValueError):
        raise ValueError("policy_delta_unsupported") from None
    if left != right or not old.taxed_accounts or old.taxed_accounts != new.taxed_accounts:
        raise ValueError("policy_delta_unsupported")
    if old.account_id != str(before.get("netsuite_account_id")).replace("_", "-").lower():
        raise ValueError("policy_profile_scope_mismatch")
    if old.subsidiary_id != before.get("subsidiary_id"):
        raise ValueError("policy_profile_scope_mismatch")
    changed = frozenset(a) ^ frozenset(b)
    if not changed:
        raise ValueError("policy_delta_empty")
    return changed


def _id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _time(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() is None:
        raise ValueError("naive_observation")
    return timestamp


def evaluate(report, snapshot, changed, *, evaluated_at):
    """Return equivalence only for the original numeric result, never an AI judgment."""

    def gap(reason, status="unknown"):
        return {"status": status, "reason": reason}

    try:
        if report.get("_observation", {}).get("final") is not True or "evidence_limits" in report:
            return gap("incomplete_original_report")
        refunds = report["refund_evidence"]
        source, target = refunds["source"], refunds["target"]
        links = target["request_links"]
        if not isinstance(links, list) or len(links) > 100:
            return gap("request_links_incomplete")
        # Known affected membership wins over a missing unrelated field: it still
        # needs fresh proof, including when the old numeric result was matched.
        if any(isinstance(link, dict) and link.get("reason_id") in changed for link in links):
            return gap("changed_refund_reason", "affected")
        if (
            source.get("complete") is not True
            or source.get("events_complete") is not True
            or target.get("complete") is not True
        ):
            return gap("refund_evidence_incomplete")
        if (
            target.get("provider"),
            target.get("connection_id"),
            target.get("account_id"),
            target.get("subsidiary_id"),
        ) != (
            "netsuite",
            snapshot["netsuite_connection_id"],
            snapshot["netsuite_account_id"].replace("_", "-").lower(),
            snapshot["subsidiary_id"],
        ):
            return gap("refund_scope_unproven")
        if target.get("dependency_manifest", {}).get("truncated") is True:
            return gap("dependency_manifest_truncated")
        reference = report["order_reference"]
        currency = report["source"]["currency"]
        if not currency or any(
            side.get("order_reference") != reference or side.get("currency") != currency for side in (source, target)
        ):
            return gap("refund_identity_unproven")
        times = [_time(report["_observation"]["observed_at"])]
        for value in [report["source"], *report["targets"], source, target]:
            times.append(_time(value["observed_at"]))
        if not times or max(times) > evaluated_at:
            return gap("observation_time_unproven")
        seen_requests, seen_sources = set(), set()
        for link in links:
            if not isinstance(link, dict) or not all(
                _id(link.get(key)) for key in ("reason_id", "request_id", "source_refund_id")
            ):
                return gap("request_identity_unproven")
            if link["request_id"] in seen_requests or link["source_refund_id"] in seen_sources:
                return gap("duplicate_request_identity")
            seen_requests.add(link["request_id"])
            seen_sources.add(link["source_refund_id"])
            amount = Decimal(link["amount"])
            if (
                not amount.is_finite()
                or amount <= 0
                or link.get("stage") not in {"pending", "unlinked", "credit_only", "refund_verified"}
            ):
                return gap("request_evidence_invalid")
            if any(link.get(key) is not None and not _id(link[key]) for key in ("credit_memo_id", "refund_id")):
                return gap("request_evidence_invalid")
        # Require the proof objects and refund events to have survived report
        # compaction. Do not infer that an absent array is an empty lookup.
        if not isinstance(source.get("events"), list) or not isinstance(target.get("tax_adjustments"), list):
            return gap("refund_details_missing")
        for proof in target["tax_adjustments"]:
            if (
                not isinstance(proof, dict)
                or not _id(proof.get("reason_id"))
                or proof.get("request_id") not in seen_requests
            ):
                return gap("adjustment_identity_unproven")
            if proof["reason_id"] in changed:
                return gap("changed_refund_reason", "affected")
        balance = report["balance"]
        if balance.get("status") not in {"matched", "difference"} or balance.get("missing_metrics"):
            return gap("original_numeric_result_incomplete")
        for metric in ("order_total", "tax", "refunds"):
            if not all(Decimal(balance["amounts"][metric][key]).is_finite() for key in ("source", "target", "delta")):
                return gap("original_numeric_result_invalid")
        # 2026-10-01: credit memos created from the order's invoice can explain an
        # order-total difference (order_reconciliation._invoice_credits). A saved
        # difference observed before credits were read is affected, so only those
        # orders are read again; matched orders and other differences keep reuse.
        if "invoice_credits" not in target and Decimal(balance["amounts"]["order_total"]["delta"]) != 0:
            return gap("invoice_credits_not_read", "affected")
        return {
            "status": "equivalent",
            "reason": "changed_reasons_absent",
            "balance": deepcopy(balance),
            "original_observed_at": min(times).isoformat(),
            "latest_original_read_at": max(times).isoformat(),
            "currency": currency,
            "request_count": len(links),
        }
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return gap("saved_evidence_invalid")
