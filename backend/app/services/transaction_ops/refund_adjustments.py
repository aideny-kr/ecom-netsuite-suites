"""Account-scoped tax-credit interpretation; never infer a tax effect from cash alone."""

import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.schemas.transaction_ops import EvidenceModel, _decimal
from app.services.transaction_ops.netsuite_reader import _account, _collection, _sublist

Id = Annotated[str, Field(pattern=r"^[0-9]{1,30}$")]
_IDENTIFIER = re.compile(r"[0-9]{1,30}")


class RefundAdjustmentProfile(EvidenceModel):
    schema_version: Literal[1]
    account_id: str
    subsidiary_id: Id
    tax_reversal_reason_ids: tuple[Id, ...] = Field(min_length=1, max_length=20)
    tax_item_accounts: dict[Id, Id] = Field(min_length=1, max_length=20)
    # The subsidiary's tax accounts. Tax reaches a credit memo two ways -- through the tax
    # engine, as its own line, or as an ordinary item line posting straight into a tax
    # account -- and only the account tells the two apart. Declared rather than derived
    # because a tax account that has seen no recent postings is invisible in the ledger.
    # Defaults to the accounts the tax-item map already names, so an existing profile keeps
    # its current meaning.
    tax_accounts: tuple[Id, ...] = Field(default=(), max_length=20)

    @property
    def taxed_accounts(self) -> frozenset[str]:
        return frozenset(self.tax_accounts) or frozenset(self.tax_item_accounts.values())

    @field_validator("account_id")
    @classmethod
    def canonical_account(cls, value):
        return _account(value)


# A credit memo's posting lines. Reclassification appends matched pairs, so the ceiling
# sits well above the line count anyone writes by hand.
LEDGER_ROWS = 40


async def ledger_postings(request, memo_ids, subsidiary_id):
    """Every posting line of every credit memo this order touches, in one query.

    One query per credit memo would scale the call count with the number of refunds, and the
    per-order ceiling is already tight enough that a busy order loses its refund evidence
    entirely rather than reporting a budget error.
    """
    if not all(_IDENTIFIER.fullmatch(str(memo_id)) for memo_id in [*memo_ids, subsidiary_id]):
        raise ValueError("credit_ledger_identifier_rejected")
    ceiling = LEDGER_ROWS * len(memo_ids)
    result = await request(
        "POST",
        "/query/v1/suiteql",
        params={"limit": ceiling + 1},
        body={
            "q": "SELECT tal.transaction, tal.account, tal.accountingbook, tal.debit, tal.credit "
            "FROM transactionaccountingline tal JOIN transaction t ON t.id = tal.transaction "
            f"WHERE tal.transaction IN ({','.join(sorted(memo_ids))}) AND t.subsidiary = {subsidiary_id}"
        },
    )
    rows, complete = _collection(result)
    if not complete or len(rows) > ceiling:
        raise ValueError("credit_ledger_incomplete")
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(str(row["transaction"]), []).append(row)
    return grouped


def ledger_tax(rows, profile, amount):
    """Net one credit memo's postings by account; return the tax part and the netting itself.

    The ledger is the only place both booking conventions are legible. Tax charged through
    the tax engine arrives as its own posting line; tax reversed by convention arrives as an
    ordinary item line aimed at a tax account. The REST item sublist renders the second as
    though it were net sales, and on a legacy-tax nexus it omits the line tax fields
    altogether, which is why reading it cannot work for every subsidiary.
    """
    if not rows:
        raise ValueError("credit_ledger_missing")
    # One accounting book only. Netting across books double counts, and a book the scope
    # never named is not evidence about the book it did.
    books = {str(row["accountingbook"]) for row in rows if row.get("accountingbook") is not None}
    if len(books) != 1:
        raise ValueError("credit_ledger_book_ambiguous")
    nets: dict[str, Decimal] = {}
    for row in rows:
        account = row.get("account")
        if account is None:
            continue  # a non-posting line carries no account and no amount
        nets[str(account)] = (
            nets.get(str(account), Decimal(0)) + _decimal(row.get("debit") or 0) - _decimal(row.get("credit") or 0)
        )
    if not nets or sum(nets.values(), Decimal(0)) != 0:
        raise ValueError("credit_ledger_unbalanced")
    tax = sum((value for account, value in nets.items() if account in profile.taxed_accounts), Decimal(0))
    debited = sum((value for value in nets.values() if value > 0), Decimal(0))
    credited = sum((value for value in nets.values() if value < 0), Decimal(0))
    # Read the split off the totals. A reclassification pair cancels exactly in the
    # subsidiary's base currency but leaves a sub-cent residue in the account it moved value
    # out of when netted in the order's own currency, which is the currency being compared,
    # so nothing here may assume those pairs cancel.
    if debited != amount or credited != -amount or not 0 <= tax <= amount:
        raise ValueError("credit_ledger_disagrees_with_refund")
    return tax, {account: str(value) for account, value in sorted(nets.items()) if value}


