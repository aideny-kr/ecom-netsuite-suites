"""Bounded, read-only native accounting evidence. No model-generated SQL or writes."""

from datetime import datetime, timezone

from app.services.transaction_ops.netsuite_reader import (
    NetSuiteEvidenceError,
    _collection,
    _id,
    authenticated_reader,
)


def _ref(value):
    return _id(value.get("id")) if isinstance(value, dict) else _id(value)


def _project(record, fields):
    # Keep native labels, booleans and amounts, but exclude customer PII and arbitrary custom fields.
    result = {}
    for key in fields:
        if key in record:
            value = record[key]
            result[key] = {k: value[k] for k in ("id", "refName") if k in value} if isinstance(value, dict) else value
    return result


DOCUMENT_FIELDS = (
    "id",
    "tranId",
    "status",
    "subsidiary",
    "currency",
    "exchangeRate",
    "createdFrom",
    "subtotal",
    "total",
    "taxTotal",
    "taxRate",
    "taxItem",
    "postingPeriod",
    "tranDate",
    "shipCountry",
    "lastModifiedDate",
    "amountPaid",
    "amountRemaining",
    "payment",
    "salesOrder",
    "taxDetailsOverride",
    "discountTotal",
    "discountRate",
    "discountItem",
    "shippingCost",
    "entity",
    "account",
    "location",
    "department",
    "class",
)


