"""Bounded, read-only native accounting evidence. No model-generated SQL or writes."""

from datetime import datetime, timezone

from app.services.transaction_ops.netsuite_reader import (
    LINE_FIELDS,
    NetSuiteEvidenceError,
    _collection,
    _id,
    _sublist,
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
    "applied",
    "unapplied",
    "isTaxable",
    "custbody_fw_order_number",
)


def historical_refund_context(review, report):
    """Expose previously collected refund identities without claiming freshness.

    The case comparison already owns this evidence. Omitting its linked credits
    from investigation made a paid invoice look like it needed a second credit.
    This projection cannot satisfy current application or write preconditions.
    """
    refunds = report.get("refund_evidence") or {}
    if not isinstance(refunds, dict):
        return None
    source, target = refunds.get("source") or {}, refunds.get("target") or {}
    if not isinstance(source, dict) or not isinstance(target, dict):
        return None
    scope = review.get("scope") or {}
    reference = report.get("order_reference")
    currency = (report.get("source") or {}).get("currency")
    if (
        not reference
        or not currency
        or any(
            part.get("order_reference") != reference or part.get("currency") != currency for part in (source, target)
        )
        or target.get("account_id") != scope.get("netsuite_account_id")
        or target.get("subsidiary_id") != scope.get("subsidiary_id")
        or target.get("connection_id") != review.get("netsuite_connection_id")
    ):
        return None
    context = {
        "authority": "Historical comparison evidence. Re-read these identified records and their applications "
        "before concluding absence, available credit, settlement, or proposing another financial change.",
        "order_reference": reference,
        "currency": currency,
        "source": {k: source.get(k) for k in ("amount", "refund_count", "complete", "observed_at")},
        "target": {k: target.get(k) for k in ("amount", "refund_count", "complete", "observed_at", "account_id")},
        "request_links": [],
    }
    links = target.get("request_links")
    if isinstance(links, list):
        context["links_truncated"] = len(links) > 100
        for link in links[:100]:
            if not isinstance(link, dict):
                continue
            context["request_links"].append(
                {
                    key: link.get(key)
                    for key in (
                        "stage",
                        "amount",
                        "reason_id",
                        "refund_id",
                        "request_id",
                        "credit_memo_id",
                        "payment_number",
                        "source_refund_id",
                    )
                }
            )
    return context


