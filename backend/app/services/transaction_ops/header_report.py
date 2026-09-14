"""Retain known header observations while keeping repair snapshots incomplete."""

import re

from app.schemas.transaction_ops import TransactionLookup, TransactionSnapshot, _decimal
from app.services.transaction_ops.comparison import compare_transactions
from app.services.transaction_ops.normalization import _time, source_entity_key


def _amount(value):
    try:
        return _decimal(value)
    except ValueError:
        return None


def _currency(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) else None


def build_header_report(source_evidence, target_evidence, config, mapping, *, now):
    orders = source_evidence.get("orders") or []
    if (source_evidence.get("source"), source_evidence.get("scope"), source_evidence.get("page_complete")) != (
        "framework",
        "order",
        True,
    ) or len(orders) != 1:
        raise ValueError("source_scope_mismatch")
    order = orders[0]
    if not order.get("id") or not order.get("number"):
        raise ValueError("source_identity_unavailable")
    subsidiary = mapping.business_entity_subsidiaries.get(source_entity_key(order))
    source = TransactionSnapshot(
        system="framework",
        account_id="frame.work",
        record_id=str(order["id"]),
        record_type="order",
        order_reference=order["number"],
        currency=_currency(order.get("currency")),
        currency_minor_unit=mapping.currency_minor_units.get(order.get("currency")),
        subsidiary_id=subsidiary if subsidiary == config["subsidiary_id"] else None,
        observed_at=_time(source_evidence["read_at"]),
        updated_at=_time(order.get("updated_at")),
        authoritative=True,
        amount_basis="transaction",
        total=_amount(order.get("total")),
        subtotal=_amount(order.get("item_total")),
        tax=_amount(order.get("tax_total")),
    )
    account = config["netsuite_account_id"].replace("_", "-").lower()
    targets = []
    for item in target_evidence.get("orders") or []:
        header, metadata = item.get("header") or {}, item.get("currency_metadata") or {}
        proven = item.get("header_complete") is True
        targets.append(
            TransactionSnapshot(
                system="netsuite",
                account_id=account,
                record_id=str(item["record_id"]),
                record_type="salesorder",
                order_reference=item["order_reference"],
                observed_at=_time(target_evidence["observed_at"]),
                authoritative=proven and target_evidence.get("provider") == "netsuite",
                amount_basis="transaction",
                subsidiary_id=str((header.get("subsidiary") or {}).get("id")) if proven else None,
                currency=_currency(metadata.get("symbol")) if proven else None,
                currency_minor_unit=metadata.get("currencyPrecision") if proven else None,
                total=_amount(header.get("total")) if proven else None,
                subtotal=_amount(header.get("subtotal")) if proven else None,
                tax=_amount(header.get("taxTotal")) if proven else None,
            )
        )
    lookup = TransactionLookup(
        source_system=source.system,
        source_account_id=source.account_id,
        source_record_id=source.record_id,
        order_reference=source.order_reference,
        target_account_id=account,
        target_subsidiary_id=config["subsidiary_id"],
        target_record_type=config["record_type"],
        complete=(target_evidence.get("lookup") or {}).get("complete") is True,
        authoritative=target_evidence.get("provider") == "netsuite",
        observed_at=_time(target_evidence["observed_at"]),
    )
    return {
        "order_reference": source.order_reference,
        "source": source.model_dump(mode="json"),
        "targets": [target.model_dump(mode="json") for target in targets],
        "lookup": lookup.model_dump(mode="json"),
        "comparison": compare_transactions(source, targets, lookup, now=now).model_dump(mode="json"),
        "evidence_limits": {"code": "detailed_evidence_unavailable"},
        "source_provenance": {
            key: value
            for key, value in source_evidence.items()
            if key in {"source", "scope", "read_at", "celigo_step_id", "connection_id"}
        },
        "netsuite_provenance": {"scope": target_evidence["scope"], "observed_at": target_evidence["observed_at"]},
    }