async def read_tax_adjustments(request, links, profile, order_id, subsidiary_id, currency_id, reference, allocations):
    profile = RefundAdjustmentProfile.model_validate(profile)
    if profile.subsidiary_id != subsidiary_id:
        raise ValueError("adjustment_profile_scope_mismatch")
    memo_ids = {str(link["credit_memo_id"]) for link in links if link.get("credit_memo_id")}
    try:
        postings = await ledger_postings(request, memo_ids, subsidiary_id) if memo_ids else {}
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return []
    proofs = []
    for link in links:
        tax_reversal = link["reason_id"] in profile.tax_reversal_reason_ids
        if not link["credit_memo_id"] and not tax_reversal:
            continue
        if link["stage"] != "refund_verified" or not link["credit_memo_id"]:
            return []
        if allocations.get(link["refund_id"]) != {link["credit_memo_id"]}:
            return []
        try:
            record = await request(
                "GET", f"/record/v1/creditmemo/{link['credit_memo_id']}", params={"expandSubResources": "true"}
            )
            amount = _decimal(link["amount"])
            if (
                record["id"] != link["credit_memo_id"]
                or record["currency"]["id"] != currency_id
                or record["subsidiary"]["id"] != subsidiary_id
                or record["custbody_fw_order_number"] != reference
                or _decimal(record["total"]) != amount
                or _decimal(record["applied"]) != amount
                or _decimal(record["unapplied"]) != 0
            ):
                return []
            problems = []
            lines = _sublist(
                record,
                "item",
                "credit_items",
                frozenset(
                    {
                        "line",
                        "item",
                        "itemType",
                        "account",
                        "amount",
                    }
                ),
                problems,
            )
            if problems or not lines:
                return []
            seen, items = set(), {}
            for line in lines:
                item, account = line["item"]["id"], line["account"]["id"]
                value = _decimal(line["amount"])
                if (
                    line["line"] in seen
                    or line["itemType"]["id"] not in {"NonInvtPart", "InvtPart", "Service"}
                    or (
                        tax_reversal
                        and (line["itemType"]["id"] != "NonInvtPart" or profile.tax_item_accounts.get(item) != account)
                    )
                    or (not tax_reversal and item in profile.tax_item_accounts)
                    or value is None
                    or value <= 0
                ):
                    return []
                seen.add(line["line"])
                items[item] = account
            # The ledger is the authority on how much of this credit memo is tax. The record
            # header disagrees with it by design on a reversal, where the whole amount posts
            # to a tax account through an item line and taxTotal stays zero.
            native_tax, ledger = ledger_tax(postings.get(str(link["credit_memo_id"])), profile, amount)
            if tax_reversal and native_tax != amount:
                return []
            proofs.append(
                {
                    "kind": "tax_reversal" if tax_reversal else "credit_memo",
                    "tax_amount": str(native_tax),
                    "request_id": link["request_id"],
                    "source_refund_id": link["source_refund_id"],
                    "payment_number": link["payment_number"],
                    "credit_memo_id": link["credit_memo_id"],
                    "refund_id": link["refund_id"],
                    "order_record_id": order_id,
                    "amount": str(amount),
                    "reason_id": link["reason_id"],
                    "item_accounts": items,
                    # Why this figure is the tax: the account-level netting it came from.
                    # A stored verdict a reviewer cannot re-derive is an assertion, not
                    # evidence, and the ledger read behind it is not otherwise retained.
                    "ledger_accounts": ledger,
                    "credit_modified_at": record.get("lastModifiedDate"),
                }
            )
        except (KeyError, TypeError, ValueError, ArithmeticError):
            # Cash evidence survives unavailable/unsupported credit detail.
            return []
    return proofs


def verified_tax_adjustments(refunds, config, order_id, reference, currency, amount_parser):
    """Cross-check native credit allocation against individual completed source refunds."""
    try:
        profile = RefundAdjustmentProfile.model_validate(config["mapping_json"]["refund_adjustments"])
        source, target = refunds["source"], refunds["target"]
        if (
            profile.account_id != _account(config["netsuite_account_id"])
            or profile.subsidiary_id != str(config["subsidiary_id"])
            or source.get("events_complete") is not True
            or target.get("provider") != "netsuite"
            or target.get("account_id") != profile.account_id
            or target.get("subsidiary_id") != profile.subsidiary_id
            or target.get("connection_id") != str(config["netsuite_connection_id"])
            or any(
                r.get("complete") is not True or r.get("order_reference") != reference or r.get("currency") != currency
                for r in (source, target)
            )
        ):
            return []
        events = {e["id"]: e for e in source["events"]}
        if len(events) != len(source["events"]) or len(events) != source["refund_count"]:
            return []
        total = sum((amount_parser(e["amount"]) for e in events.values()), Decimal(0))
        if total != amount_parser(source["amount"]) or total != amount_parser(target["amount"]):
            return []
        proofs = target.get("tax_adjustments") or []
        identities = {key: set() for key in ("request_id", "source_refund_id", "credit_memo_id", "refund_id")}
        for proof in proofs:
            event = events[proof["source_refund_id"]]
            amount = amount_parser(proof["amount"])
            tax_reversal = proof["kind"] == "tax_reversal"
            tax_amount = amount_parser(proof.get("tax_amount", proof["amount"] if tax_reversal else None))
            if (
                proof["kind"] not in {"tax_reversal", "credit_memo"}
                or proof["order_record_id"] != order_id
                or (proof["reason_id"] in profile.tax_reversal_reason_ids) != tax_reversal
                or not proof["item_accounts"]
                or (
                    tax_reversal
                    and any(profile.tax_item_accounts.get(k) != v for k, v in proof["item_accounts"].items())
                )
                or (not tax_reversal and any(k in profile.tax_item_accounts for k in proof["item_accounts"]))
                or proof["payment_number"] != event["payment_number"]
                or not event["payment_number"]
                or amount is None
                or amount <= 0
                or tax_amount is None
                or not 0 <= tax_amount <= amount
                or (tax_reversal and tax_amount != amount)
                or amount != amount_parser(event["amount"])
            ):
                return []
            for key, seen in identities.items():
                value = proof[key]
                if not isinstance(value, str) or not value.isdigit() or value in seen:
                    return []
                seen.add(value)
        return proofs
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return []