async def collect_accounting_evidence(db, tenant_id, review, report):
    """Read one case's native evidence chain, preserving partial failures and scope conflicts."""
    scope = review.get("scope") or {}
    targets = (review.get("observed_scope") or {}).get("target_records") or []
    result = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "sections": {},
        "blockers": [],
        "assessment": {
            "root_cause": "not_verified",
            "native_tax_regime": "not_verified",
            "tax_legality": "not_verified",
            "unapplied_deposit_balance": "not_verified",
            "correction_ready": False,
            "executable_proposal": None,
        },
        "interpretation": (
            "Use native status labels verbatim. A default tax-item rate of zero does not prove the transaction's "
            "tax engine or integration root cause. Compare its transaction tax rate separately. A deposit and one "
            "application do not prove the current unapplied balance. Equal tax/gross variances are one gap, not two. "
            "Do not mark matched or settled from a historical mapping hypothesis. Reconciliation and exact-change "
            "human approval remain required. Continue targeted missing-evidence reads without asking discretionary "
            "permission; do not repeat successful sections or failed guessed SQL fields."
            " Line totals need not sum to the order header: order-level discounts, shipping and adjustments "
            "must be included. An internally balanced invoice/GL or Paid In Full status does not prove the "
            "invoice amount is economically correct. Inspect applied credits and payment applications. "
            "No supported automated correction candidate is a capability limit, not proof that no error exists."
        ),
    }
    if review.get("configuration_status") == "ambiguous":
        result["blockers"].append("ambiguous_reconciliation_configuration")
        result["interpretation"] = review.get("read_only_next_step") or (
            "Multiple reconciliation configurations match this case. Resolve the exact connection scope "
            "before native reads or any correction proposal."
        )
        return result
    if (
        review.get("configuration_status") != "scoped_configuration_found"
        or not review.get("connection_active")
        or (review.get("observed_scope") or {}).get("status") != "consistent_in_stored_observation"
        or scope.get("record_type") != "salesorder"
        or len(targets) != 1
        or not _id(targets[0].get("record_id"))
        or not _id(scope.get("subsidiary_id"))
    ):
        result["blockers"].append("verified_unique_sales_order_scope_required")
        return result
    order_id = _id(targets[0]["record_id"])
    subsidiary = str(scope["subsidiary_id"])
    expected_currency = (report.get("source") or {}).get("currency")

    async with authenticated_reader(
        db, tenant_id, review["netsuite_connection_id"], scope["netsuite_account_id"], max_api_calls=20
    ) as reader:
        result["verified_connection_scope"] = {
            "connection_id": review["netsuite_connection_id"],
            "account_id": scope["netsuite_account_id"],
        }

        async def read(label, method, path, **kwargs):
            try:
                return await reader.request(method, path, **kwargs)
            except NetSuiteEvidenceError as exc:
                result["blockers"].append(f"{label}:{exc}")
                return None

        async def query(label, sql, limit=30):
            body = await read(label, "POST", "/query/v1/suiteql", params={"limit": limit, "offset": 0}, body={"q": sql})
            if body is None:
                return None
            try:
                rows, complete = _collection(body)
            except NetSuiteEvidenceError:
                result["blockers"].append(f"{label}:invalid_collection")
                return None
            if not complete:
                result["blockers"].append(f"{label}:incomplete_collection")
            return {"complete": complete, "rows": rows}

        async def document(kind, identifier, label):
            raw = await read(label, "GET", f"/record/v1/{kind}/{identifier}", params={"expandSubResources": "true"})
            if raw is None:
                return None
            if _ref(raw.get("id")) != identifier or _ref(raw.get("subsidiary")) != subsidiary:
                result["blockers"].append(f"{label}:identity_conflict")
                return None
            currency_id = _ref(raw.get("currency"))
            if not currency_id:
                result["blockers"].append(f"{label}:currency_missing")
                return None
            try:
                currency = await reader.currency(currency_id)
            except NetSuiteEvidenceError as exc:
                result["blockers"].append(f"{label}:currency:{exc}")
                return None
            if currency.get("symbol") != expected_currency:
                result["blockers"].append(f"{label}:currency_conflict")
                return None
            projected = _project(raw, DOCUMENT_FIELDS)
            projected["currency_code"] = currency["symbol"]
            projected["record_type"] = kind.lower()
            return projected

        order = await document("salesOrder", order_id, "sales_order")
        if order is None:
            return result
        if order.get("tranId") != report.get("order_reference"):
            result["blockers"].append("native_order_reference_requires_mapping_verification")
            return result
        result["sections"]["sales_order"] = order
        links = await query(
            "linked_documents",
            "SELECT DISTINCT t.id, t.tranid, t.type, BUILTIN.DF(t.status) AS status_name "
            "FROM transaction t JOIN transactionline tl ON tl.transaction = t.id "
            f"WHERE tl.createdfrom = {order_id} AND t.subsidiary = {subsidiary}",
        )
        if links is None:
            return result
        result["sections"]["linked_documents"] = links
        invoices = [r for r in links["rows"] if r.get("type") in {"CustInvc", "CashSale"}]
        deposits = [r for r in links["rows"] if r.get("type") == "CustDep"]
        if not invoices:
            result["blockers"].append("no_linked_posting_sale_observed")
        if len(invoices) > 2 or len(deposits) > 2:
            result["blockers"].append("additional_documents_require_targeted_read")
        result["sections"]["posting_documents"] = []
        seen_tax, seen_period = set(), set()
        for row in invoices[:2]:
            identifier = _id(row.get("id"))
            if not identifier:
                result["blockers"].append("invalid_linked_document_id")
                continue
            kind = "invoice" if row["type"] == "CustInvc" else "cashSale"
            doc = await document(kind, identifier, f"{kind}:{identifier}")
            if doc is None:
                continue
            if _ref(doc.get("createdFrom")) != order_id:
                result["blockers"].append(f"{kind}:{identifier}:origin_conflict")
                continue
            result["sections"]["posting_documents"].append(doc)
            for field, record_type, fields, seen in (
                ("taxItem", "salesTaxItem", ("id", "itemId", "rate", "isInactive", "taxAgency"), seen_tax),
                (
                    "postingPeriod",
                    "accountingPeriod",
                    ("id", "periodName", "closed", "arLocked", "apLocked", "allLocked", "allowNonGLChanges"),
                    seen_period,
                ),
            ):
                ref = _ref(doc.get(field))
                if not ref:
                    result["blockers"].append(f"{identifier}:{field}:not_available")
                elif ref not in seen:
                    seen.add(ref)
                    raw = await read(field, "GET", f"/record/v1/{record_type}/{ref}")
                    if raw is not None and _ref(raw.get("id")) == ref:
                        result["sections"].setdefault(field, []).append(_project(raw, fields))
                    elif raw is not None:
                        result["blockers"].append(f"{field}:identity_conflict")
            gl = await query(
                f"gl:{identifier}",
                "SELECT tal.account, BUILTIN.DF(tal.account) AS account_name, "
                "tal.accountingbook, tal.debit, tal.credit "
                "FROM transactionaccountingline tal JOIN transaction t ON t.id = tal.transaction "
                f"WHERE tal.transaction = {identifier} AND t.subsidiary = {subsidiary}",
            )
            if gl is not None:
                result["sections"].setdefault("gl", {})[identifier] = gl
        for row in deposits[:2]:
            identifier = _id(row.get("id"))
            if not identifier:
                result["blockers"].append("invalid_deposit_id")
                continue
            deposit = await document("customerDeposit", identifier, f"deposit:{identifier}")
            if deposit is not None and _ref(deposit.get("salesOrder")) == order_id:
                result["sections"].setdefault("deposits", []).append(deposit)
            elif deposit is not None:
                result["blockers"].append(f"deposit:{identifier}:origin_conflict")
        result["native_api_calls"] = reader.calls
    result["blockers"].extend(
        [
            "all_deposit_applications_and_reversals_not_verified",
            "tax_regime_jurisdiction_and_integration_mapping_not_verified",
            "accounting_treatment_and_supported_posted_adjustment_not_established",
        ]
    )
    return result
