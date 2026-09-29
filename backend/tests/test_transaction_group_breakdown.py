"""Group breakdown: every member in exactly one cause, causes only where their rule holds.

Built from two real Framework Inc groups (2026-09-28/29). "Order differences" (46 orders) was
mostly manual Solidus order adjustments that the order sync never carried to NetSuite, plus orders
whose invoice already matched Solidus. "Order + Tax differences" (72) was mostly orders the app had
already corrected, whose refund reason the subsidiary's settings did not count as a tax refund.
"""

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core.database import set_tenant_context
from app.core.encryption import encrypt_credentials
from app.models.chat import ChatMessage, ChatSession
from app.models.connection import Connection
from app.models.transaction_ops import TransactionCase, TransactionConfig
from app.services.transaction_ops import group_breakdown as gb
from app.services.transaction_ops import source_snapshot
from app.services.transaction_ops.case_groups import list_groups
from app.services.transaction_ops.state_service import StateError
from tests.conftest import create_test_user
from tests.test_transaction_ops_runner import NOW as SNAPSHOT_AT
from tests.test_transaction_ops_runner import source_order

NOW = datetime.now(timezone.utc)


class World:
    def __init__(self, db, tenant, user, source, netsuite):
        self.db, self.tenant, self.user, self.source, self.netsuite = db, tenant, user, source, netsuite
        self.scope = {
            "record_type": "salesorder",
            "subsidiary_id": "1",
            "source_step_id": None,
            "netsuite_account_id": "6738075",
            "source_connection_id": str(source.id),
        }
        self.session = None

    async def case(
        self,
        ref,
        *,
        delta="-59.00",
        tax="0.00",
        source_total="2684.00",
        links=(),
        status="open",
        balance="difference",
        record_id=None,
        refunds="0.00",
        proofs=(),
        tax_source="0.00",
    ):
        report = {
            "balance": {
                "status": balance,
                "currency": "USD",
                "target_currency": "USD",
                "missing_metrics": [],
                "amounts": {
                    "order_total": {"delta": delta, "source": source_total},
                    "tax": {"delta": tax, "source": tax_source} if tax is not None else None,
                    "refunds": {"delta": refunds},
                },
                "adjustments": [],
            },
            "targets": [
                {
                    "status": "fulfilled",
                    "record_id": str(15_000_000 + len(ref) * 7 + int(ref[-3:])) if record_id is None else record_id,
                }
            ],
            "refund_evidence": {"target": {"request_links": list(links), "tax_adjustments": list(proofs)}},
        }
        row = TransactionCase(
            id=uuid4(),
            tenant_id=self.tenant.id,
            case_key=uuid4().hex,
            order_reference=ref,
            scope_json=self.scope,
            status=status,
            first_observed_at=NOW,
            last_observed_at=NOW,
            latest_report_json=report,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def solidus(self, ref, *, customer_type="consumer", adjustments=()):
        """Saved through the app's own saver, so the breakdown reads it through the canonical loader."""
        evidence = source_order()
        order = evidence["orders"][0]
        order.update(
            number=ref,
            customer_type=customer_type,
            adjustments=[
                {
                    "id": str(900 + index),
                    "label": label,
                    "amount": amount,
                    "adjustable_type": "Spree::Order",
                    "adjustable_id": "1",
                    "source_type": None,
                    "finalized": False,
                }
                for index, (label, amount) in enumerate(adjustments)
            ],
        )
        evidence.update(
            source_transport="solidus_direct",
            connection_id=str(self.source.id),
            _connection_fingerprint=hashlib.sha256(self.source.encrypted_credentials.encode()).hexdigest(),
        )
        assert await source_snapshot.save(self.db, self.tenant.id, self.source.id, ref, evidence, now=SNAPSHOT_AT)

    async def corrected(self, case, record_type="creditmemo", record_id="15788939"):
        if self.session is None:
            self.session = ChatSession(tenant_id=self.tenant.id, user_id=self.user.id, title="fixes")
            self.db.add(self.session)
            await self.db.flush()
        self.db.add(
            ChatMessage(
                tenant_id=self.tenant.id,
                session_id=self.session.id,
                role="assistant",
                content="card",
                structured_output={
                    "status": "approved",
                    "accounting_review": {"case_id": str(case.id), "record_type": record_type, "record_id": record_id},
                    "accounting_verification": {"status": "verified"},
                },
            )
        )
        await self.db.flush()

    async def config(self, tax_reasons=("102",)):
        self.db.add(
            TransactionConfig(
                tenant_id=self.tenant.id,
                config_key=uuid4().hex,
                name="Inc",
                source_connection_id=self.source.id,
                netsuite_connection_id=self.netsuite.id,
                netsuite_account_id="6738075",
                subsidiary_id="1",
                record_type="salesorder",
                interval_minutes=1440,
                max_api_calls=100,
                max_orders=100,
                deadline_seconds=900,
                created_by=self.user.id,
                mapping_json={"refund_adjustments": {"tax_reversal_reason_ids": list(tax_reasons)}},
            )
        )
        await self.db.flush()

    async def group_id(self):
        groups = (await list_groups(self.db, self.tenant.id))["groups"]
        assert len(groups) == 1, groups
        return groups[0]["group_id"]


@pytest.fixture
async def world(db, tenant_a):
    await set_tenant_context(db, str(tenant_a.id))
    user, _ = await create_test_user(db, tenant_a, role_name="admin")
    source = Connection(
        tenant_id=tenant_a.id,
        provider="solidus",
        label="Solidus",
        status="active",
        metadata_json={"api_profile": "framework_sync"},
        encrypted_credentials=encrypt_credentials(
            {
                "base_url": "https://private-direct-access.frame.work/api/",
                "auth_type": "api_key",
                "header_name": "X-Store-Token",
                "token": "test",
                "api_profile": "framework_sync",
            }
        ),
    )
    netsuite = Connection(tenant_id=tenant_a.id, provider="netsuite", label="NetSuite", encrypted_credentials="x")
    db.add_all([source, netsuite])
    await db.flush()
    return World(db, tenant_a, user, source, netsuite)


def link(reason, credit_memo="15788939"):
    return {"reason_id": reason, "credit_memo_id": credit_memo, "stage": "refund_verified"}


def by_cause(result):
    return {cause["cause"]: cause for cause in result["causes"]}


async def test_every_order_lands_in_exactly_one_cause(world, monkeypatch):
    await world.config()
    touchpad = [await world.case(f"R00000010{i}", delta=d) for i, d in enumerate(("-59.00", "-59.02", "-58.98"))]
    for case, amount in zip(touchpad, ("-59.0", "-59.02", "-58.98")):
        await world.solidus(case.order_reference, adjustments=[("SKU Adjustment", amount)])
    business = await world.case("R000000201", delta="-6449.00")
    await world.solidus("R000000201", customer_type="business", adjustments=[("reseller discount", "-100.0")])
    unexplained = await world.case("R000000301", delta="-12.00")  # no saved Solidus order at all
    monkeypatch.setattr(gb, "_invoices", _no_invoices)

    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    causes = by_cause(result)

    assert result["orders"] == 5 == sum(c["orders"] for c in result["causes"])
    assigned = [ref for c in result["causes"] for ref in c["order_references"]]
    assert sorted(assigned) == sorted(c.order_reference for c in [*touchpad, business, unexplained])
    # The cents are not leftovers: each adjustment equals its own difference exactly.
    assert causes["source_adjustment_not_in_netsuite"]["orders"] == 3
    assert {"kind": "adjustment_label", "fact": '"SKU Adjustment"', "orders": 3} in causes[
        "source_adjustment_not_in_netsuite"
    ]["facts"]
    assert causes["source_adjustment_not_in_netsuite"]["amounts"]["order_total"] == "-177.00"
    # An adjustment that does not equal the difference explains nothing.
    assert causes["business_priced_in_netsuite"]["order_references"] == ["R000000201"]
    assert causes["no_shared_cause"]["order_references"] == ["R000000301"]
    assert {"kind": "missing_source", "fact": "no saved Solidus detail yet", "orders": 1} in causes["no_shared_cause"][
        "facts"
    ]
    assert result["causes"][-1]["cause"] == "no_shared_cause"
    assert result["totals"]["order_total"] == "-6638.00"
    assert result["scope"] == {"review_run_ids": None, "status": None, "search": ""}


async def _no_invoices(db, tenant_id, config, order_ids):
    return {}, "complete"


async def test_an_invoice_that_already_matches_solidus_outranks_the_adjustment(world, monkeypatch):
    await world.config()
    zeroed = await world.case("R000000401", delta="-449.00", source_total="0.00", record_id="14388301")
    await world.solidus("R000000401", adjustments=[("Adjusted to zero", "-449.0")])
    still_open = await world.case("R000000402", delta="-59.00", source_total="2684.00", record_id="15941837")
    await world.solidus("R000000402", adjustments=[("SKU Adjustment", "-59.0")])
    seen = []

    async def invoices(db, tenant_id, config, order_ids):
        seen.append(sorted(order_ids))
        return {
            "14388301": {
                "total": gb.Decimal("0.00"),
                "tax": gb.Decimal("0"),
                "open": gb.Decimal("0"),
                "customers": {"FW Marketing Orders"},
            },
            "15941837": {
                "total": gb.Decimal("2743.00"),
                "tax": gb.Decimal("0"),
                "open": gb.Decimal("59"),
                "customers": {"Doug Finch"},
            },
        }, "complete"

    monkeypatch.setattr(gb, "_invoices", invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    causes = by_cause(result)

    assert seen == [["14388301", "15941837"]]
    assert causes["invoice_matches_source"]["order_references"] == [zeroed.order_reference]
    assert causes["invoice_matches_source"]["amounts"]["open_on_invoices"] == "0.00"
    assert causes["source_adjustment_not_in_netsuite"]["order_references"] == [still_open.order_reference]
    assert causes["source_adjustment_not_in_netsuite"]["amounts"]["open_on_invoices"] == "59.00"
    assert result["checked"]["netsuite"] == "complete"


async def test_a_slow_netsuite_still_returns_what_saved_evidence_explains(world, monkeypatch):
    await world.config()
    await world.case("R000000501", delta="-59.00")
    await world.solidus("R000000501", adjustments=[("SKU Adjustment", "-59.0")])
    await world.case("R000000502", delta="-10.00")

    async def slow(db, tenant_id, config, order_ids):
        return None, "timed_out"

    monkeypatch.setattr(gb, "_invoices", slow)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())

    assert result["checked"]["netsuite"] == "timed_out"
    assert by_cause(result)["source_adjustment_not_in_netsuite"]["orders"] == 1
    assert by_cause(result)["no_shared_cause"]["orders"] == 1
    assert "open_on_invoices" not in by_cause(result)["no_shared_cause"]["amounts"]


async def test_the_invoice_deadline_is_enforced_inside_the_read(world, monkeypatch):
    # The limit is code, not a request to the reader: a hung read ends as "timed_out".
    import asyncio
    from contextlib import asynccontextmanager

    from app.services.transaction_ops import netsuite_reader

    @asynccontextmanager
    async def hung_reader(*args, **kwargs):
        await asyncio.sleep(5)
        yield None

    monkeypatch.setattr(netsuite_reader, "authenticated_reader", hung_reader)
    monkeypatch.setattr(gb, "NETSUITE_SECONDS", 0.05)
    await world.config()
    config = (await world.db.execute(gb.select(TransactionConfig))).scalars().one()
    assert await gb._invoices(world.db, world.tenant.id, config, ["15941837"]) == (None, "timed_out")


async def test_orders_already_corrected_here_point_to_the_refund_setting(world, monkeypatch):
    await world.config(tax_reasons=("102",))
    fixed = [await world.case(f"R00000060{i}", delta="-2.80", tax="-2.80", links=[link("4")]) for i in range(3)]
    for case in fixed:
        await world.corrected(case)
    odd = await world.case("R000000701", delta="-3.00", tax="-3.00", links=[link("3")])
    await world.corrected(odd)
    await world.case("R000000801", delta="-100.45", tax="-100.46", links=[link("2", credit_memo=None)])
    monkeypatch.setattr(gb, "_invoices", _no_invoices)

    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    corrected = [c for c in result["causes"] if c["cause"] == "corrected_in_app"]

    reason_4 = next(c for c in corrected if c["orders"] == 3)
    assert reason_4["next_step"] == "settings_change" and "reason 4" in reason_4["next_label"]
    assert {
        "kind": "setting",
        "fact": "not counted as a tax refund in this subsidiary's settings",
        "orders": 3,
    } in reason_4["facts"]
    # One order is too little evidence to change a setting that affects every future refund.
    reason_3 = next(c for c in corrected if c["orders"] == 1)
    assert reason_3["next_step"] == "recheck"
    assert by_cause(result)["refund_without_credit_memo"]["order_references"] == ["R000000801"]


async def test_the_model_never_receives_an_amount(world, monkeypatch):
    await world.config()
    await world.case("R000000901", delta="-90008.99")
    await world.solidus("R000000901", adjustments=[("Ram Price Adjustment", "-90008.99")])
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())

    condensed = json.dumps(gb.condensed_for_model(result))
    assert "90008" not in condensed
    assert '"amounts"' not in condensed and '"totals"' not in condensed and '"open_on_invoices"' not in condensed
    assert "source_adjustment_not_in_netsuite" in condensed and "Ram Price Adjustment" not in condensed


