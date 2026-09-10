"""Explicit business projection. Unknown fields can never become API-visible.

Numbers stay lossless decimal strings; pagination metadata is handled separately.
Oversized or malformed known fields fail the read instead of silently truncating
financial evidence. Customer/address fields require explicit create preparation;
auth, HTTP and raw-stage objects are always omitted.
"""

from decimal import Decimal


class ProjectionError(ValueError):
    pass


_ORDER = frozenset(
    """
id number item_total total ship_total state adjustment_total created_at updated_at completed_at
payment_total shipment_state payment_state included_tax_total additional_tax_total tax_total currency
order_total_after_store_credit total_applicable_store_credit deposit_amount order_type line_item_type
customer_type vat_number credit_sale requires_review total_quantity
""".split()
)
_LINE = frozenset(
    """
id variant_id parent_id product_id sku name quantity price amount total subtotal currency cost_price
adjustment_total included_tax_total additional_tax_total tax_total pre_tax_amount discounted_amount
created_at updated_at
""".split()
)
_ADJUSTMENT = frozenset(
    """
id amount label state eligible mandatory included finalized source_id source_type adjustable_id adjustable_type
created_at updated_at tax_rate_id tax_rate rate tax_category_id currency
""".split()
)
_SHIPMENT = frozenset(
    """
id number state cost shipped_at created_at updated_at order_id stock_location_id shipping_method_id
adjustment_total included_tax_total additional_tax_total tax_total pre_tax_amount currency
""".split()
)
_TAX = frozenset(
    """
id name amount rate included show_rate tax_category_id zone_id currency taxable_amount tax_amount
""".split()
)
_VARIANT = frozenset("id product_id sku name price cost_price currency".split())
_BUSINESS = frozenset("id name currency".split())
_PAYMENT = frozenset(
    """
id source_type source_id amount payment_method_id state created_at updated_at exchange_rate currency
""".split()
)
_ADDRESS = frozenset(
    """
name firstname lastname company address1 address2 city zipcode phone state_name state_text state_id
country_id country_iso vat_id reverse_charge_status
""".split()
)
_COUNTRY = frozenset("id iso iso3 name".split())
_STATE = frozenset("id name abbr".split())
_SHIPPING_METHOD = frozenset("id name code".split())
_SHIPPING_RATE = frozenset("id name cost selected shipping_method_id shipping_method_code".split())
_INVENTORY_UNIT = frozenset("id shipment_id variant_id state".split())
_MAX_ITEMS = 500
_MAX_TEXT = 2048


def _scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, Decimal)):
        if isinstance(value, Decimal) and not value.is_finite():
            raise ProjectionError("invalid_business_field")
        text = str(value)
        if len(text) <= _MAX_TEXT:
            return text
    elif isinstance(value, str) and len(value) <= _MAX_TEXT:
        return value
    raise ProjectionError("invalid_business_field")


def _object(value, fields, children=None):
    if not isinstance(value, dict):
        raise ProjectionError("invalid_business_field")
    out = {key: _scalar(value[key]) for key in fields if key in value}
    for key, projector in (children or {}).items():
        if key in value:
            out[key] = None if value[key] is None else projector(value[key])
    return out


def _list(value, projector):
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        raise ProjectionError("invalid_business_field")
    return [projector(item) for item in value]


def _adjustment(value):
    return _object(value, _ADJUSTMENT)


def _tax(value):
    return _object(value, _TAX)


def _line(value, *, include_sync_data=False):
    children = {
        "adjustments": lambda v: _list(v, _adjustment),
        "taxes": lambda v: _list(v, _tax),
        "variant": lambda v: _object(v, _VARIANT),
    }
    if include_sync_data:
        children["inventory_units"] = lambda v: _list(v, lambda item: _object(item, _INVENTORY_UNIT))
    return _object(
        value,
        _LINE | {"batch_id"} if include_sync_data else _LINE,
        children,
    )


def _shipping_rate(value):
    return _object(value, _SHIPPING_RATE, {"shipping_method": lambda v: _object(v, _SHIPPING_METHOD)})


def _shipment(value, *, include_sync_data=False):
    children = {
        "adjustments": lambda v: _list(v, _adjustment),
        "taxes": lambda v: _list(v, _tax),
        "line_items": lambda v: _list(v, lambda item: _line(item, include_sync_data=include_sync_data)),
    }
    if include_sync_data:
        children.update(
            {
                "shipping_rates": lambda v: _list(v, _shipping_rate),
                "selected_shipping_rate": _shipping_rate,
                "shipping_method": lambda v: _object(v, _SHIPPING_METHOD),
                "shipping_methods": lambda v: _list(v, lambda item: _object(item, _SHIPPING_METHOD)),
            }
        )
    return _object(
        value,
        _SHIPMENT | {"stock_location_name"} if include_sync_data else _SHIPMENT,
        children,
    )


def project_order(value: dict, *, include_sync_data=False) -> dict:
    if type(include_sync_data) is not bool:
        raise ProjectionError("invalid_projection_mode")
    projected = _object(
        value,
        _ORDER,
        {
            "line_items": lambda v: _list(v, lambda item: _line(item, include_sync_data=include_sync_data)),
            "adjustments": lambda v: _list(v, _adjustment),
            "taxes": lambda v: _list(v, _tax),
            "shipments": lambda v: _list(v, lambda item: _shipment(item, include_sync_data=include_sync_data)),
            "payments": lambda v: _list(v, lambda payment: _object(payment, _PAYMENT)),
            "business_entity": lambda v: _object(v, _BUSINESS) if isinstance(v, dict) else _scalar(v),
        },
    )
    if include_sync_data:
        projected.update(
            _object(
                value,
                frozenset({"email", "batch_name"}),
                {
                    key: lambda v: _object(
                        v, _ADDRESS, {"country": lambda c: _object(c, _COUNTRY), "state": lambda s: _object(s, _STATE)}
                    )
                    for key in ("bill_address", "ship_address")
                },
            )
        )
    return projected
