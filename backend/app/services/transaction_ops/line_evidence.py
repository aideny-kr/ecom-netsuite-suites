"""Identity-bound observed unit values; no financial treatment is inferred."""

from collections import Counter
from decimal import localcontext

from app.schemas.transaction_ops import _decimal


def number(value):
    try:
        return _decimal(value)
    except (TypeError, ValueError):
        return None


def tax_observation(line, target):
    """Source adjustment amounts versus the integration's custom VAT field.

    Neither is native line-tax allocation or proof of tax legality. Preserve
    recalculation flags without turning them into accounting authority.
    """
    adjustments = line.get("adjustments")
    if not isinstance(adjustments, list) or any(not isinstance(a, dict) for a in adjustments):
        return None
    taxes = [a for a in adjustments if isinstance(a, dict) and a.get("source_type") == "Spree::TaxRate"]
    if not taxes or len({str(a.get("id")) for a in taxes}) != len(taxes):
        return None
    if any(
        not str(a.get("id", "")).isdigit()
        or a.get("adjustable_type") != "Spree::LineItem"
        or str(a.get("adjustable_id")) != str(line.get("id"))
        or number(a.get("amount")) is None
        or a.get("eligible") is False
        for a in taxes
    ):
        return None
    custom_vat = number(target.get("custcol_fw_vat_amount"))
    with localcontext() as ctx:
        ctx.prec = 60
        amount = sum(number(a["amount"]) for a in taxes)
        return {
            "source_adjustment_amount": str(amount),
            "target_custom_vat_amount": str(custom_vat) if custom_vat is not None else None,
            "delta": str(amount - custom_vat) if custom_vat is not None else None,
            "source_adjustments": [{k: a.get(k) for k in ("id", "amount", "finalized", "updated_at")} for a in taxes],
            "basis": "Source adjustments and ERP integration VAT field; not native tax allocation or tax authority.",
        }


def compare_source_lines(source, evidence):
    """Compare order line prices, never the product catalog's current price.

    Custom source IDs are used only inside the collector's verified case scope.
    SKU corroborates identity; it never replaces a missing or duplicated ID.
    Rate deltas are observed values, not automatic credit or GL instructions.
    """
    result = {
        "changes": [],
        "unverified": [],
        "interpretation": "Observed unit values only. Establish tax-inclusive/exclusive basis, "
        "adjustments, applications and current source authority before choosing a correction. "
        "Catalog prices do not replace the actual order line price.",
    }
    lines = source.get("line_items")
    if not evidence.get("verified_connection_scope") or not isinstance(lines, list):
        result["unverified"].append("scoped_complete_source_lines_required")
        return result
    sections = evidence.get("sections") or {}
    order = sections.get("sales_order") or {}
    if source.get("number") != order.get("tranId") or source.get("currency") != order.get("currency_code"):
        result["unverified"].append("source_order_scope_mismatch")
        return result
    source_ids = Counter(str(line.get("id")) for line in lines if isinstance(line, dict))
    documents = [order] + (sections.get("posting_documents") or [])
    for document in documents:
        detail = document.get("line_evidence") or {}
        native = detail.get("lines")
        if detail.get("complete") is not True or not isinstance(native, list):
            result["unverified"].append(f"{document.get('record_type')}:{document.get('id')}:incomplete_lines")
            continue
        native_ids = Counter(str(line.get("custcol_fw_solidus_line_id")) for line in native if isinstance(line, dict))
        for line in lines:
            if not isinstance(line, dict):
                continue
            identifier = str(line.get("id"))
            if not identifier.isdigit() or source_ids[identifier] != 1 or native_ids[identifier] != 1:
                result["unverified"].append(f"{document.get('id')}:source_line:{identifier}:identity_not_unique")
                continue
            target = next(row for row in native if str(row.get("custcol_fw_solidus_line_id")) == identifier)
            variant = line.get("variant") or {}
            sku = line.get("sku") or (variant.get("sku") if isinstance(variant, dict) else None)
            # A source bundle may expand to a different ERP component SKU.
            # The account's explicit original-ecommerce SKU preserves that mapping.
            target_sku = target.get("custcol_fw_original_ecom_sku") or target.get("custcol_fw_item_sku")
            quantity, target_quantity = number(line.get("quantity")), number(target.get("quantity"))
            price, target_price = number(line.get("price")), number(target.get("rate"))
            if not sku or sku != target_sku or None in (quantity, target_quantity, price, target_price):
                result["unverified"].append(f"{document.get('id')}:source_line:{identifier}:values_or_sku_unverified")
                continue
            if price == target_price and quantity == target_quantity:
                continue
            with localcontext() as context:
                context.prec = 60
                result["changes"].append(
                    {
                        "source_line_id": identifier,
                        "sku": sku,
                        "target_record_type": document.get("record_type"),
                        "target_record_id": document.get("id"),
                        "target_line": target.get("line"),
                        "target_line_unique_key": target.get("lineUniqueKey"),
                        "source_unit_price": str(price),
                        "target_unit_price": str(target_price),
                        "unit_price_delta": str(price - target_price),
                        "source_quantity": str(quantity),
                        "target_quantity": str(target_quantity),
                        "source_updated_at": line.get("updated_at") or source.get("updated_at"),
                        "tax_observation": tax_observation(line, target),
                    }
                )
    return result