async def test_one_order_is_a_group_of_one(world, monkeypatch):
    await world.config()
    case = await world.case("R000001001", delta="-59.00")
    await world.solidus("R000001001", adjustments=[("SKU Adjustment", "-59.0")])
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, case_id=str(case.id))
    assert result["orders"] == 1 and result["causes"][0]["cause"] == "source_adjustment_not_in_netsuite"
    assert result["scope"] is None and result["case_id"] == str(case.id)


async def test_another_tenants_case_is_never_read(world, tenant_b):
    await world.config()
    case = await world.case("R000001101")
    with pytest.raises(StateError):
        await gb.breakdown(world.db, tenant_b.id, case_id=str(case.id))


async def test_a_group_or_a_case_is_required(world):
    with pytest.raises(StateError):
        await gb.breakdown(world.db, world.tenant.id)


async def test_a_credit_that_left_the_tax_in_place_goes_to_the_group_fix(world, monkeypatch):
    # The US tax split of 2026-09-26: the refund's credit memo was booked to revenue, so NetSuite
    # still carries the tax. The group fix corrects these (credit reallocation), one proof each.
    await world.config()
    await world.case("R000001201", delta="-2.80", tax="-2.80", links=[link("4")])
    await world.case("R000001202", delta="-3.10", tax="-3.10", links=[link("4")])
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    cause = by_cause(result)["tax_left_after_credit"]
    assert cause["orders"] == 2 and cause["next_step"] == "prepare_corrections"


