"""The outcome check for an agent-authored reallocation of an existing credit's lines.

Fixtures are the two orders verified read-only against Framework's production books on
2026-09-25 (ids and amounts only):

- R094649369 (US Inc, USD): a tax-only refund of 2.80 booked on item 1603 (Sales Returns).
  Solidus now says net 799.00 / tax 0.00. The fix moves the 2.80 to tax-refund item 5005 (-> 210).
- R600526599 (BV, EUR, VAT included): a 225.00 refund booked entirely on 1603 while Solidus'
  included VAT fell 489.25 -> 450.20. The fix keeps 185.95 on 1603 and moves 39.05 to 4699 (-> 846).
"""

from copy import deepcopy

import pytest

from app.services.transaction_ops import credit_line_reallocation as reallocation
from app.services.transaction_ops.credit_line_reallocation import RefusalError

ACCOUNT_TYPES = {
    "119": "AcctRec",
    "54": "Income",
    "783": "Income",
    "210": "OthCurrLiab",
    "846": "OthCurrLiab",
    "906": "OthCurrLiab",
    "999": "OthCurrLiab",
    "500": "COGS",
    "130": "OthCurrAsset",
    "2100": "LongTermLiab",
}


def _gl(*rows):
    return {"complete": True, "rows": [{"account": a, "accountingbook": "1", side: v} for a, side, v in rows]}


def _us():
    invoice = {
        "id": "15407087",
        "record_type": "invoice",
        "total": "801.8",
        "taxTotal": "2.8",
        "subtotal": "799.0",
        "currency": {"id": "1"},
        "currency_code": "USD",
        "exchangeRate": "1.0",
        "subsidiary": {"id": "1"},
        "entity": {"id": "5685197"},
        "account": {"id": "119"},
        "createdFrom": {"id": "15400563"},
    }
    credit = {
        "id": "15788939",
        "record_type": "creditmemo",
        "total": "2.8",
        "subtotal": "2.8",
        "taxTotal": None,
        "currency": {"id": "1"},
        "currency_code": "USD",
        "exchangeRate": "1.0",
        "subsidiary": {"id": "1"},
        "entity": {"id": "5685197"},
        "account": {"id": "119"},
        "location": {"id": "30"},
        "postingPeriod": {"id": "171"},
        "applied": "2.8",
        "unapplied": "0.0",
        "isTaxable": False,
        "line_evidence": {
            "complete": True,
            "lines": [
                {
                    "line": 1,
                    "item": {"id": "1603"},
                    "itemType": {"id": "NonInvtPart"},
                    "quantity": "1.0",
                    "rate": "2.8",
                    "amount": "2.8",
                    "isTaxable": False,
                    "lineUniqueKey": "65604965",
                }
            ],
        },
        "application_evidence": {
            "complete": True,
            "lines": [{"amount": "2.8", "apply": True, "doc": {"id": "15788941"}}],
        },
    }
    return {
        "credit": credit,
        "credit_gl": _gl(("119", "credit", "2.8"), ("783", "debit", "2.8")),
        "invoices": [
            (
                invoice,
                _gl(
                    ("119", "debit", "801.8"), ("54", "credit", "759"), ("210", "credit", "2.8"), ("54", "credit", "40")
                ),
            )
        ],
        "other_credits": [],
        "source": {
            "number": "R094649369",
            "completed_at": "2026-08-05T10:00:00Z",
            "requires_review": False,
            "state": "complete",
            "payment_state": "paid",
            "currency": "USD",
            "total": "799.0",
            "payment_total": "799.0",
            "tax_total": "0.0",
            "additional_tax_total": "0.0",
            "included_tax_total": "0.0",
        },
        "profile": {"subsidiary_id": "1", "tax_accounts": ["210", "866", "905"], "tax_item_accounts": {"5005": "210"}},
        "account_types": ACCOUNT_TYPES,
        "items": {
            "1603": {"id": "1603", "isInactive": False, "incomeAccount": {"id": "783"}, "itemType": "NonInvtPart"},
            "5005": {"id": "5005", "isInactive": False, "incomeAccount": {"id": "210"}, "itemType": "NonInvtPart"},
        },
        "period": {"id": "171", "closed": False, "arLocked": False, "allLocked": False},
        "subsidiary_id": "1",
    }


