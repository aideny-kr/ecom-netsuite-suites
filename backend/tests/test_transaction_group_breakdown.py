"""Group breakdown: every member in exactly one cause, causes only where their rule holds.

Built from two real Framework Inc groups (2026-09-28/29). "Order differences" (46 orders) was
mostly manual Solidus order adjustments that the order sync never carried to NetSuite, plus orders
whose invoice already matched Solidus. "Order + Tax differences" (72) was mostly orders the app had
already corrected, whose refund reason the subsidiary's settings did not count as a tax refund.
"""

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core.database import set_tenant_context
from app.models.chat import ChatMessage, ChatSession
from app.models.connection import Connection
from app.models.transaction_ops import TransactionCase, TransactionConfig
from app.models.transaction_source_snapshot import TransactionSourceSnapshot
from app.services.transaction_ops import group_breakdown as gb
from app.services.transaction_ops.case_groups import list_groups
from app.services.transaction_ops.state_service import StateError
from tests.conftest import create_test_user

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
    ):
        report = {
            "balance": {
                "status": balance,
                "currency": "USD",
                "target_currency": "USD",
                "missing_metrics": [],
                "amounts": {
                    "order_total": {"delta": delta, "source": source_total},
                    "tax": {"delta": tax},
                    "refunds": {"delta": "0.00"},
                },
                "adjustments": [],
            },
            "targets": [
                {"status": "fulfilled", "record_id": record_id or str(15_000_000 + len(ref) * 7 + int(ref[-3:]))}
            ],
            "refund_evidence": {"target": {"request_links": list(links)}},
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
        order = {
            "number": ref,
            "customer_type": customer_type,
            "adjustments": [
                {
                    "label": label,
                    "amount": amount,
                    "adjustable_type": "Spree::Order",
                    "source_type": None,
                    "finalized": False,
                }
                for label, amount in adjustments
            ],
        }
        self.db.add(
            TransactionSourceSnapshot(
                tenant_id=self.tenant.id,
                connection_id=self.source.id,
                order_reference=ref,
                connection_fingerprint="f" * 64,
                observed_at=NOW,
                source_updated_at=NOW,
                evidence_json={"version": 1, "evidence": {"orders": [order]}},
            )
        )
        await self.db.flush()

    async def corrected(self, case):
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
                    "accounting_review": {"case_id": str(case.id)},
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
    connections = []
    for provider in ("framework", "netsuite"):
        connection = Connection(tenant_id=tenant_a.id, provider=provider, label=provider, encrypted_credentials="x")
        db.add(connection)
        connections.append(connection)
    await db.flush()
    return World(db, tenant_a, user, *connections)


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
    assert {"fact": '"SKU Adjustment"', "orders": 3} in causes["source_adjustment_not_in_netsuite"]["facts"]
    assert causes["source_adjustment_not_in_netsuite"]["amounts"]["order_total"] == "-177.00"
    # An adjustment that does not equal the difference explains nothing.
    assert causes["business_priced_in_netsuite"]["order_references"] == ["R000000201"]
    assert causes["no_shared_cause"]["order_references"] == ["R000000301"]
    assert {"fact": "no saved Solidus detail yet", "orders": 1} in causes["no_shared_cause"]["facts"]
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
            "14388301": {"total": gb.Decimal("0.00"), "open": gb.Decimal("0"), "customers": {"FW Marketing Orders"}},
            "15941837": {"total": gb.Decimal("2743.00"), "open": gb.Decimal("59"), "customers": {"Doug Finch"}},
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
    assert {"fact": "not counted as a tax refund in this subsidiary's settings", "orders": 3} in reason_4["facts"]
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
    assert "source_adjustment_not_in_netsuite" in condensed and "Ram Price Adjustment" in condensed


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