async def collect_accounting_evidence(db, tenant_id, review, report, *, posting_detail=True, field_map=None):
    """Read one case's native evidence chain, preserving partial failures and scope conflicts."""
    from app.services.transaction_ops.accounting_field_map import resolve

    native_fields = resolve(field_map)
    document_fields = (
        DOCUMENT_FIELDS
        if field_map is None
        else (*(key for key in DOCUMENT_FIELDS if not key.startswith("custbody_")), native_fields["order_reference"])
    )
    line_fields = (
        LINE_FIELDS
        if field_map is None
        else frozenset(
            {
                *(key for key in LINE_FIELDS if not key.startswith("custcol_")),
                native_fields["source_line_id"],
                native_fields["original_sku"],
                native_fields["vat_amount"],
            }
        )
    )
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
            raw = await read(
                label,
                "GET",
                f"/record/v1/{kind}/{identifier}",
                # Item identity/pricing is needed even in the inexpensive
                # group pass. Expansion adds no separate API request; GL,
                # period/tax references and application research remain deferred.
                params={"expandSubResources": "true"},
            )
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
            projected = _project(raw, document_fields)
            projected["currency_code"] = currency["symbol"]
            projected["record_type"] = kind.lower()
            address = raw.get("shippingAddress")
            if isinstance(address, dict):
                jurisdiction = _project(address, ("country", "state", "zip"))
                if jurisdiction:
                    projected["tax_jurisdiction"] = jurisdiction
            if kind in {"salesOrder", "invoice", "cashSale", "creditMemo"}:
                problems = []
                lines = _sublist(raw, "item", "item_lines", line_fields, problems)
                projected["line_evidence"] = {
                    "complete": lines is not None and not problems,
                    "lines": lines,
                    "problems": problems,
                    "source": "native_record_item_sublist",
                }
            if posting_detail and kind in {"creditMemo", "customerRefund"}:
                problems = []
                applications = _sublist(
                    raw,
                    "apply",
                    "applications",
                    frozenset({"apply", "doc", "line", "amount", "refNum", "type"}),
                    problems,
                )
                projected["application_evidence"] = {
                    "complete": applications is not None and not problems,
                    "lines": applications,
                    "problems": problems,
                }
            return projected

        order = await document("salesOrder", order_id, "sales_order")
        if order is None:
            return result
        if order.get("tranId") != report.get("order_reference"):
            result["blockers"].append("native_order_reference_requires_mapping_verification")
            return result
        result["sections"]["sales_order"] = order
        refund_context = historical_refund_context(review, report)
        if refund_context is not None:
            result["sections"]["historical_refunds"] = refund_context
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
            if not posting_detail:
                continue
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
                limit=1000,
            )
            if gl is not None:
                result["sections"].setdefault("gl", {})[identifier] = gl
        if not posting_detail:
            result["blockers"].append("posting_detail_deferred_until_supported_treatment_is_identified")
            result["deferred_sections"] = ["gl", "deposits", "taxItem", "postingPeriod"]
        if posting_detail and refund_context:
            # Historical identifiers are leads, not current allocation proof.
            # Refresh a bounded set in the already authenticated account. The
            # full refund graph remains the authority for order allocation.
            related = {
                "documents": [],
                "complete": False,
                "authority": "Fresh linked-record observations, not a complete refund graph or "
                "certification of tax treatment. Do not create duplicate credits/refunds. "
                "Use the scoped refund graph to establish allocation and completeness.",
            }
            result["sections"]["related_refund_documents"] = related
            credit_ids = list(
                dict.fromkeys(
                    ident for link in refund_context["request_links"] if (ident := _id(link.get("credit_memo_id")))
                )
            )
            related["additional_credit_ids"] = credit_ids[2:]
            known_origins = {order_id, *(_ref(doc.get("id")) for doc in result["sections"]["posting_documents"])}
            refund_ids = set()
            for identifier in credit_ids[:2]:
                doc = await document("creditMemo", identifier, f"credit:{identifier}")
                if doc is None:
                    continue
                if not (
                    doc.get(native_fields["order_reference"]) == report.get("order_reference")
                    or _ref(doc.get("createdFrom")) in known_origins
                ) or (
                    doc.get(native_fields["order_reference"])
                    and doc[native_fields["order_reference"]] != report.get("order_reference")
                ):
                    result["blockers"].append(f"credit:{identifier}:order_identity_unverified")
                    continue
                related["documents"].append(doc)
                applications = doc["application_evidence"]
                for link in refund_context["request_links"]:
                    refund_id = _id(link.get("refund_id"))
                    if (
                        link.get("credit_memo_id") == identifier
                        and refund_id
                        and any(
                            row.get("apply") is True and _ref(row.get("doc")) == refund_id
                            for row in applications.get("lines") or []
                        )
                    ):
                        refund_ids.add(refund_id)
                gl = await query(
                    f"gl:{identifier}",
                    "SELECT tal.account, BUILTIN.DF(tal.account) AS account_name, "
                    "tal.accountingbook, tal.debit, tal.credit "
                    "FROM transactionaccountingline tal JOIN transaction t ON t.id = tal.transaction "
                    f"WHERE tal.transaction = {identifier} AND t.subsidiary = {subsidiary}",
                    limit=1000,
                )
                if gl is not None:
                    result["sections"].setdefault("gl", {})[identifier] = gl
            for identifier in sorted(refund_ids, key=int)[:2]:
                doc = await document("customerRefund", identifier, f"refund:{identifier}")
                if doc is not None:
                    related["documents"].append(doc)
            related["additional_refund_ids"] = sorted(refund_ids, key=int)[2:]
        for row in deposits[:2] if posting_detail else []:
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