def _bv():
    invoice = {
        "id": "15772226",
        "record_type": "invoice",
        "total": "2819.0",
        "taxTotal": "489.25",
        "subtotal": "2329.75",
        "currency": {"id": "4"},
        "currency_code": "EUR",
        "exchangeRate": "1.0",
        "subsidiary": {"id": "2"},
        "entity": {"id": "E-BV"},
        "account": {"id": "119"},
        "createdFrom": {"id": "14402405"},
    }
    credit = {
        "id": "15777793",
        "record_type": "creditmemo",
        "total": "225.0",
        "subtotal": "225.0",
        "taxTotal": "0.0",
        "currency": {"id": "4"},
        "currency_code": "EUR",
        "exchangeRate": "1.0",
        "subsidiary": {"id": "2"},
        "entity": {"id": "E-BV"},
        "account": {"id": "119"},
        "postingPeriod": {"id": "171"},
        "applied": "225.0",
        "unapplied": "0.0",
        "line_evidence": {
            "complete": True,
            "lines": [
                {
                    "line": 1,
                    "item": {"id": "1603"},
                    "itemType": {"id": "NonInvtPart"},
                    "quantity": "1.0",
                    "rate": "225.0",
                    "amount": "225.0",
                    "taxCode": {"id": "4059"},
                    "lineUniqueKey": "65500001",
                }
            ],
        },
        "application_evidence": {
            "complete": True,
            "lines": [{"amount": "225.0", "apply": True, "doc": {"id": "15777794"}}],
        },
    }
    return {
        "credit": credit,
        "credit_gl": _gl(("119", "credit", "225"), ("783", "debit", "225")),
        "invoices": [
            (invoice, _gl(("119", "debit", "2819"), ("54", "credit", "2329.75"), ("846", "credit", "489.25")))
        ],
        "other_credits": [],
        "source": {
            "number": "R600526599",
            "completed_at": "2026-08-05T10:00:00Z",
            "requires_review": False,
            "state": "complete",
            "payment_state": "paid",
            "currency": "EUR",
            "total": "2594.0",
            "payment_total": "2594.0",
            "tax_total": "450.2",
            "additional_tax_total": "0.0",
            "included_tax_total": "450.2",
        },
        # BV's active config declares only the item map; its account counts as a tax account.
        "profile": {
            "subsidiary_id": "2",
            "tax_accounts": [],
            "tax_item_accounts": {"4699": "846"},
            "correction_location_id": "81",
        },
        "account_types": ACCOUNT_TYPES,
        "items": {
            "1603": {"id": "1603", "isInactive": False, "incomeAccount": {"id": "783"}, "itemType": "NonInvtPart"},
            "4699": {"id": "4699", "isInactive": False, "incomeAccount": {"id": "846"}, "itemType": "NonInvtPart"},
        },
        "period": {"id": "171", "closed": False, "arLocked": False, "allLocked": False},
        "subsidiary_id": "2",
    }


def _refused(facts, lines):
    with pytest.raises(RefusalError) as exc:
        reallocation.assess(lines=lines, **facts)
    return exc.value.code


US_FIX = [{"line": 1, "item_id": "5005", "amount": "2.80"}]
BV_FIX = [{"line": 1, "item_id": "1603", "amount": "185.95"}, {"item_id": "4699", "amount": "39.05"}]


