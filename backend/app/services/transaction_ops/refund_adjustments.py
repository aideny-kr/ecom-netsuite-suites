"""Account-scoped tax-credit interpretation; never infer a tax effect from cash alone."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.schemas.transaction_ops import EvidenceModel, _decimal
from app.services.transaction_ops.netsuite_reader import _account, _sublist

Id = Annotated[str, Field(pattern=r"^[0-9]{1,30}$")]


class RefundAdjustmentProfile(EvidenceModel):
    schema_version: Literal[1]
    account_id: str
    subsidiary_id: Id
    tax_reversal_reason_ids: tuple[Id, ...] = Field(min_length=1, max_length=20)
    tax_item_accounts: dict[Id, Id] = Field(min_length=1, max_length=20)

    @field_validator("account_id")
    @classmethod
    def canonical_account(cls, value):
        return _account(value)


async def read_tax_adjustments(request, links, profile, order_id, subsidiary_id, currency_id, reference, allocations):
    profile = RefundAdjustmentProfile.model_validate(profile)
    if profile.subsidiary_id != subsidiary_id:
        raise ValueError("adjustment_profile_scope_mismatch")
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
            native_tax = _decimal(record.get("taxTotal"))
            if (
                record["id"] != link["credit_memo_id"]
                or record["currency"]["id"] != currency_id
                or record["subsidiary"]["id"] != subsidiary_id
                or record["custbody_fw_order_number"] != reference
                or _decimal(record["total"]) != amount
                or _decimal(record["applied"]) != amount
                or _decimal(record["unapplied"]) != 0
                or native_tax is None
                or native_tax < 0
                or native_tax > amount
                or (tax_reversal and native_tax != 0)
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
                        "grossAmt",
                        "tax1Amt",
                    }
                ),
                problems,
            )
            if problems or not lines:
                return []
            total, line_taxes, seen, items = Decimal(0), Decimal(0), set(), {}
            for line in lines:
                item, account = line["item"]["id"], line["account"]["id"]
                value = _decimal(line["amount"])
                line_tax = _decimal(line["tax1Amt"])
                gross = _decimal(line["grossAmt"])
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
                    or line_tax is None
                    or line_tax < 0
                    or gross != value + line_tax
                    or (tax_reversal and line_tax != 0)
                ):
                    return []
                seen.add(line["line"])
                items[item] = account
                total += gross
                line_taxes += line_tax
            if total != amount or line_taxes != native_tax:
                return []
            proofs.append(
                {
                    "kind": "tax_reversal" if tax_reversal else "credit_memo",
                    "tax_amount": str(amount if tax_reversal else native_tax),
                    "request_id": link["request_id"],
                    "source_refund_id": link["source_refund_id"],
                    "payment_number": link["payment_number"],
                    "credit_memo_id": link["credit_memo_id"],
                    "refund_id": link["refund_id"],
                    "order_record_id": order_id,
                    "amount": str(amount),
                    "reason_id": link["reason_id"],
                    "item_accounts": items,
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
