"""Prove the narrow zero-charge service/RMA pattern without changing tax records."""

from decimal import Decimal, DecimalException

from app.schemas.transaction_ops import _decimal


def service_order_offset(source, header, tax, precision):
    def money(value):
        amount = _decimal(value)
        if amount is None or amount != amount.quantize(Decimal(1).scaleb(-precision)):
            raise ValueError("incomplete_service_amount")
        return amount

    def adjustments(row, kind):
        entries = row["adjustments"]
        if not isinstance(entries, list):
            raise ValueError("incomplete_service_adjustments")
        total = Decimal(0)
        for entry in entries:
            identifier = entry["id"]
            if (
                not isinstance(identifier, str)
                or not identifier.isdigit()
                or identifier in seen
                or entry["adjustable_type"] != kind
                or entry["adjustable_id"] != row["id"]
                or entry["source_type"] != "Spree::TaxRate"
                or entry["finalized"] is not True
                or entry.get("eligible") is False
                or ("included" in entry and entry["included"] is not (included > 0))
            ):
                raise ValueError("unproven_service_tax")
            seen.add(identifier)
            amount = money(entry["amount"])
            if amount < 0:
                raise ValueError("negative_service_tax")
            total += amount
        return total

    try:
        if source.get("order_type") != "service" or tax is None or tax <= 0:
            return None
        if (
            any(
                money(source[key]) != 0
                for key in (
                    "total",
                    "payment_total",
                    "deposit_amount",
                    "total_applicable_store_credit",
                    "order_total_after_store_credit",
                )
            )
            or source["payments"] != []
        ):
            return None
        items, shipping = money(source["item_total"]), money(source["ship_total"])
        included, additional = money(source["included_tax_total"]), money(source["additional_tax_total"])
        if (
            included < 0
            or additional < 0
            or included + additional != tax
            or (included and additional)
            or included > shipping
        ):
            return None
        if items < 0 or shipping < 0 or money(source["adjustment_total"]) != -items - shipping:
            return None
        offsets = source["adjustments"]
        if not isinstance(offsets, list) or not 1 <= len(offsets) <= 100:
            return None
        seen, line_ids, shipment_ids = set(), set(), set()
        offset_total = Decimal(0)
        for offset in offsets:
            amount = money(offset["amount"])
            if (
                offset["finalized"] is not True
                or offset.get("eligible") is False
                or offset["source_type"] is not None
                or offset["adjustable_type"] != "Spree::Order"
                or offset["adjustable_id"] != source["id"]
                or not isinstance(offset["id"], str)
                or not offset["id"].isdigit()
                or offset["id"] in seen
                or amount >= 0
            ):
                return None
            seen.add(offset["id"])
            offset_total += amount
        if offset_total != -(items + shipping + additional):
            return None
        line_total = shipping_total = shipping_tax = Decimal(0)
        if not source["line_items"] or not source["shipments"]:
            return None
        for line in source["line_items"]:
            quantity, price, total = money(line["quantity"]), money(line["price"]), money(line["total"])
            if (
                line["id"] in line_ids
                or quantity <= 0
                or price < 0
                or quantity * price != total
                or adjustments(line, "Spree::LineItem") != 0
            ):
                return None
            line_ids.add(line["id"])
            line_total += total
        for shipment in source["shipments"]:
            cost = money(shipment["cost"])
            if shipment["id"] in shipment_ids or cost < 0:
                return None
            shipment_ids.add(shipment["id"])
            shipping_total += cost
            shipping_tax += adjustments(shipment, "Spree::Shipment")
        if (line_total, shipping_total, shipping_tax) != (items, shipping, tax):
            return None
        if (
            any(
                money(header[key]) != 0
                for key in ("total", "taxTotal", "shippingCost", "custbody_fw_solidus_order_total")
            )
            or ("handlingCost" in header and money(header["handlingCost"]) != 0)
            or money(header["subtotal"]) != items
            or money(header["discountTotal"]) != -items
            or money(header["discountRate"]) != -100
            or not header["discountItem"]["id"]
            or money(header["custbody_fw_solidus_tax_amount"]) != tax
        ):
            return None
        return {
            "kind": "service_order_full_offset",
            "source_adjustment_id": offsets[0]["id"] if len(offsets) == 1 else None,
            "source_adjustment_ids": [offset["id"] for offset in offsets],
            "source_adjustment_amount": f"{offset_total:.{precision}f}",
            "source_shipping_tax_offset": f"{tax:.{precision}f}",
            "target_record_id": header["id"],
            "target_discount_item_id": header["discountItem"]["id"],
            "target_discount_amount": f"{-items:.{precision}f}",
            "explanation": "The finalized service adjustment offsets all items, shipping and shipping tax; "
            "NetSuite has a full item discount, free shipping and zero tax.",
        }
    except (KeyError, TypeError, ValueError, DecimalException):
        return None