class TestAccepts:
    def test_us_tax_only_refund_moves_to_the_tax_refund_item(self):
        result = reallocation.assess(lines=US_FIX, **_us())
        assert result["balance"]["before"] == {"gross": "799.00", "net": "796.20", "tax": "2.80"}
        assert result["balance"]["after"] == {"gross": "799.00", "net": "799.00", "tax": "0.00"}
        assert result["balance"]["source"] == {"gross": "799.00", "net": "799.00", "tax": "0.00"}
        assert result["expected_ledger"] == {"debit": {"210": "2.80"}, "credit": {"119": "2.80"}}
        assert result["expected_after"] == {"total": "2.80", "taxTotal": "0.00"}
        assert result["proposed_fields"] == {
            "item": {
                "items": [
                    {
                        "line": 1,
                        "item": {"id": "5005"},
                        "quantity": 1,
                        "rate": "2.80",
                        "amount": "2.80",
                        "isTaxable": False,
                    }
                ]
            }
        }

    def test_bv_vat_included_refund_splits_net_and_vat(self):
        result = reallocation.assess(lines=BV_FIX, **_bv())
        assert result["balance"]["before"] == {"gross": "2594.00", "net": "2104.75", "tax": "489.25"}
        assert (
            result["balance"]["after"]
            == result["balance"]["source"]
            == {
                "gross": "2594.00",
                "net": "2143.80",
                "tax": "450.20",
            }
        )
        assert result["expected_ledger"] == {"debit": {"783": "185.95", "846": "39.05"}, "credit": {"119": "225.00"}}
        # A new line copies the existing line's tax code; nothing about tax is chosen by the model.
        new_line = result["proposed_fields"]["item"]["items"][1]
        assert "line" not in new_line and new_line["taxCode"] == {"id": "4059"}

    def test_derive_produces_the_same_fix_the_check_accepts(self):
        for facts, expected in ((_us(), US_FIX), (_bv(), BV_FIX)):
            derived = reallocation.derive(**facts)
            assert derived == expected
            reallocation.assess(lines=derived, **facts)

    def test_another_owned_credit_counts_toward_the_order_balance(self):
        facts = _bv()
        # A second, already-correct VAT-only credit of 10.00 on the same order.
        other = deepcopy(facts["credit"])
        other["id"], other["total"] = "15777999", "10.0"
        facts["other_credits"] = [(other, _gl(("119", "credit", "10"), ("846", "debit", "10")))]
        facts["source"] = {
            **facts["source"],
            "total": "2584.0",
            "payment_total": "2584.0",
            "tax_total": "440.2",
            "included_tax_total": "440.2",
        }
        reallocation.assess(lines=BV_FIX, **facts)


