"""Explicit fields for native amendments; legacy recipes retain their contract."""

LEGACY_FIELDS = {
    "order_reference": "custbody_fw_order_number",
    "source_line_id": "custcol_fw_solidus_line_id",
    "original_sku": "custcol_fw_original_ecom_sku",
    "vat_amount": "custcol_fw_vat_amount",
}


def resolve(field_map):
    # Only historical callers may omit a map. New native execution requires an
    # enabled, tenant/account-bound profile and must always pass its exact map.
    if field_map is None:
        return LEGACY_FIELDS
    from app.services.transaction_ops.native_accounting_profile import NativeFieldMap

    return NativeFieldMap.model_validate(field_map).model_dump()
