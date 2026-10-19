"""The order's saved Stripe lines (head-to-head 2026-10-18, R072103456), as the agent reads them.

Packet review round 1 (gpt-6-astra) on fbaa9831: the exact-number match ran after the row limit
(R5), dates were ingestion dates (R4), a failed read aborted everything (R2), and orders were
matched by tenant and number alone (R3).
"""

from decimal import Decimal

import pytest

from app.models.canonical import PayoutLine
from app.services.transaction_ops import stripe_evidence
from tests.conftest import create_test_tenant


def _line(tenant, ident, order, *, amount="100.00", kind="charge", currency="USD", created=1786253784):
    return PayoutLine(
        tenant_id=tenant,
        dedupe_key=ident,
        source="stripe",
        source_id=ident,
        line_type=kind,
        amount=Decimal(amount),
        fee=Decimal("0"),
        net=Decimal(amount),
        currency=currency,
        description=f"Framework Marketplace Order ID: {order}-XC8PUK5B",
        related_order_id=ident,
        raw_data={"created": created} if created is not None else {},
    )


async def test_the_exact_order_is_found_even_behind_many_longer_numbers(db):
    tenant = await create_test_tenant(db, name="Stripe Exact Corp")
    db.add_all([_line(tenant.id, f"ch_long_{i}", "R12345") for i in range(stripe_evidence.MAX_ROWS)])
    db.add(_line(tenant.id, "re_1234", "R1234", amount="-4.82", kind="refund"))
    await db.flush()
    section = await stripe_evidence.order_section(db, tenant.id, "R1234", "USD")
    assert section["available"] is True
    assert [(x["id"], x["type"], x["amount"]) for x in section["lines"]] == [("re_1234", "refund", "-4.82")]


async def test_the_date_is_when_stripe_moved_the_money_not_when_we_synced_it(db):
    tenant = await create_test_tenant(db, name="Stripe Date Corp")
    db.add(_line(tenant.id, "re_dated", "R072103456", amount="-2901.38", kind="refund", created=1788393600))
    db.add(_line(tenant.id, "ch_undated", "R072103456", created=None))
    await db.flush()
    section = await stripe_evidence.order_section(db, tenant.id, "R072103456", "USD")
    dates = {x["id"]: x["date"] for x in section["lines"]}
    assert dates == {"re_dated": "2026-09-03", "ch_undated": None}


async def test_only_lines_in_the_orders_currency_belong_to_it(db):
    tenant = await create_test_tenant(db, name="Stripe Currency Corp")
    db.add(_line(tenant.id, "ch_usd", "R555555555"))
    db.add(_line(tenant.id, "ch_eur", "R555555555", currency="EUR"))
    await db.flush()
    usd = await stripe_evidence.order_section(db, tenant.id, "R555555555", "USD")
    assert [x["id"] for x in usd["lines"]] == ["ch_usd"]
    unknown = await stripe_evidence.order_section(db, tenant.id, "R555555555", None)
    assert unknown == {"available": False, "reason": "order currency unknown"}


async def test_a_failed_read_is_named_and_leaves_the_session_usable(db, monkeypatch):
    tenant = await create_test_tenant(db, name="Stripe Failure Corp")
    await db.flush()
    real = db.execute

    async def failing(statement, *args, **kwargs):
        if "payout_lines" in str(statement):
            await real(__import__("sqlalchemy").text("SELECT 1/0"))  # aborts inside the savepoint
        return await real(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", failing)
    section = await stripe_evidence.order_section(db, tenant.id, "R072103456", "USD")
    assert section == {"available": False, "reason": "stripe lines unreadable"}
    monkeypatch.setattr(db, "execute", real)
    assert (await db.execute(__import__("sqlalchemy").text("SELECT 1"))).scalar() == 1


@pytest.mark.parametrize("reference", ["R123", "R12%", "R1_34", "", None])
async def test_a_reference_that_is_not_an_order_number_reads_nothing(db, reference):
    section = await stripe_evidence.order_section(db, "00000000-0000-0000-0000-000000000000", reference, "USD")
    assert section == {"available": False, "reason": "no saved Stripe lines for this order"}


def test_the_agents_evidence_summary_carries_the_stripe_section():
    from app.services.transaction_ops.accounting_evidence import completion_evidence_summary

    section = {"available": True, "lines": [{"type": "refund", "amount": "-2901.38", "id": "re_1"}], "truncated": False}
    summary = completion_evidence_summary({"sections": {"stripe": section}})
    assert summary["stripe"] == section