class TestRefuses:
    def test_model_rounded_vat(self):
        lines = [{"line": 1, "item_id": "1603", "amount": "186.00"}, {"item_id": "4699", "amount": "39.00"}]
        assert _refused(_bv(), lines) == "outcome_does_not_match_source"

    def test_changed_credit_total(self):
        lines = [{"line": 1, "item_id": "1603", "amount": "2.80"}, {"item_id": "5005", "amount": "2.80"}]
        assert _refused(_us(), lines) == "lines_total_changed"

    def test_item_not_configured_for_the_subsidiary(self):
        facts = _us()
        facts["items"]["4699"] = {
            "id": "4699",
            "isInactive": False,
            "incomeAccount": {"id": "846"},
            "itemType": "NonInvtPart",
        }
        assert _refused(facts, [{"line": 1, "item_id": "4699", "amount": "2.80"}]) == "item_not_allowed"

    def test_tax_item_whose_account_is_not_the_configured_one(self):
        facts = _us()
        facts["items"]["5005"]["incomeAccount"] = {"id": "783"}
        assert _refused(facts, US_FIX) == "tax_item_account_mismatch"

    def test_inactive_item(self):
        facts = _us()
        facts["items"]["5005"]["isInactive"] = True
        assert _refused(facts, US_FIX) == "item_inactive"

    def test_existing_line_left_out(self):
        assert _refused(_us(), [{"item_id": "5005", "amount": "2.80"}]) == "existing_line_missing"

    def test_unknown_line(self):
        assert _refused(_us(), [{"line": 7, "item_id": "5005", "amount": "2.80"}]) == "unknown_line"

    @pytest.mark.parametrize("amount", ["0", "-2.80", "2.801", "NaN", 2.8])
    def test_bad_amounts(self, amount):
        assert _refused(_us(), [{"line": 1, "item_id": "5005", "amount": amount}]) == "invalid_amount"

    @pytest.mark.parametrize("flag", ["closed", "arLocked", "allLocked"])
    def test_locked_period(self, flag):
        facts = _us()
        facts["period"][flag] = True
        assert _refused(facts, US_FIX) == "period_locked"

    def test_foreign_currency(self):
        facts = _us()
        facts["credit"]["exchangeRate"] = "1.25"
        assert _refused(facts, US_FIX) == "foreign_currency_unsupported"

    def test_source_not_final(self):
        facts = _us()
        facts["source"]["payment_state"] = "balance_due"
        assert _refused(facts, US_FIX) == "source_not_final"

    def test_nothing_to_fix(self):
        facts = _us()
        facts["credit_gl"] = _gl(("119", "credit", "2.8"), ("210", "debit", "2.8"))
        assert _refused(facts, US_FIX) == "no_difference"

    def test_gross_not_reconciled(self):
        facts = _us()
        facts["source"] = {**facts["source"], "total": "790.0", "payment_total": "790.0"}
        assert _refused(facts, US_FIX) == "gross_not_reconciled"

    def test_credit_with_tax_engine_tax(self):
        facts = _us()
        facts["credit"]["taxTotal"] = "0.20"
        assert _refused(facts, US_FIX) == "credit_tax_engine_nonzero"

    def test_credit_of_another_customer_or_subsidiary(self):
        facts = _us()
        facts["credit"]["entity"] = {"id": "999"}
        assert _refused(facts, US_FIX) == "credit_scope_mismatch"
        facts = _us()
        facts["profile"]["subsidiary_id"] = "2"
        assert _refused(facts, US_FIX) == "credit_scope_mismatch"

    def test_incomplete_evidence(self):
        facts = _us()
        facts["credit_gl"]["complete"] = False
        assert _refused(facts, US_FIX) == "evidence_incomplete"
        facts = _us()
        facts["credit"]["application_evidence"]["complete"] = False
        assert _refused(facts, US_FIX) == "evidence_incomplete"

    def test_existing_line_quantity_other_than_one(self):
        facts = _us()
        facts["credit"]["line_evidence"]["lines"][0]["quantity"] = "2.0"
        assert _refused(facts, US_FIX) == "line_quantity_unsupported"

    def test_duplicate_line_reference(self):
        lines = [{"line": 1, "item_id": "1603", "amount": "1.40"}, {"line": 1, "item_id": "5005", "amount": "1.40"}]
        assert _refused(_us(), lines) == "duplicate_line"

    def test_refusal_carries_a_detail_the_agent_can_act_on(self):
        lines = [{"line": 1, "item_id": "1603", "amount": "186.00"}, {"item_id": "4699", "amount": "39.00"}]
        with pytest.raises(RefusalError) as exc:
            reallocation.assess(lines=lines, **_bv())
        assert exc.value.detail["required"] == {"gross": "2594.00", "net": "2143.80", "tax": "450.20"}
        assert exc.value.detail["proposed"] == {"gross": "2594.00", "net": "2143.75", "tax": "450.25"}


