"""Framework refund-request links supplement (never replace) native payment proof."""

import re

from app.schemas.transaction_ops import _decimal
from app.services.transaction_ops.netsuite_reader import _collection, _id

MAX_REQUESTS = 100
_REFERENCE = re.compile(r"R[0-9]{9}(?:-[A-Z0-9]+)?")
_FIELDS = (
    "id",
    "name",
    "order_reference",
    "order_id",
    "processed",
    "credit_id",
    "refund_id",
    "amount",
    "payment_number",
    "reason_id",
    "credit_type",
    "credit_currency",
    "credit_subsidiary",
    "credit_posting",
    "credit_voided",
    "credit_reference",
    "deposit_id",
    "deposit_type",
    "deposit_currency",
    "deposit_subsidiary",
    "deposit_posting",
    "deposit_voided",
    "deposit_customer",
    "order_customer",
)


def request_query(condition):
    # Conditions are composed only below, after exact reference/ID validation.
    return (
        "SELECT r.id, r.name, r.custrecord_refreq_order_number AS order_reference, "
        "r.custrecord_refreq_so_link AS order_id, r.custrecord_refreq_processed AS processed, "
        "r.custrecord_refreq_cm_link AS credit_id, r.custrecord_refreq_refund_link AS refund_id, "
        "r.custrecord_refreq_refund_amount AS amount, r.custrecord_refreq_payment_id AS payment_number, "
        "r.custrecord_refreq_refund_reason AS reason_id, c.type AS credit_type, "
        "c.currency AS credit_currency, cl.subsidiary AS credit_subsidiary, "
        "c.posting AS credit_posting, c.voided AS credit_voided, c.custbody_fw_order_number AS credit_reference, "
        "r.custrecord_refreq_cust_dep_link AS deposit_id, d.type AS deposit_type, "
        "d.currency AS deposit_currency, dl.subsidiary AS deposit_subsidiary, "
        "d.posting AS deposit_posting, d.voided AS deposit_voided, "
        "d.entity AS deposit_customer, so.entity AS order_customer "
        "FROM customrecord_fw_refund_requests r "
        "LEFT JOIN transaction c ON c.id=r.custrecord_refreq_cm_link "
        "LEFT JOIN transactionline cl ON cl.transaction=c.id AND cl.mainline='T' "
        "LEFT JOIN transaction d ON d.id=r.custrecord_refreq_cust_dep_link "
        "LEFT JOIN transactionline dl ON dl.transaction=d.id AND dl.mainline='T' "
        "LEFT JOIN transaction so ON so.id=r.custrecord_refreq_so_link "
        f"WHERE {condition} ORDER BY r.id"
    )


