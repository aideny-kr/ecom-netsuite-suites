"""Exact source-line amendment intents, separate from posting corrections.

These observations cannot execute. A subsequent approved operation must prove
native save behavior, protect fulfilled/billed quantities and verify that linked
posting documents, applications and inventory were unchanged.
"""

from decimal import ROUND_HALF_UP, Decimal, localcontext

from app.services.transaction_ops.line_evidence import (
    compare_source_lines,
    number,
    source_revision_delta,
    source_tax_refund_delta,
)


def build_intent(source, evidence, posting_intent, *, field_map=None):
    from app.services.transaction_ops.accounting_field_map import resolve

    fields = resolve(field_map)
    order = (evidence.get("sections") or {}).get("sales_order") or {}
    if (
        not posting_intent
        or posting_intent.get("kind") != "credit_tax_reallocation"
        or str(posting_intent.get("sales_order_id")) != str(order.get("id"))
    ):
        return None
    basis = source_revision_delta(source, evidence, order.get("id"), field_map=field_map) or (
        source_tax_refund_delta(source, evidence, order.get("id")) if posting_intent.get("tax_only") else None
    )
    if not basis:
        return None
    changes = [
        c
        for c in compare_source_lines(source, evidence, field_map=field_map)["changes"]
        if c["target_record_id"] == order["id"]
    ]
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            subtotal, tax, total = (number(source[k]) for k in ("item_total", "tax_total", "total"))
            if subtotal <= 0 or tax < 0 or total != subtotal + tax + number(source["ship_total"]):
                return None
            rate = (tax / subtotal * 100).quantize(Decimal(".0000001"), rounding=ROUND_HALF_UP)
            if (subtotal * rate / 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) != tax:
                return None
            amended, identities = [], []
            if posting_intent.get("tax_only"):
                tax_changes = _tax_only_changes(source, order, field_map=field_map)
                if tax_changes is None:
                    return None
                amended, identities = tax_changes
            for change in [] if posting_intent.get("tax_only") else changes:
                observation = change.get("tax_observation") or {}
                line_tax = number(observation.get("source_adjustment_amount"))
                if line_tax is None or number(observation.get("delta")) is None:
                    return None
                if (
                    not str(change.get("target_line", "")).isdigit()
                    or not str(change.get("target_line_unique_key", "")).isdigit()
                ):
                    return None
                price, quantity = number(change["source_unit_price"]), number(change["source_quantity"])
                if price < 0 or quantity <= 0:
                    return None
                amended.append(
                    {
                        "line": change["target_line"],
                        "rate": str(price),
                        "amount": str(price * quantity),
                        fields["vat_amount"]: str(line_tax),
                    }
                )
                identities.append(
                    {k: change[k] for k in ("source_line_id", "target_line", "target_line_unique_key", "sku")}
                )
            return {
                "kind": "sales_order_line_alignment",
                "status": "intent_requires_schema_policy_and_preflight_validation",
                "record_type": "salesorder",
                "record_id": str(order["id"]),
                "currency": source["currency"],
                "tax_only": bool(posting_intent.get("tax_only")),
                "required_transport": "native_accounting_amendment_with_tax_preview",
                "source_revision": source["updated_at"],
                "before": {k: order.get(k) for k in ("id", "lastModifiedDate", "subtotal", "taxTotal", "total")},
                "proposed_fields": {"taxRate": str(rate), **({"item": {"items": amended}} if amended else {})},
                "line_identities": identities,
                "expected_after": {"subtotal": str(subtotal), "taxTotal": str(tax), "total": str(total)},
                "depends_on": {
                    "kind": posting_intent["kind"],
                    "record_id": posting_intent["record_id"],
                    "required_status": "verified",
                },
                "approval_basis": (
                    "Align only the identified sales-order integration VAT values with "
                    if posting_intent.get("tax_only")
                    else "Align only the identified sales-order prices and integration VAT values with "
                )
                + "the reviewed source revision, after verifying the related posting correction. Preserve quantities, "
                "fulfillment, billing, inventory, classifications and existing invoice/credit/refund applications. "
                "The effective header rate reproduces source tax; it does not select a statutory tax rate.",
                "remaining_requirements": [
                    "Verify the posting correction before preparing the dependent approval.",
                    "Verify native keyed-line save behavior and protect all unaffected fields and records.",
                    "After an approved save, independently reconcile order, net postings, tax and refunds.",
                ],
                "affects_gl": False,
                "executable": False,
                "financial_write_authorized": False,
            }
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def _tax_only_changes(source, order, *, field_map=None):
    """Match unchanged extended values for tax allocation only, never reprice kits.

    A missing integration line ID can use a unique, explicitly stored original
    ecommerce SKU within this already verified order. Conflicting IDs, repeated
    SKUs or unequal extended values are never silently resolved by this fallback.
    """
    from app.services.transaction_ops.accounting_field_map import resolve

    fields = resolve(field_map)
    from collections import Counter

    from app.services.transaction_ops.line_evidence import tax_observation

    source_lines = source.get("line_items") or []
    native = (order.get("line_evidence") or {}).get("lines") or []

    def source_sku(line):
        return line.get("sku") or (line.get("variant") or {}).get("sku")

    counts = Counter(source_sku(line) for line in source_lines)
    changes, identities, used = [], [], set()
    source_tax = Decimal(0)
    for line in source_lines:
        sku = source_sku(line)
        by_id = [n for n in native if str(n.get(fields["source_line_id"])) == str(line.get("id"))]
        matches = by_id or [
            n
            for n in native
            if n.get(fields["source_line_id"]) in (None, "")
            and n.get(fields["original_sku"]) == sku
            and counts[sku] == 1
        ]
        if len(matches) != 1 or not sku:
            return None
        target = matches[0]
        key = str(target.get("lineUniqueKey"))
        if (
            target.get(fields["original_sku"]) != sku
            or not key.isdigit()
            or key in used
            or not str(target.get("line", "")).isdigit()
            or number(line["price"]) * number(line["quantity"]) != number(target.get("amount"))
        ):
            return None
        detail = tax_observation(line, target, field_map=field_map)
        if not detail or number(detail["delta"]) is None:
            return None
        source_tax += number(detail["source_adjustment_amount"])
        used.add(key)
        if number(detail["delta"]) != 0:
            changes.append({"line": target["line"], fields["vat_amount"]: detail["source_adjustment_amount"]})
            identities.append(
                {
                    "source_line_id": str(line["id"]),
                    "target_line": target["line"],
                    "target_line_unique_key": key,
                    "sku": sku,
                    "identity_basis": "source_line_id_and_original_sku"
                    if by_id
                    else "unique_original_sku_and_equal_extended_value",
                    "native_quantity_preserved": str(target["quantity"]),
                    "native_rate_preserved": str(target["rate"]),
                }
            )
    for line in native:
        if str(line.get("lineUniqueKey")) not in used and (
            number(line.get("amount")) != 0
            or number(line.get("rate")) != 0
            or (line.get(fields["vat_amount"]) is not None and number(line[fields["vat_amount"]]) != 0)
        ):
            return None
    if source_tax != number(source["tax_total"]):
        return None
    return changes, identities