async def test_a_tax_difference_is_never_called_a_business_price(world, monkeypatch):
    # Live 2026-09-29: a 1-cent tax difference on a business order was labelled "priced in NetSuite".
    await world.config()
    await world.case("R000001301", delta="-0.01", tax="-0.02")
    await world.solidus("R000001301", customer_type="business")
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    assert [c["cause"] for c in result["causes"]] == ["no_shared_cause"]


async def test_a_correction_counts_only_while_its_credit_memo_is_still_on_the_order(world, monkeypatch):
    # Review round 1 of #356: any past verified card used to mark a case corrected forever, even
    # after the order moved on to a different credit memo.
    await world.config()
    kept = [await world.case(f"R00000140{i}", delta="-2.80", tax="-2.80", links=[link("4")]) for i in range(3)]
    for case in kept:
        await world.corrected(case)
    moved = await world.case("R000001501", delta="-2.80", tax="-2.80", links=[link("4", credit_memo="16000001")])
    await world.corrected(moved)  # the card corrected 15788939, which this order no longer carries
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    corrected = by_cause(result)["corrected_in_app"]
    assert sorted(corrected["order_references"]) == sorted(c.order_reference for c in kept)
    assert corrected["next_step"] == "settings_change"
    assert "R000001501" not in corrected["order_references"]


