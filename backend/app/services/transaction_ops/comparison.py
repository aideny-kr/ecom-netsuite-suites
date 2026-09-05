"""Deterministic comparison. No tools, model calls or external mutations.

Recommendations are candidates for a later evidence-bound HITL proposal. A
matching total alone cannot establish correctness, a Celigo error's disappearance
cannot establish success, and stale/partial evidence cannot establish absence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import (
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)

from app.schemas.transaction_ops import (
    TransactionComparison,
    TransactionDifference,
    TransactionFinding,
    TransactionLookup,
    TransactionSnapshot,
)

_AMOUNTS = ("total", "subtotal", "shipping", "shipping_tax", "discount", "tax")
_IMPORTABLE = frozenset({"confirmed", "fulfilled"})


def _fingerprint(source, records, lookup) -> str:
    def normalize(value):
        if isinstance(value, Decimal):
            return format(value.normalize(), "f") if value else "0"
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(v) for v in value]
        return value

    def canonical(snapshot):
        data = snapshot.model_dump(mode="python", exclude={"observed_at"})
        for key in ("lines", "tax_details"):
            data[key] = sorted(data[key], key=lambda item: item["key"])
        return data

    payload = {
        "source": canonical(source),
        "targets": sorted((canonical(r) for r in records), key=lambda r: (r["account_id"], r["record_id"])),
        "lookup": lookup.model_dump(mode="python", exclude={"observed_at", "error_observed_at"}),
    }
    return hashlib.sha256(json.dumps(normalize(payload), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def compare_transactions(
    source: TransactionSnapshot,
    netsuite_records: Sequence[TransactionSnapshot],
    lookup: TransactionLookup,
    *,
    now: datetime,
    max_age: timedelta = timedelta(minutes=15),
) -> TransactionComparison:
    # The validated magnitude/scale plus 2000 lines fits in 60 digits. Never
    # inherit an unrelated caller's Decimal precision (or silently round money).
    with localcontext(Context(prec=60, Emin=-99, Emax=99, traps=[InvalidOperation, DivisionByZero, Overflow])):
        return _compare_transactions(source, netsuite_records, lookup, now=now, max_age=max_age)


def _compare_transactions(source, netsuite_records, lookup, *, now, max_age):
    if now.tzinfo is None or now.utcoffset() is None or max_age <= timedelta(0):
        raise ValueError("Comparison needs an aware clock and a positive evidence lifetime")
    findings = []
    differences = []
    fingerprint = _fingerprint(source, netsuite_records, lookup)

    def finding(code, reason):
        findings.append(TransactionFinding(code=code, reason=reason))

    def result(action):
        return TransactionComparison(
            recommended_action=action,
            currency=source.currency,
            evidence_fingerprint=fingerprint,
            findings=findings,
            differences=differences,
        )

    def fresh(observed_at):
        return timedelta(0) <= now - observed_at <= max_age

    # The lookup is collector-bound to an exact source and destination. Never
    # use a fuzzy/prefix order-reference match to prove an absence or an error.
    if (source.system, source.account_id, source.record_id, source.order_reference) != (
        lookup.source_system,
        lookup.source_account_id,
        lookup.source_record_id,
        lookup.order_reference,
    ):
        finding(
            "source_identity_mismatch", "Lookup does not cover the exact source account, record and order reference."
        )
        return result("human_review")
    if source.subsidiary_id != lookup.target_subsidiary_id:
        finding("subsidiary_mismatch", "Source subsidiary mapping does not match the configured NetSuite destination.")
        return result("human_review")
    if lookup.celigo_error_id and lookup.error_order_reference != source.order_reference:
        finding("error_identity_unproven", "Celigo error is not linked to this full order reference.")
        return result("human_review")
    if not (lookup.complete and lookup.authoritative and fresh(lookup.observed_at)):
        finding("lookup_incomplete", "An authoritative, complete and fresh destination lookup is required.")
        return result("gather_evidence")
    if lookup.celigo_error_id and (
        not lookup.error_is_open or lookup.error_observed_at is None or not fresh(lookup.error_observed_at)
    ):
        finding(
            "error_state_unproven",
            "Celigo error must be freshly observed and still open before any resolution proposal.",
        )
        return result("gather_evidence")

    # Structural target mismatches veto arithmetic altogether.
    for target in netsuite_records:
        if (target.system, target.account_id, target.subsidiary_id, target.record_type, target.order_reference) != (
            "netsuite",
            lookup.target_account_id,
            lookup.target_subsidiary_id,
            lookup.target_record_type,
            source.order_reference,
        ):
            finding(
                "target_identity_mismatch",
                "Destination evidence has a different system, account, subsidiary, type or order.",
            )
            return result("human_review")
    if len(netsuite_records) > 1:
        finding(
            "ambiguous_match", "Multiple destination records match; no arbitrary pairing or amount aggregation is safe."
        )
        return result("human_review")
    for snapshot in (source, *netsuite_records):
        if not snapshot.currency or snapshot.amount_basis != "transaction" or snapshot.currency != source.currency:
            finding(
                "currency_mismatch",
                "Known, equal transaction currencies are required; base amounts are not comparable.",
            )
            return result("human_review")
        if snapshot.status not in _IMPORTABLE:
            finding(
                "record_state_requires_review", "Draft, cancelled, refunded or unknown records require investigation."
            )
            return result("human_review")
        if not snapshot.authoritative or not fresh(snapshot.observed_at):
            finding("stale_evidence", "Evidence must be authoritative and freshly observed in the source system.")
        if snapshot.updated_at is None or snapshot.updated_at > snapshot.observed_at:
            finding("version_unproven", "A source version/time no later than the observation is required.")
        if any(getattr(snapshot, field) is None for field in _AMOUNTS):
            finding("unknown_amount", "Missing tax, shipping, discount or totals cannot be treated as zero.")
        if snapshot.currency_minor_unit is None or any(tax.rounding is None for tax in snapshot.tax_details):
            finding(
                "tax_calculation_unproven", "Currency precision and each tax component's rounding policy must be known."
            )
        if not snapshot.lines_complete or not snapshot.tax_complete or not snapshot.lines:
            finding(
                "incomplete_detail",
                "Complete line and tax evidence is required before proposing a repair or resolving an error.",
            )
        if any(line.net is None or line.tax is None for line in snapshot.lines) or any(
            tax.basis is None or tax.rate is None or tax.amount is None for tax in snapshot.tax_details
        ):
            finding("incomplete_detail", "A line or tax detail contains unknown values.")
        if snapshot.tax and not snapshot.tax_details:
            finding("incomplete_detail", "Nonzero tax requires tax detail evidence.")
    if findings:
        # Detection remains useful when a collector still lacks tax metadata.
        # These observations never promote incomplete evidence into a repair.
        if all(snapshot.authoritative and fresh(snapshot.observed_at) for snapshot in (source, *netsuite_records)):
            if not netsuite_records:
                finding(
                    "missing_transaction",
                    "Complete exact lookup found no destination record; source detail still needs verification.",
                )
            else:
                target = netsuite_records[0]
                for field in _AMOUNTS:
                    expected, actual = getattr(source, field), getattr(target, field)
                    if expected is not None and actual is not None and expected != actual:
                        differences.append(
                            TransactionDifference(field=field, source=expected, target=actual, delta=expected - actual)
                        )
                if differences:
                    finding(
                        "amount_mismatch",
                        "Observed transaction-currency amounts differ; incomplete detail prevents a repair proposal.",
                    )
        return result("gather_evidence")

    for snapshot in (source, *netsuite_records):
        quantum = Decimal(1).scaleb(-snapshot.currency_minor_unit)
        amounts = [getattr(snapshot, name) for name in _AMOUNTS]
        amounts.extend(value for line in snapshot.lines for value in (line.net, line.tax))
        amounts.extend(tax.amount for tax in snapshot.tax_details)
        if any(value != value.quantize(quantum) for value in amounts):
            finding("currency_precision_violation", "Final monetary amounts exceed the declared currency precision.")
            return result("human_review")

    # The source is the proposed truth. Invalid canonical arithmetic must not
    # be propagated into NetSuite. Target inconsistencies remain findings.
    expected_total = source.subtotal + source.shipping - source.discount + source.tax
    if source.total != expected_total or sum((line.net for line in source.lines), Decimal(0)) != source.subtotal:
        finding(
            "source_amounts_inconsistent", "Source totals/lines do not reconcile under the configured amount mapping."
        )
        return result("human_review")
    if (
        sum((tax.amount for tax in source.tax_details), Decimal(0)) != source.tax
        or sum((line.tax for line in source.lines), Decimal(0)) + source.shipping_tax != source.tax
    ):
        finding("source_tax_inconsistent", "Source tax details do not reconcile to the source tax total.")
        return result("human_review")
    for side, snapshot in (("source", source), *(("target", target) for target in netsuite_records)):
        quantum = Decimal(1).scaleb(-snapshot.currency_minor_unit)
        for tax in snapshot.tax_details:
            rounding = ROUND_HALF_UP if tax.rounding == "half_up" else ROUND_HALF_EVEN
            calculated = tax.basis * tax.rate
            if tax.included_gross_basis is not None:
                calculated = tax.included_gross_basis * tax.rate / (Decimal(1) + tax.included_rate_total)
            if calculated.quantize(quantum, rounding=rounding) != tax.amount:
                finding(
                    f"{side}_tax_calculation_inconsistent",
                    f"{side.capitalize()} taxable basis, rate and rounded tax amount contradict each other.",
                )
                return result("human_review")
    if source.total < 0 or source.discount < 0 or any(line.quantity <= 0 for line in source.lines):
        finding(
            "source_state_requires_review", "Negative totals/discounts or nonpositive quantities require investigation."
        )
        return result("human_review")
    if not netsuite_records:
        finding(
            "missing_transaction", "Complete authoritative lookup found no destination record for this exact order."
        )
        return result("propose_missing_sync")

    target = netsuite_records[0]
    if source.currency_minor_unit != target.currency_minor_unit:
        finding("currency_precision_mismatch", "Source and destination currency precision metadata differ.")
    if source.status != target.status:
        finding("state_mismatch", "The source and destination lifecycle states differ.")
    source_lines = {line.key: line for line in source.lines}
    target_lines = {line.key: line for line in target.lines}
    source_taxes = {tax.key: tax for tax in source.tax_details}
    target_taxes = {tax.key: tax for tax in target.tax_details}
    if source_lines.keys() != target_lines.keys():
        finding(
            "line_structure_mismatch", "Source and destination line identities differ; explicit mapping is required."
        )
    if source_taxes.keys() != target_taxes.keys():
        finding(
            "tax_structure_mismatch",
            "Tax jurisdiction/code identities differ; amount equality does not establish correctness.",
        )

    def difference(field, expected, actual):
        if expected != actual:
            differences.append(
                TransactionDifference(field=field, source=expected, target=actual, delta=expected - actual)
            )

    for field in _AMOUNTS:
        difference(field, getattr(source, field), getattr(target, field))
    for key in sorted(source_lines.keys() & target_lines.keys()):
        for field in ("quantity", "net", "tax"):
            difference(f"lines.{key}.{field}", getattr(source_lines[key], field), getattr(target_lines[key], field))
    for key in sorted(source_taxes.keys() & target_taxes.keys()):
        for field in ("basis", "rate", "amount"):
            difference(
                f"tax_details.{key}.{field}", getattr(source_taxes[key], field), getattr(target_taxes[key], field)
            )
    if findings:
        return result("human_review")
    if differences:
        finding("amount_mismatch", "Transaction-currency totals, line amounts/quantities or tax details differ.")
        return result("propose_amount_correction")
    if lookup.celigo_error_id:
        scope = lookup.error_scope
        if scope is None or (
            scope.target_account_id,
            scope.target_subsidiary_id,
            scope.target_record_type,
            scope.kind,
            scope.operation,
        ) != (
            lookup.target_account_id,
            lookup.target_subsidiary_id,
            lookup.target_record_type,
            "duplicate_transaction",
            "create",
        ):
            finding(
                "error_operation_unproven",
                "Order equality only supports resolving a verified duplicate-create error for this destination.",
            )
            return result("human_review")
        finding("matching_transaction", "Fresh detailed equality supports a false-alarm proposal for the linked error.")
        return result("propose_false_alarm_resolution")
    return result("no_action")