async def read_request_links(request, order_id, subsidiary_id, currency_id, reference):
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise ValueError("invalid_refund_reference")

    async def read(condition):
        result = await request(
            "POST",
            "/query/v1/suiteql",
            params={"limit": MAX_REQUESTS + 1},
            body={"q": request_query(condition)},
        )
        rows, complete = _collection(result)
        if not complete or len(rows) > MAX_REQUESTS:
            raise ValueError("refund_requests_incomplete")
        return [{key: row.get(key) for key in _FIELDS} for row in rows]

    rows = await read(f"r.custrecord_refreq_so_link={order_id} OR r.custrecord_refreq_order_number='{reference}'")
    nodes, links, identities, source_ids, credits, refunds = {}, [], set(), set(), set(), set()
    deposits = set()
    for row in rows:
        rid, source_id = _id(row["id"]), _id(row["name"])
        if (
            not rid
            or not source_id
            or rid in identities
            or source_id in source_ids
            or row["order_reference"] != reference
        ):
            raise ValueError("refund_request_identity_unproven")
        identities.add(rid)
        source_ids.add(source_id)
        amount = _decimal(row["amount"])
        if amount is None or amount <= 0 or row["processed"] not in {"T", "F"}:
            raise ValueError("refund_request_invalid")
        if row["order_id"] is not None and _id(row["order_id"]) != order_id:
            raise ValueError("refund_request_order_mismatch")
        credit, refund = _id(row["credit_id"]), _id(row["refund_id"])
        if (row["credit_id"] is not None and not credit) or (row["refund_id"] is not None and not refund):
            raise ValueError("refund_request_link_invalid")
        if (credit or refund or row["processed"] == "T") and _id(row["order_id"]) != order_id:
            raise ValueError("refund_request_order_unproven")
        if credit:
            if credit in credits or (
                row["credit_type"],
                row["credit_currency"],
                row["credit_subsidiary"],
                row["credit_posting"],
                row["credit_voided"],
                row["credit_reference"],
            ) != ("CustCred", currency_id, subsidiary_id, "T", "F", reference):
                raise ValueError("refund_credit_ownership_unproven")
            credits.add(credit)
            nodes[credit] = {
                key: row["credit_" + key] for key in ("type", "currency", "subsidiary", "posting", "voided")
            }
        deposit = _id(row["deposit_id"])
        if row["deposit_id"] is not None:
            if (
                not deposit
                or _id(row["order_id"]) != order_id
                or not _id(row["order_customer"])
                or row["deposit_customer"] != row["order_customer"]
                or (
                    row["deposit_type"],
                    row["deposit_currency"],
                    row["deposit_subsidiary"],
                    row["deposit_posting"],
                    row["deposit_voided"],
                )
                != ("CustDep", currency_id, subsidiary_id, "T", "F")
            ):
                raise ValueError("refund_deposit_ownership_unproven")
            deposits.add(deposit)
            metadata = {key: row["deposit_" + key] for key in ("type", "currency", "subsidiary", "posting", "voided")}
            if deposit in nodes and nodes[deposit] != metadata:
                raise ValueError("refund_deposit_ownership_changed")
            nodes[deposit] = metadata
        if refund:
            if refund in refunds:
                raise ValueError("refund_request_duplicate_refund")
            refunds.add(refund)
        links.append(
            {
                **({"deposit_id": deposit} if deposit else {}),
                "request_id": rid,
                "source_refund_id": source_id,
                "payment_number": row["payment_number"],
                "credit_memo_id": credit,
                "refund_id": refund,
                "amount": str(amount),
                "reason_id": row["reason_id"],
                "stage": "linked"
                if refund
                else "credit_only"
                if credit
                else "pending"
                if row["processed"] == "F"
                else "unlinked",
            }
        )

    async def recheck():
        # A second bounded lookup catches shared custom ownership and links changing
        # between reads. Never assign a shared credit based on the requested row alone.
        if credits or refunds or deposits:
            conditions = []
            if credits:
                conditions.append(f"r.custrecord_refreq_cm_link IN ({','.join(sorted(credits, key=int))})")
            if refunds:
                conditions.append(f"r.custrecord_refreq_refund_link IN ({','.join(sorted(refunds, key=int))})")
            if deposits:
                conditions.append(f"r.custrecord_refreq_cust_dep_link IN ({','.join(sorted(deposits, key=int))})")
            linked = [
                row for row in rows if any(row[key] is not None for key in ("credit_id", "refund_id", "deposit_id"))
            ]
            reverse = await read(" OR ".join(conditions))
            if sorted(reverse, key=lambda r: str(r["id"])) != sorted(linked, key=lambda r: str(r["id"])):
                raise ValueError("refund_request_shared_or_changed")

    return nodes, links, recheck


def verify_request_allocations(links, allocations, amounts):
    for link in links:
        refund, credit = link["refund_id"], link["credit_memo_id"]
        if refund:
            if refund not in amounts or (credit and credit not in allocations[refund]):
                raise ValueError("refund_request_application_unproven")
            if amounts[refund] != _decimal(link["amount"]):
                raise ValueError("refund_request_amount_mismatch")
            link["stage"] = "refund_verified"
        elif link["stage"] == "unlinked" and not amounts:
            # A processed request alone cannot establish that no money returned.
            raise ValueError("processed_refund_unlinked")