def completion_evidence_summary(evidence):
    """Small factual receipt for a connector-neutral completion review.

    Do not hide unknowns behind the summary: omitted line/jurisdiction detail
    still requires the full observation for any conclusion relying on it.
    """
    sections = evidence.get("sections") or {}
    related = sections.get("related_refund_documents") or {}
    documents = [sections.get("sales_order") or {}] + (sections.get("posting_documents") or [])
    documents += related.get("documents") or []
    documents += sections.get("deposits") or []
    projected = []
    fields = (
        "record_type",
        "id",
        "tranId",
        "status",
        "total",
        "taxTotal",
        "subtotal",
        "currency_code",
        "createdFrom",
        "amountPaid",
        "amountRemaining",
        "applied",
        "unapplied",
        "isTaxable",
        "exchangeRate",
        "taxItem",
        "postingPeriod",
    )
    for doc in documents:
        if not doc:
            continue
        entry = {k: doc[k] for k in fields if k in doc}
        if "application_evidence" in doc:
            entry["applications"] = doc["application_evidence"]
        # Omission is easy to mistake for zero when a gross credit matches.
        # Make the unknown explicit without fabricating native tax evidence.
        if "taxTotal" not in doc:
            entry["tax_total_observation"] = "not_returned; tax effect unknown from this header"
        projected.append(entry)
    ledger = {}
    for identifier, observation in list((sections.get("gl") or {}).items())[:4]:
        rows = observation.get("rows") or []
        ledger[identifier] = {
            "rows": rows[:12],
            "observed_row_count": len(rows),
            "query_complete": observation.get("complete") is True,
            "complete": observation.get("complete") is True and len(rows) <= 12,
            "projection_truncated": len(rows) > 12,
        }
    return {
        "audit_id": evidence.get("audit_id"),
        "observed_at": evidence.get("observed_at"),
        "scope": evidence.get("verified_connection_scope"),
        "source": {
            k: (evidence.get("source_refresh") or {}).get(k)
            for k in ("number", "currency", "total", "tax_total", "updated_at")
        },
        "documents": projected,
        "gl": ledger,
        "gl_documents_projection_truncated": len(sections.get("gl") or {}) > len(ledger),
        "gl_basis": "Native accounting-book debit/credit values; do not assume transaction-currency amounts. "
        "Read saved documents detail for omitted rows and establish account classifications before tax conclusions.",
        "related_refund_graph_complete": related.get("complete", False),
        "blockers": evidence.get("blockers") or [],
        "assessment": evidence.get("assessment"),
        "candidate_available": bool(evidence.get("correction_candidate")),
        "execution_capabilities": (evidence.get("resolution_assessment") or {}).get("execution_capabilities"),
        "solution_status": (evidence.get("resolution_assessment") or {}).get("status"),
        "selected_treatment": (evidence.get("resolution_assessment") or {}).get("selected_treatment"),
        "posting_balance": {
            k: v
            for k, v in (evidence.get("posting_balance") or {}).items()
            if k
            in {
                "status",
                "basis",
                "currency",
                "amounts",
                "sales_order_alignment",
                "observed_at",
                "accounting_book",
                "interpretation",
            }
        }
        if isinstance(evidence.get("posting_balance"), dict)
        else None,
        "line_comparison": evidence.get("line_comparison"),
        "source_revision_deltas": evidence.get("source_revision_deltas"),
        "resolution_intents": evidence.get("resolution_intents") or [],
        "projection_limit": "Line identity, tax jurisdiction and treatment policy require full evidence; "
        "this summary does not establish them or authorize any write.",
    }