def source_revision_delta(source, evidence, record_id):
    """Prove the arithmetic of same-quantity source line repricing.

    This is a reusable evidence basis for planning, not a write candidate. It
    neither authorizes changing an issued document nor ignores existing credits.
    Tax legality and the commercial reason remain outside this proof.
    """
    comparison = compare_source_lines(source, evidence)
    identifier = str(record_id)
    if comparison["unverified"]:
        return None
    sections = evidence.get("sections") or {}
    documents = [sections.get("sales_order") or {}, *(sections.get("posting_documents") or [])]
    matches = [d for d in documents if str(d.get("id")) == identifier]
    if len(matches) != 1:
        return None
    document = matches[0]
    changes = [c for c in comparison["changes"] if str(c["target_record_id"]) == identifier]
    source_lines = source.get("line_items") or []
    native_lines = (document.get("line_evidence") or {}).get("lines") or []
    try:
        if (
            not changes
            or source.get("state") != "complete"
            or source.get("requires_review") is not False
            or not source.get("updated_at")
            or len(native_lines) != len(source_lines)
            or number(source.get("included_tax_total")) != 0
            or number(source.get("ship_total")) != 0
            or number(document.get("shippingCost")) != 0
            or number(document.get("discountTotal")) != 0
            or source.get("adjustments") != []
            or any(
                c["source_quantity"] != c["target_quantity"]
                and number(c["source_quantity"]) != number(c["target_quantity"])
                for c in changes
            )
        ):
            return None
        with localcontext() as ctx:
            ctx.prec = 60
            item_delta = sum(number(c["unit_price_delta"]) * number(c["source_quantity"]) for c in changes)
            source_items = sum(number(l["price"]) * number(l["quantity"]) for l in source_lines)
            target_items = sum(number(l["amount"]) for l in native_lines)
            gross_delta = number(source["total"]) - number(document["total"])
            tax_delta = number(source["tax_total"]) - number(document["taxTotal"])
            if (
                source_items != number(source["item_total"])
                or target_items != number(document["subtotal"])
                or source_items - target_items != item_delta
                or number(source["total"]) != source_items + number(source["tax_total"])
                or number(document["total"]) != target_items + number(document["taxTotal"])
                or gross_delta != item_delta + tax_delta
            ):
                return None
            return {
                "status": "source_line_repricing_arithmetic_observed",
                "record_id": identifier,
                "source_revision": source["updated_at"],
                "currency": source["currency"],
                "net_delta": str(item_delta),
                "tax_delta": str(tax_delta),
                "gross_delta": str(gross_delta),
                "source_line_ids": [c["source_line_id"] for c in changes],
                "authority": "Observed source revision arithmetic only. Inspect existing credits/applications and "
                "account policy before choosing a treatment. Source authority and tax legality are not certified.",
            }
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