async def test_an_ordinary_credit_proof_without_tax_does_not_hide_the_tax_left_behind(world, monkeypatch):
    # Review round 1 of #356: any recognised proof used to exclude the case, even one carrying no tax.
    await world.config()
    await world.case(
        "R000001601",
        delta="-2.80",
        tax="-2.80",
        links=[link("4")],
        proofs=[{"kind": "credit_memo", "tax_amount": "0", "amount": "10.00"}],
    )
    await world.case(
        "R000001602",
        delta="-2.80",
        tax="-2.80",
        links=[link("4")],
        proofs=[{"kind": "credit_memo", "tax_amount": "1.40", "amount": "10.00"}],
    )
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    assert by_cause(result)["tax_left_after_credit"]["order_references"] == ["R000001601"]


async def test_the_server_names_the_amount_each_cause_shows(world, monkeypatch):
    # Review round 1 of #356: the card guessed between order total and tax, and missed refunds.
    await world.config()
    await world.case("R000001701", delta="0.00", tax="0.00", refunds="-5.00")
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    assert result["causes"][0]["primary"] == {"metric": "refunds", "amount": "-5.00"}
    assert result["causes"][0]["next_pill"] == "Review"


async def test_the_netsuite_count_is_only_orders_actually_checked(world, monkeypatch):
    await world.config()
    await world.case("R000001801", delta="-12.00")
    await world.case("R000001802", delta="-13.00", record_id="")  # no single sales order to look up
    seen = []

    async def invoices(db, tenant_id, config, order_ids):
        seen.append(list(order_ids))
        return {}, "complete"

    monkeypatch.setattr(gb, "_invoices", invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    assert result["checked"]["netsuite_orders"] == 1 and len(seen[0]) == 1


async def test_the_model_never_reads_customer_names_or_numbers_inside_labels(world, monkeypatch):
    # Review round 1 of #356: free text from Solidus and NetSuite reached the model verbatim.
    await world.config()
    await world.case("R000001901", delta="-50.00", source_total="10.00", record_id="15000001")
    await world.solidus("R000001901", adjustments=[("Refund $50 per call 5%", "-50.0")])
    await world.case("R000001902", delta="-5.00", source_total="20.00", record_id="15000002")
    await world.case("R000001903", delta="-6.00", source_total="30.00", record_id="15000003")

    async def invoices(db, tenant_id, config, order_ids):
        row = {"total": gb.Decimal("20.00"), "tax": gb.Decimal("0"), "open": gb.Decimal("0"), "customers": {"Jane Doe"}}
        return {"15000002": row, "15000003": {**row, "total": gb.Decimal("30.00")}}, "complete"

    monkeypatch.setattr(gb, "_invoices", invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    assert any(f["fact"] == "Jane Doe" for f in by_cause(result)["invoice_matches_source"]["facts"])
    condensed = json.dumps(gb.condensed_for_model(result))
    assert "Jane Doe" not in condensed and "50" not in condensed and "Refund" not in condensed


async def test_an_invoice_with_the_right_total_but_the_wrong_tax_is_not_called_right(world, monkeypatch):
    # Review round 2 of #356: the gross total alone let a wrong tax split pass as "books right".
    await world.config()
    await world.case("R000002001", delta="-10.00", source_total="100.00", tax_source="10.00", record_id="15000011")

    async def invoices(db, tenant_id, config, order_ids):
        return {
            "15000011": {
                "total": gb.Decimal("100.00"),
                "tax": gb.Decimal("20.00"),
                "open": gb.Decimal("0"),
                "customers": set(),
            }
        }, "complete"

    monkeypatch.setattr(gb, "_invoices", invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    assert "invoice_matches_source" not in by_cause(result)


async def test_only_a_credit_memo_correction_still_on_the_order_counts(world, monkeypatch):
    # Review round 2 of #356: an invoice or sales order correction counted as "corrected" forever.
    await world.config()
    case = await world.case("R000002101", delta="-2.80", tax="-2.80", links=[link("4")])
    await world.corrected(case, record_type="invoice", record_id="15733697")
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    assert "corrected_in_app" not in by_cause(result)


async def test_the_setting_names_only_the_reason_on_the_corrected_credit(world, monkeypatch):
    # Review round 2 of #356: reasons came from every refund link, so an unrelated merchandise
    # refund's reason would have been proposed as a tax refund reason.
    await world.config()
    orders = [
        await world.case(
            f"R00000220{i}", delta="-2.80", tax="-2.80", links=[link("4"), link("7", credit_memo="17000001")]
        )
        for i in range(3)
    ]
    for case in orders:
        await world.corrected(case)
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    corrected = by_cause(result)["corrected_in_app"]
    assert corrected["next_step"] == "settings_change"
    assert "reason 4" in corrected["next_label"] and "7" not in corrected["next_label"]


async def test_a_missing_tax_amount_is_never_read_as_zero(world, monkeypatch):
    # Review round 2 of #356: a missing metric became 0, so a business order with no tax evidence
    # was called "priced in NetSuite".
    await world.config()
    await world.case("R000002301", delta="-10.00", tax=None)
    await world.solidus("R000002301", customer_type="business")
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(
        world.db,
        world.tenant.id,
        case_id=str(
            (await world.db.execute(gb.select(TransactionCase).where(TransactionCase.order_reference == "R000002301")))
            .scalars()
            .one()
            .id
        ),
    )
    assert [c["cause"] for c in result["causes"]] == ["no_shared_cause"]


async def test_a_cause_accounts_for_every_part_of_the_difference(world, monkeypatch):
    # Review round 3 of #356, the same shape as rounds 1 and 2: a rule that checked one part of the
    # difference and ignored another. Every cause now needs every other part to be a known zero.
    await world.config()
    await world.case("R000002401", delta="-59.00", tax="-5.00")  # the adjustment explains no tax
    await world.solidus("R000002401", adjustments=[("SKU Adjustment", "-59.0")])
    await world.case("R000002402", delta="-10.00", source_total="100.00", refunds="-50.00", record_id="15000021")
    corrected = await world.case("R000002403", delta="-12.80", tax="-2.80", links=[link("4")])  # more than tax
    await world.corrected(corrected)
    await world.case("R000002404", delta="2.80", tax="2.80", links=[link("4")])  # NetSuite tax is lower

    async def invoices(db, tenant_id, config, order_ids):
        return {
            "15000021": {
                "total": gb.Decimal("100.00"),
                "tax": gb.Decimal("0"),
                "open": gb.Decimal("0"),
                "customers": set(),
            }
        }, "complete"

    monkeypatch.setattr(gb, "_invoices", invoices)
    groups = (await list_groups(world.db, world.tenant.id))["groups"]
    causes = set()
    for group in groups:
        result = await gb.breakdown(world.db, world.tenant.id, group_id=group["group_id"])
        causes |= {c["cause"] for c in result["causes"]}
    assert causes <= {"no_shared_cause", "refund_without_credit_memo"}, causes


async def test_a_saved_order_from_a_rotated_connection_is_not_evidence(world, monkeypatch):
    # Review round 3 of #356: the saved Solidus order is read through the canonical loader, which
    # rejects it once the connection's credentials change.
    await world.config()
    await world.case("R000002501", delta="-59.00")
    await world.solidus("R000002501", adjustments=[("SKU Adjustment", "-59.0")])
    world.source.encrypted_credentials = encrypt_credentials(
        {
            "base_url": "https://private-direct-access.frame.work/api/",
            "auth_type": "api_key",
            "header_name": "X-Store-Token",
            "token": "rotated",
            "api_profile": "framework_sync",
        }
    )
    await world.db.flush()
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    assert [c["cause"] for c in result["causes"]] == ["no_shared_cause"]


async def test_an_invoice_shared_by_several_orders_is_never_split_between_them(world, monkeypatch):
    # Review round 3 of #356: a consolidated invoice's full total was counted for every order it covers.
    from contextlib import asynccontextmanager

    from app.services.transaction_ops import netsuite_bulk, netsuite_reader

    @asynccontextmanager
    async def reader(*args, **kwargs):
        yield object()

    answers = iter(
        [
            [{"invoice_id": 1, "order_id": 11}, {"invoice_id": 1, "order_id": 12}, {"invoice_id": 2, "order_id": 13}],
            [
                {"id": 1, "foreigntotal": 500, "taxtotal": 50, "foreignamountunpaid": 0, "customer": "A", "parents": 2},
                {"id": 2, "foreigntotal": 80, "taxtotal": 0, "foreignamountunpaid": 80, "customer": "B", "parents": 1},
            ],
        ]
    )

    async def query(reader, sql, limit=1000):
        return next(answers)

    monkeypatch.setattr(netsuite_reader, "authenticated_reader", reader)
    monkeypatch.setattr(netsuite_bulk, "query", query)
    await world.config()
    config = (await world.db.execute(gb.select(TransactionConfig))).scalars().one()
    by_order, status = await gb._invoices(world.db, world.tenant.id, config, ["11", "12", "13"])
    assert status == "complete" and set(by_order) == {"13"}
    assert by_order["13"]["total"] == gb.Decimal("80")


async def test_says_so_when_saved_solidus_orders_cannot_be_read(world, monkeypatch):
    # Found validating round 3 live: when the loader refuses (here, credentials that cannot be
    # decrypted), the card must not say "no saved Solidus detail yet".
    await world.config()
    await world.case("R000002601", delta="-59.00")
    await world.solidus("R000002601", adjustments=[("SKU Adjustment", "-59.0")])
    world.source.encrypted_credentials = "not-decryptable"
    await world.db.flush()
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    assert result["checked"]["saved_source"] == "unavailable"
    assert not any("no saved Solidus detail" in f["fact"] for c in result["causes"] for f in c["facts"])


async def test_an_invoice_with_another_order_outside_the_request_is_not_this_orders(world, monkeypatch):
    # Packet review F1: shared invoices were detected only among the requested orders.
    from contextlib import asynccontextmanager

    from app.services.transaction_ops import netsuite_bulk, netsuite_reader

    @asynccontextmanager
    async def reader(*args, **kwargs):
        yield object()

    answers = iter(
        [
            [{"invoice_id": 1, "order_id": 11}],
            [{"id": 1, "foreigntotal": 500, "taxtotal": 50, "foreignamountunpaid": 0, "customer": "A", "parents": 2}],
        ]
    )

    async def query(reader, sql, limit=1000):
        return next(answers)

    monkeypatch.setattr(netsuite_reader, "authenticated_reader", reader)
    monkeypatch.setattr(netsuite_bulk, "query", query)
    await world.config()
    config = (await world.db.execute(gb.select(TransactionConfig))).scalars().one()
    by_order, status = await gb._invoices(world.db, world.tenant.id, config, ["11"])
    assert status == "complete" and by_order == {}


async def test_a_missing_amount_is_shown_as_unknown_not_zero(world, monkeypatch):
    # Packet review F2: the card summed missing amounts as zero and showed the sum as complete.
    await world.config()
    await world.case("R000002701", delta=None, tax=None, refunds=None)
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    cause = result["causes"][0]
    assert cause["amounts"]["order_total"] is None and result["totals"]["order_total"] is None
    assert cause["primary"] is None


async def test_each_cause_names_its_exact_cases(world, monkeypatch):
    # Packet review F3: follow-up prompts carried only order references, which do not identify a case
    # across configurations; the evidence tool needs the case id.
    await world.config()
    case = await world.case("R000002801", delta="-12.00")
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    assert result["causes"][0]["case_ids"] == [str(case.id)]
    assert "case_ids" not in json.dumps(gb.condensed_for_model(result))


async def test_without_the_subsidiarys_settings_no_setting_change_is_suggested(world, monkeypatch):
    # Packet review F4: an unresolved configuration was read as "the settings do not count this reason".
    orders = [await world.case(f"R00000290{i}", delta="-2.80", tax="-2.80", links=[link("4")]) for i in range(3)]
    for case in orders:
        await world.corrected(case)
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())  # no config seeded
    corrected = by_cause(result)["corrected_in_app"]
    assert corrected["next_step"] == "recheck"
    facts = [f["fact"] for f in corrected["facts"]]
    assert "not counted as a tax refund in this subsidiary's settings" not in facts
    assert "the subsidiary's refund settings could not be read" in facts


async def test_an_unexpected_customer_type_never_reaches_the_model_as_text(world, monkeypatch):
    # Packet review F5: customer_type is free text from Solidus.
    await world.config()
    await world.case("R000003001", delta="-59.00")
    await world.solidus("R000003001", customer_type="Jane Doe 123.45", adjustments=[("SKU Adjustment", "-59.0")])
    monkeypatch.setattr(gb, "_invoices", _no_invoices)
    result = await gb.breakdown(world.db, world.tenant.id, group_id=await world.group_id())
    assert "Jane" not in json.dumps(gb.condensed_for_model(result))
    assert {"kind": "customer_type", "fact": "other customer", "orders": 1} in result["causes"][0]["facts"]