class TestReviewRoundOne:
    """Findings of the 2026-09-25 T2 gate round 1 (wf_caf4e844-a12)."""

    def test_tax_must_go_to_an_account_the_invoice_actually_credited(self):
        facts = _bv()
        facts["profile"]["tax_item_accounts"] = {"4699": "846", "9999": "999"}
        facts["items"]["9999"] = {
            "id": "9999",
            "isInactive": False,
            "incomeAccount": {"id": "999"},
            "itemType": "NonInvtPart",
        }
        lines = [{"line": 1, "item_id": "1603", "amount": "185.95"}, {"item_id": "9999", "amount": "39.05"}]
        assert _refused(facts, lines) == "tax_account_not_on_invoice"

    def test_tax_reversed_per_account_cannot_exceed_what_that_account_posted(self):
        facts = _bv()
        invoice, _ = facts["invoices"][0]
        facts["invoices"] = [
            (
                invoice,
                _gl(
                    ("119", "debit", "2819"),
                    ("54", "credit", "2329.75"),
                    ("846", "credit", "20"),
                    ("906", "credit", "469.25"),
                ),
            )
        ]
        facts["profile"]["tax_item_accounts"] = {"4699": "846", "4700": "906"}
        facts["items"]["4700"] = {
            "id": "4700",
            "isInactive": False,
            "incomeAccount": {"id": "906"},
            "itemType": "NonInvtPart",
        }
        assert _refused(facts, BV_FIX) == "tax_reversal_exceeds_posted"
        ok = [{"line": 1, "item_id": "1603", "amount": "185.95"}, {"item_id": "4700", "amount": "39.05"}]
        reallocation.assess(lines=ok, **facts)

    @pytest.mark.parametrize(
        "change",
        [
            {"requires_review": True},
            {"completed_at": None},
        ],
    )
    def test_unfinalized_source_is_refused(self, change):
        facts = _us()
        facts["source"] = {
            **facts["source"],
            "completed_at": "2026-08-05T10:00:00Z",
            "requires_review": False,
            **change,
        }
        assert _refused(facts, US_FIX) == "source_not_final"

    def test_unfinalized_source_adjustment_is_refused(self):
        facts = _us()
        facts["source"] = {**facts["source"], "adjustments": [{"id": 1, "amount": "-2.8", "finalized": False}]}
        assert _refused(facts, US_FIX) == "source_not_final"

    def test_readback_ledger_is_compared_exactly(self):
        expected = {"debit": {"783": "185.95", "846": "39.05"}, "credit": {"119": "225.00"}}
        sub_cent = _gl(("119", "credit", "225"), ("783", "debit", "185.946"), ("846", "debit", "39.054"))
        exact = _gl(("119", "credit", "225"), ("783", "debit", "185.95"), ("846", "debit", "39.05"))
        try:
            accepted = reallocation._ledger_matches(sub_cent, expected)
        except reallocation.RefusalError:  # the shared ledger reader rejects sub-cent postings outright
            accepted = False
        assert not accepted
        assert reallocation._ledger_matches(exact, expected)


class TestReviewRoundTwo:
    """Findings of the 2026-09-25 T2 gate round 2 (wf_67359078-97c)."""

    def test_an_existing_line_may_keep_its_own_item_that_posts_to_a_tax_account(self):
        facts = _bv()
        credit = facts["credit"]
        credit["total"] = credit["subtotal"] = credit["applied"] = "235.0"
        credit["application_evidence"]["lines"][0]["amount"] = "235.0"
        vat_line = {
            "line": 2,
            "item": {"id": "7777"},
            "itemType": {"id": "NonInvtPart"},
            "quantity": "1.0",
            "rate": "10.0",
            "amount": "10.0",
            "taxCode": {"id": "4059"},
        }
        credit["line_evidence"]["lines"].append(vat_line)
        facts["credit_gl"] = _gl(("119", "credit", "235"), ("783", "debit", "225"), ("846", "debit", "10"))
        facts["profile"]["tax_accounts"] = ["846"]
        facts["items"]["7777"] = {
            "id": "7777",
            "isInactive": False,
            "incomeAccount": {"id": "846"},
            "itemType": "NonInvtPart",
        }
        facts["source"] = {
            **facts["source"],
            "total": "2584.0",
            "payment_total": "2584.0",
            "tax_total": "440.2",
            "included_tax_total": "440.2",
        }
        lines = [*BV_FIX, {"line": 2, "item_id": "7777", "amount": "10.00"}]
        reallocation.assess(lines=lines, **facts)
        # ...but a new line may not introduce that unconfigured item.
        extra = [
            {"line": 1, "item_id": "1603", "amount": "185.95"},
            {"line": 2, "item_id": "7777", "amount": "10.00"},
            {"item_id": "7777", "amount": "39.05"},
        ]
        assert _refused(facts, extra) == "item_not_allowed"

    @pytest.mark.parametrize(
        "rows",
        [
            (("119", "credit", "2.8"), ("783", "debit", "2.7")),  # unbalanced
            (("119", "credit", "2.8"), ("783", "debit", "3.8"), ("783", "credit", "-1.0")),  # negative
        ],
    )
    def test_malformed_gl_is_incomplete_evidence(self, rows):
        facts = _us()
        facts["credit_gl"] = _gl(*rows)
        assert _refused(facts, US_FIX) == "evidence_incomplete"


