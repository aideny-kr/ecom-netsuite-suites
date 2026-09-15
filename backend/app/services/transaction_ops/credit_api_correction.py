"""Existing-credit amendments through the connected MCP record API.

No transport or save lives here: the normal signed confirmation dispatcher
owns execution. Schema discovery, fresh evidence and readback stay explicit.
"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from app.services.transaction_ops import case_resolution_scope
from app.services.transaction_ops.credit_reallocation import build_intent, collect_support
from app.services.transaction_ops.native_accounting_service import _stable, _verify_ledger
from app.services.transaction_ops.netsuite_reader import authenticated_reader
from app.services.transaction_ops.resolution_plan import fingerprint


def _json(value):
    """Persist exact decimals as strings, consistently on prepare and re-read."""
    return json.loads(json.dumps(value, default=str))


def typed_fields(raw, fields):
    """Serialize numeric schema fields as JSON numbers without changing value.

    Accounting arithmetic stays Decimal. At the connector boundary require the
    JSON number to round-trip to exactly the same decimal; reject precision loss.
    Reference IDs remain strings and booleans remain booleans.
    """

    def convert(value, spec):
        kind = spec.get("type")
        if kind in ("number", "integer"):
            if isinstance(value, bool):
                raise ValueError("credit_api_invalid_numeric_field")
            amount = Decimal(str(value))
            if not amount.is_finite() or (kind == "integer" and amount != amount.to_integral_value()):
                raise ValueError("credit_api_invalid_numeric_field")
            number = int(amount) if amount == amount.to_integral_value() else float(amount)
            # MCP uses JavaScript numbers: integers must also fit its exact range.
            if abs(amount) > 2**53 - 1 or Decimal(json.dumps(number, allow_nan=False)) != amount:
                raise ValueError("credit_api_numeric_precision_loss")
            return number
        if isinstance(value, dict):
            props = spec.get("properties") or {}
            return {k: convert(v, props.get(k) or {}) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v, spec.get("items") or {}) for v in value]
        return value

    return convert(fields, raw)


def schema_contract(raw, fields):
    props = raw.get("properties") or {}
    item = (((props.get("item") or {}).get("properties") or {}).get("items") or {}).get("items") or {}
    lines = item.get("properties") or {}
    if ((item.get("x-ns-sublistkey") or {}).get("value") or {}).get("existing") != ["line"]:
        raise ValueError("credit_api_line_identity_unverified")
    selected = {}
    for key in fields:
        spec = props.get(key)
        if not spec or spec.get("readOnly") is True:
            raise ValueError("credit_api_field_unavailable:" + key)
        if key == "item":
            for line in fields[key]["items"]:
                for column in line:
                    if not lines.get(column) or lines[column].get("readOnly") is True:
                        raise ValueError("credit_api_line_field_unavailable:" + column)
            selected[key] = {k: lines[k] for k in fields[key]["items"][0]}
        else:
            selected[key] = spec
    return {"existing_item_key": "line", "fields_digest": fingerprint(selected)}


async def prepare(db, tenant_id, intent, evidence, restriction):
    if not intent.get("connector_id") or not case_resolution_scope.allows(restriction, intent):
        return None
    if not (intent["support"]["credit"].get("application_evidence") or {}).get("complete"):
        raise ValueError("credit_applications_unverified")
    async with authenticated_reader(
        db, tenant_id, intent["connection_id"], intent["scope"]["netsuite_account_id"], max_api_calls=1
    ) as reader:
        raw = await reader.request("GET", "/record/v1/metadata-catalog/creditMemo")
    proposal = deepcopy(intent)
    # Keep exact decimal strings in financial evidence and operation identities.
    # JSON numeric tokens belong only in the opaque, signed connector payload.
    proposal["wire_record_json"] = json.dumps(typed_fields(raw, proposal["proposed_fields"]), allow_nan=False)
    proposal.update(
        execution_transport="mcp_record_api",
        connector_schema=schema_contract(raw, proposal["proposed_fields"]),
        protected_sales_order=deepcopy(evidence["sections"]["sales_order"]),
        required_transport="connected_mcp_record_update",
        status="ready_for_exact_human_approval",
        observed_at=datetime.now(timezone.utc).isoformat(),
    )
    if restriction:
        proposal["resolution_scope"] = restriction
    # build_intent already requires a complete balanced ledger proving zero
    # original tax. Retain the original API record in support, and label the
    # display-only derived zero rather than pretending REST supplied it.
    if proposal["before"].get("taxTotal") is None:
        proposal["before"] = deepcopy(proposal["before"])
        proposal["before"]["taxTotal"] = "0.00"
        proposal["before_tax_basis"] = "complete_balanced_credit_ledger"
    proposal["approval_basis"] = proposal["approval_basis"].replace(
        "Sales-order alignment remains a separately approved step.",
        "The invoice and sales order are protected records and will not be amended by this correction. "
        "The connector advertises these fields; save-time calculation is verified by independent readback, "
        "not an unsaved native preview.",
    )
    return _json(proposal)


def review_for_card(db, tenant_id, tool_name, record_type, normalized, *, check_age=True):
    from app.services.chat.tools import parse_external_tool_name

    p = db.info.get("accounting_correction_candidate") or {}
    parsed = parse_external_tool_name(tool_name)
    if not p or p.get("execution_transport") != "mcp_record_api":
        raise ValueError("fresh_credit_api_evidence_required")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(p["observed_at"])).total_seconds()
    if (
        not parsed
        or parsed[1] != "ns_updateRecord"
        or str(parsed[0]) != p["connector_id"]
        or p["tenant_id"] != str(tenant_id)
        or record_type.lower() != "creditmemo"
        or normalized.record_id != p["record_id"]
        or normalized.record != json.loads(p["wire_record_json"])
        or (check_age and not 0 <= age <= 300)
    ):
        raise ValueError("credit_api_proposal_binding_changed")
    return p


async def fresh(db, tenant_id, p):
    from app.services.transaction_ops.accounting_evidence import collect_accounting_evidence
    from app.services.transaction_ops.accounting_review import accounting_context
    from app.services.transaction_ops.case_service import get_case
    from app.services.transaction_ops.tax_correction import refresh_source

    case = await get_case(db, tenant_id, UUID(p["case_id"]))
    review = await accounting_context(db, tenant_id, case.scope_json, case.latest_report_json)
    if any(
        review.get(k) != value
        for k, value in {
            "scope": p["scope"],
            "config_id": p["config_id"],
            "netsuite_connection_id": p["connection_id"],
            "native_mcp_connector_id": p["connector_id"],
            "connection_active": True,
        }.items()
    ):
        raise ValueError("credit_api_connection_scope_changed")
    source = _json(
        await refresh_source(db, tenant_id, p["scope"], p["order_reference"], include_accounting_detail=True)
    )
    if source != p["source"]:
        raise ValueError("credit_api_source_changed")
    evidence = _json(await collect_accounting_evidence(db, tenant_id, review, case.latest_report_json))
    support = _json(await collect_support(db, tenant_id, source, review, evidence))
    if not support:
        raise ValueError("credit_api_subledger_incomplete")
    if _stable(evidence["sections"]["sales_order"]) != _stable(p["protected_sales_order"]):
        raise ValueError("credit_api_protected_sales_order_changed")
    return source, review, evidence, support


async def validate_approved(db, tenant_id, tool_name, tool_input, p):
    from app.services.chat.write_payload import normalize_write_payload

    db.info["accounting_correction_candidate"] = p
    review_for_card(
        db, tenant_id, tool_name, tool_input.get("recordType", ""), normalize_write_payload(tool_input), check_age=False
    )
    await case_resolution_scope.validate(db, tenant_id, p)
    source, review, evidence, support = await fresh(db, tenant_id, p)
    if _stable(support) != _stable(p["support"]):
        raise ValueError("credit_api_subledger_changed")
    rebuilt = build_intent(tenant_id, p["case_id"], source, review, evidence, support)
    if not rebuilt or any(rebuilt[k] != p[k] for k in ("record_id", "expected_after", "expected_ledger")):
        raise ValueError("credit_api_treatment_changed")
    async with authenticated_reader(
        db, tenant_id, p["connection_id"], p["scope"]["netsuite_account_id"], max_api_calls=1
    ) as reader:
        raw = await reader.request("GET", "/record/v1/metadata-catalog/creditMemo")
    if schema_contract(raw, p["proposed_fields"]) != p["connector_schema"]:
        raise ValueError("credit_api_schema_changed")
    if rebuilt["proposed_fields"] != p["proposed_fields"] or typed_fields(
        raw, rebuilt["proposed_fields"]
    ) != json.loads(p["wire_record_json"]):
        raise ValueError("credit_api_treatment_changed")


def verify_evidence(p, support):
    credit = support["credit"]
    for key, expected in p["expected_after"].items():
        if credit.get(key) is None or Decimal(str(credit[key])) != Decimal(expected):
            raise ValueError("credit_api_expected_amount_mismatch:" + key)
    for key in (
        "invoice",
        "refund",
        "refund_graph",
        "invoice_gl",
        "currency",
        "item",
        "tax_item",
        "period",
        "book",
        "ar_account",
        "offset_account",
        "tax_account",
    ):
        if _stable(support[key]) != _stable(p["support"][key]):
            raise ValueError("credit_api_related_record_changed:" + key)
    before = p["support"]["credit"]
    if not (credit.get("application_evidence") or {}).get("complete") or credit["application_evidence"] != before.get(
        "application_evidence"
    ):
        raise ValueError("credit_api_applications_changed")
    for key in ("id", "entity", "account", "subsidiary", "currency", "postingPeriod", "tranDate", "exchangeRate"):
        if credit.get(key) != before.get(key):
            raise ValueError("credit_api_identity_changed:" + key)
    lines = credit.get("line_evidence") or {}
    prior = before["line_evidence"]["lines"]
    if not lines.get("complete") or len(lines.get("lines", [])) != len(prior) or len(prior) != 1:
        raise ValueError("credit_api_line_evidence_incomplete")
    old, after = prior[0], lines["lines"][0]
    amendment = p["proposed_fields"]["item"]["items"][0]
    for key in ("line", "lineUniqueKey", "item", "quantity", "itemType"):
        if old.get(key) != after.get(key):
            raise ValueError("credit_api_line_identity_changed")
    if (
        any(Decimal(str(after[k])) != Decimal(str(amendment[k])) for k in ("amount", "rate"))
        or after.get("isTaxable") is not True
    ):
        raise ValueError("credit_api_line_values_mismatch")
    if credit.get("taxItem") != p["proposed_fields"]["taxItem"]:
        if (credit.get("taxItem") or {}).get("id") != p["proposed_fields"]["taxItem"]["id"]:
            raise ValueError("credit_api_tax_item_changed")
    _verify_ledger(p, support)


async def verify_after(db, tenant_id, p, receipt=None):
    try:
        if isinstance(receipt, dict) and any(
            str(receipt[k]) != p["record_id"] for k in ("id", "recordId", "internalId") if receipt.get(k)
        ):
            raise ValueError("credit_api_receipt_identity_conflict")
        _, _, evidence, support = await fresh(db, tenant_id, p)
        verify_evidence(p, support)
        credit = support["credit"]
        return {
            "status": "verified",
            "record_type": "creditmemo",
            "record_id": p["record_id"],
            "credit_memo_id": p["record_id"],
            "invoice": support["invoice"],
            "sales_order": evidence["sections"]["sales_order"],
            "after": {
                "body": {"subtotal": credit["subtotal"], "taxtotal": credit["taxTotal"], "total": credit["total"]}
            },
            "ledger": support["credit_gl"],
            "related_records_unchanged": True,
            "retry_allowed": False,
            "cash_settlement": "not_verified",
            "case_settlement": "not_verified",
        }
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return {"status": "needs_review", "reason": str(exc), "retry_allowed": False}