class TestReviewRoundThree:
    """Findings of the 2026-09-25 T2 gate round 3 (wf_5430d9c2-780) and the SB1 sandbox write."""

    def test_tax_on_an_unconfigured_liability_account_is_refused(self):
        facts = _bv()
        invoice, _ = facts["invoices"][0]
        facts["invoices"] = [
            (
                invoice,
                _gl(
                    ("119", "debit", "2819"),
                    ("54", "credit", "2329.75"),
                    ("846", "credit", "450.20"),
                    ("906", "credit", "39.05"),
                ),
            )
        ]
        assert _refused(facts, BV_FIX) == "tax_account_not_configured"

    def test_an_account_of_unknown_type_is_incomplete_evidence(self):
        facts = _us()
        facts["account_types"] = {k: v for k, v in ACCOUNT_TYPES.items() if k != "783"}
        assert _refused(facts, US_FIX) == "evidence_incomplete"

    def test_a_line_without_an_item_is_unsupported(self):
        facts = _us()
        facts["credit"]["line_evidence"]["lines"][0].pop("item")
        assert _refused(facts, US_FIX) == "credit_lines_unsupported"

    def test_a_credit_with_non_item_charges_is_refused(self):
        facts = _us()
        facts["credit"]["line_evidence"]["lines"][0].update(amount="2.5", rate="2.5")  # 0.30 of shipping
        assert _refused(facts, US_FIX) == "credit_has_non_item_charges"

    def test_an_unapplied_credit_is_refused(self):
        facts = _us()
        facts["credit"].update(applied="0.0", unapplied="2.8")
        assert _refused(facts, US_FIX) == "credit_not_applied"

    def test_each_tax_field_is_carried_from_the_template_when_a_line_lacks_it(self):
        facts = _bv()
        result = reallocation.assess(lines=BV_FIX, **facts)
        assert all(entry["taxCode"] == {"id": "4059"} for entry in result["proposed_fields"]["item"]["items"])

    def test_a_credit_without_location_needs_the_configured_correction_location(self):
        facts = _bv()
        result = reallocation.assess(lines=BV_FIX, **facts)
        assert result["proposed_fields"]["location"] == {"id": "81"}
        assert result["expected_after"]["location"] == "81"
        facts["profile"].pop("correction_location_id")
        assert _refused(facts, BV_FIX) == "credit_location_required"

    def test_a_credit_with_its_own_location_keeps_it(self):
        result = reallocation.assess(lines=US_FIX, **_us())
        assert "location" not in result["proposed_fields"] and "location" not in result["expected_after"]


class TestReviewRoundFour:
    """Findings of the 2026-09-25 T2 gate round 4 (wf_667216d4-61f): allowlists, not denylists."""

    @pytest.mark.parametrize("account", ["2100", "500", "130"])  # long-term liability, COGS, inventory asset
    def test_only_income_accounts_count_as_net(self, account):
        facts = _us()
        facts["credit_gl"] = _gl(
            ("119", "credit", "2.8"), ("783", "debit", "2.8"), (account, "debit", "1"), (account, "credit", "1")
        )
        assert _refused(facts, US_FIX) == "account_not_supported"

    def test_an_inventory_item_on_the_credit_is_unsupported(self):
        facts = _us()
        facts["items"]["1603"]["itemType"] = "InvtPart"
        assert _refused(facts, US_FIX) == "credit_lines_unsupported"

    def test_a_tax_refund_item_must_be_non_inventory_too(self):
        facts = _us()
        facts["items"]["5005"]["itemType"] = "InvtPart"
        assert _refused(facts, US_FIX) == "item_not_allowed"


def test_a_profile_is_only_used_for_its_own_account_and_subsidiary():
    from app.services.transaction_ops.refund_adjustments import RefundAdjustmentProfile

    profile = RefundAdjustmentProfile.model_validate(
        {
            "schema_version": 1,
            "account_id": "6738075",
            "subsidiary_id": "2",
            "tax_item_accounts": {"4699": "846"},
            "tax_reversal_reason_ids": ["102"],
            "correction_location_id": "81",
        }
    )
    assert reallocation.profile_matches_scope(profile, {"netsuite_account_id": "6738075", "subsidiary_id": "2"})
    assert not reallocation.profile_matches_scope(profile, {"netsuite_account_id": "6738075_SB1", "subsidiary_id": "2"})
    assert not reallocation.profile_matches_scope(profile, {"netsuite_account_id": "6738075", "subsidiary_id": "5"})
