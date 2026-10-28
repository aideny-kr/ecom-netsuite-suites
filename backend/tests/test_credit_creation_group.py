"""Group preparation of a zero-total credit (the open-period half of a reallocation).

Staging 2026-10-22: 75 of Framework's open cases have the order total and refunds matching Solidus
but NetSuite's tax higher. The refund credit booked the VAT part to income, and 72 of those credits
sit in closed periods, so the group fix's existing-credit reallocation sets them aside as
period_locked. The fix the agent proposes one order at a time in chat (CM12243, CM12245, CM12389,
CM12390, all verified) is a zero-total credit in the open period. The group derives the same lines
on the server: the verified exemplar's two items, sized by this order's own figures, accepted only
by the same outcome check.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import credit_creation as cc
from tests.test_credit_creation import _refunded_credit_order

TENANT = uuid4()
CASE = str(uuid4())
CONFIG = str(uuid4())
EXEMPLAR_ID = "11111111-1111-1111-1111-111111111111"
# CM12389's lines (R183209823): a different order and amount; only the items carry over.
EXEMPLAR_LINES = [{"item_id": "4699", "amount": "35.92"}, {"item_id": "1603", "amount": "-35.92"}]


def _tax_split_order():
    """R166593377's figures with the gross already right: the credit refunded the VAT-inclusive
    amount to sales returns, so NetSuite's tax is 71.85 above the source and its net 71.85 below."""
    facts = _refunded_credit_order()
    facts["source"].update(total="3621.00", tax_total="578.15")
    return facts


class TestDerive:
    def test_the_exemplars_items_sized_by_this_orders_tax_gap(self):
        facts = _tax_split_order()
        lines = cc.derive_zero_total(EXEMPLAR_LINES, **facts)
        assert lines == [{"item_id": "4699", "amount": "71.85"}, {"item_id": "1603", "amount": "-71.85"}]
        result = cc.assess(lines=lines, memo="x", **facts)
        assert result["settlement"] == "none"
        assert result["balance"]["after"] == result["balance"]["source"]

    def test_a_gross_difference_is_not_a_reclass(self):
        facts = _tax_split_order()
        facts["source"]["total"] = "3600.00"
        with pytest.raises(cc.RefusalError) as exc:
            cc.derive_zero_total(EXEMPLAR_LINES, **facts)
        assert exc.value.code == "derive_unsupported_shape"
        assert exc.value.detail["gross_gap"] == "21.00"

    def test_tax_below_the_source_is_not_this_fix(self):
        facts = _tax_split_order()
        facts["source"].update(total="3621.00", tax_total="700.00")
        with pytest.raises(cc.RefusalError) as exc:
            cc.derive_zero_total(EXEMPLAR_LINES, **facts)
        assert exc.value.code == "derive_unsupported_shape"
        assert exc.value.detail["tax_gap"] == "-50.00"

    @pytest.mark.parametrize(
        "exemplar",
        [
            # Not a zero-total reclass at all: one positive line applied to an invoice.
            [{"item_id": "1471", "amount": "674.73"}],
            # Three lines: the group never invents a split the exemplar did not have.
            [*EXEMPLAR_LINES, {"item_id": "1603", "amount": "-1.00"}],
            # The positive item must be one of the subsidiary's configured tax items.
            [{"item_id": "1603", "amount": "35.92"}, {"item_id": "1471", "amount": "-35.92"}],
            # Unreadable amounts.
            [{"item_id": "4699", "amount": "x"}, {"item_id": "1603", "amount": "-35.92"}],
        ],
    )
    def test_only_a_two_line_tax_reclass_exemplar_is_followed(self, exemplar):
        with pytest.raises(cc.RefusalError) as exc:
            cc.derive_zero_total(exemplar, **_tax_split_order())
        assert exc.value.code == "derive_unsupported_shape"

    def test_the_outcome_check_still_decides(self):
        # The exemplar's income item posts to an account this order's credits never debited.
        facts = _tax_split_order()
        lines = cc.derive_zero_total([{"item_id": "4699", "amount": "1"}, {"item_id": "1471", "amount": "-1"}], **facts)
        with pytest.raises(cc.RefusalError) as exc:
            cc.assess(lines=lines, memo="x", **facts)
        assert exc.value.detail["reason"] == "reversal_account_not_on_order_credit"


@pytest.fixture
def member(monkeypatch):
    """prepare_group_member with the NetSuite reads replaced by the R166593377-shaped order."""
    state = {"facts": _tax_split_order(), "exemplar": (EXEMPLAR_ID, EXEMPLAR_LINES), "gathered": []}
    review = {
        "scope": {"netsuite_account_id": "6738075", "subsidiary_id": "1", "record_type": "salesorder"},
        "config_id": CONFIG,
        "netsuite_connection_id": str(uuid4()),
        "native_mcp_connector_id": str(uuid4()),
        "connection_active": True,
        "configuration_status": "scoped_configuration_found",
    }

    async def case(db, tenant_id, case_id):
        return SimpleNamespace(id=case_id, scope_json={}, latest_report_json={})

    async def context(db, tenant_id, scope, report):
        return review

    async def exemplar(db, tenant_id, config_id):
        state["exemplar_config"] = config_id
        return state["exemplar"]

    async def gather(db, tenant_id, case_id, item_ids, *, reserve=0):
        state["gathered"].append((sorted(item_ids), reserve))
        facts = state["facts"]
        invoice = facts["invoices"][0][0]
        invoice.setdefault("createdFrom", {"id": "16000000"})
        return facts, {
            "case_id": str(case_id),
            "report": {"order_reference": facts["source"]["number"], "balance": {}},
            "review": review,
            "order": {"id": "16000000", "tranId": facts["source"]["number"]},
            "catalog": {"properties": {}},  # as test_credit_creation's reader returns it
            "external_id": "ss-credit-x",
            "refund_graph": {"amount": "450.00", "record_ids": ["1"], "dependency_manifest": {}},
        }

    monkeypatch.setattr("app.services.transaction_ops.case_service.get_case", case)
    monkeypatch.setattr("app.services.transaction_ops.accounting_review.accounting_context", context)
    monkeypatch.setattr(cc, "verified_exemplar", exemplar)
    monkeypatch.setattr(cc, "gather", gather)
    # As test_credit_creation's card test: the live creditMemo catalog is checked at runtime.
    from app.services.transaction_ops import credit_api_correction

    monkeypatch.setattr(credit_api_correction, "schema_contract", lambda raw, fields: {"digest": "x"})
    monkeypatch.setattr(credit_api_correction, "typed_fields", lambda raw, fields: fields)
    return state


class TestPrepareGroupMember:
    async def test_a_member_gets_the_server_derived_zero_total_credit(self, member):
        db = SimpleNamespace(info={})
        p = await cc.prepare_group_member(db, TENANT, CASE)
        assert db.info["accounting_correction_candidate"] is p
        assert p["kind"] == "credit_creation" and p["settlement"] == "none"
        assert p["lines"] == [{"item_id": "4699", "amount": "71.85"}, {"item_id": "1603", "amount": "-71.85"}]
        assert p["exemplar_confirmation_id"] == EXEMPLAR_ID and member["exemplar_config"] == CONFIG
        # The exemplar's items are read with the readback's budget held back, as in chat.
        assert member["gathered"] == [(["1603", "4699"], cc.READBACK_RESERVE)]
        assert p["memo"].startswith("R166593377 ")
        assert "derived" in p["reason"] and EXEMPLAR_ID in p["reason"]
        # The card says who chose the numbers: the server, not the assistant.
        assert "chosen by the assistant" not in p["approval_basis"]
        assert EXEMPLAR_ID in p["approval_basis"]
        assert p["resolution_plan"]

    async def test_no_member_before_one_verified_zero_total_correction(self, member):
        member["exemplar"] = None
        db = SimpleNamespace(info={})
        with pytest.raises(cc.RefusalError, match="no_verified_exemplar"):
            await cc.prepare_group_member(db, TENANT, CASE)
        assert "accounting_correction_candidate" not in db.info and member["gathered"] == []

    async def test_a_refused_outcome_leaves_no_candidate(self, member):
        member["facts"]["source"]["total"] = "3600.00"
        db = SimpleNamespace(info={})
        with pytest.raises(cc.RefusalError):
            await cc.prepare_group_member(db, TENANT, CASE)
        assert "accounting_correction_candidate" not in db.info


def test_the_exemplar_is_a_verified_zero_total_credit_on_the_same_configuration():
    """The query, compiled: approved, independently verified, this kind, this configuration and
    settled as a zero-total credit (a credit applied to an invoice is not this treatment)."""
    from sqlalchemy.dialects import postgresql

    captured = {}

    class _Db:
        async def execute(self, statement):
            captured["sql"] = str(
                statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
            )
            return SimpleNamespace(first=lambda: None)

    assert asyncio.run(cc.verified_exemplar(_Db(), TENANT, CONFIG)) is None
    sql = " ".join(captured["sql"].split())
    # PR #421 review round 1 (F3): the predicates and their values, not just the key names.
    tenant = str(TENANT).replace("-", "")
    assert f"chat_messages.tenant_id = '{tenant}'" in sql or f"chat_messages.tenant_id = '{TENANT}'" in sql
    for path, value in (
        ("'status'", "'approved'"),
        ("'kind'", "'credit_creation'"),
        ("'config_id'", f"'{CONFIG}'"),
        ("'settlement'", "'none'"),
        ("'status'", "'verified'"),
    ):
        assert f"->> {path}) = {value}" in sql, (path, value, sql)
    assert "'accounting_verification'" in sql
    assert "ORDER BY chat_messages.created_at DESC, chat_messages.id DESC LIMIT 1" in sql


class TestGroupFallback:
    """The group step: a member whose credit sits in a locked period gets the zero-total credit."""

    @staticmethod
    async def _prepare(monkeypatch, *, reallocation_code, zero_total, queued=None, seen=None):
        from app.services.chat.write_confirmation_service import WriteConfirmationPayload
        from app.services.transaction_ops import accounting_group as group
        from app.services.transaction_ops import credit_line_reallocation as reallocation
        from tests.test_accounting_group import group_fixture

        so, session = group_fixture(1)
        card = so["accounting_group"]["members"][0]["card"]
        member = so["accounting_group"]["members"][0]
        db = AsyncMock()
        db.add = MagicMock()
        db.info = {
            "accounting_group_selection": {
                "group_id": "g",
                "scope": {},
                "members": [{"case_id": member["case_id"], "order_reference": member["order_reference"]}],
            }
        }
        calls = []

        @asynccontextmanager
        async def factory():
            child = AsyncMock()
            child.add = MagicMock()
            child.info = {}
            yield child

        async def evidence(*args, **kwargs):
            if queued is not None:  # as the evidence tool does on the order's own session
                from app.services.transaction_ops.fix_blockers import queue_card

                queue_card(kwargs["context"]["db"], queued)
            return {"success": True, "accounting_evidence": {}}

        async def reallocate(child_db, tenant_id, case_id):
            calls.append("reallocation")
            raise reallocation.RefusalError(reallocation_code)

        async def zero(child_db, tenant_id, case_id):
            calls.append("zero_total")
            if isinstance(zero_total, Exception):
                raise zero_total
            child_db.info["accounting_correction_candidate"] = {"case_id": case_id}

        async def candidate(**kwargs):
            if not kwargs["db"].info.get("accounting_correction_candidate"):
                return None
            return WriteConfirmationPayload(**card), "test"

        async def blocker(db, tenant_id, p):
            return None

        monkeypatch.setattr(group, "async_session_factory", factory)
        monkeypatch.setattr(group, "set_tenant_context", AsyncMock())
        monkeypatch.setattr(group, "log_event", AsyncMock())
        monkeypatch.setattr("app.mcp.tools.transaction_ops_tools.execute_accounting_evidence", evidence)
        monkeypatch.setattr("app.services.transaction_ops.tax_correction.candidate_confirmation", candidate)
        monkeypatch.setattr("app.services.transaction_ops.group_investigation.summarize", lambda e: {})
        monkeypatch.setattr("app.services.transaction_ops.chat_confirmation.attempt_blocker", blocker)
        monkeypatch.setattr(reallocation, "prepare_group_member", reallocate)
        monkeypatch.setattr(cc, "prepare_group_member", zero)
        snapshots = []
        result = await asyncio.wait_for(
            group.prepare_group_confirmation(
                db=db,
                tenant_id=session.tenant_id,
                actor_id=session.user_id,
                session_id=str(session.id),
                correlation_id="t",
                tools=[],
                policy=None,
                progress=snapshots.append,
            ),
            5,
        )
        skipped = [
            c.kwargs
            for c in group.log_event.await_args_list
            if c.kwargs.get("action") == "accounting_group.case.skipped"
        ]
        # What the progress line shows for members set aside: label -> count.
        calls.append({a["label"]: a["count"] for a in snapshots[-1]["set_aside"]})
        if seen is not None:
            from app.services.transaction_ops.fix_blockers import take_cards

            seen.extend(take_cards(db))
        return result, skipped, calls

    async def test_a_locked_period_falls_back_to_the_zero_total_credit(self, monkeypatch):
        result, skipped, calls = await self._prepare(monkeypatch, reallocation_code="period_locked", zero_total=None)
        assert calls == ["reallocation", "zero_total", {}]
        card, _ = result
        assert card.accounting_group["members"][0].get("card") and not skipped

    async def test_other_reallocation_refusals_never_try_it(self, monkeypatch):
        _, skipped, calls = await self._prepare(monkeypatch, reallocation_code="no_verified_exemplar", zero_total=None)
        assert calls == ["reallocation", {"needs one approved fix of this kind first": 1}]
        assert skipped

    async def test_a_refused_zero_total_credit_sets_the_member_aside_with_its_own_reason(self, monkeypatch):
        refusal = cc.RefusalError("foreign_currency_unsupported")
        result, skipped, calls = await self._prepare(monkeypatch, reallocation_code="period_locked", zero_total=refusal)
        assert calls == ["reallocation", "zero_total", {"foreign currency not supported yet": 1}]
        reason = skipped[0]["payload"]["reason"]
        assert "zero-total correction in the open period was not prepared: foreign_currency_unsupported" in reason
        assert "Existing-credit reallocation not prepared" not in reason
        assert result is None

    async def test_a_member_the_reclass_does_not_fit_is_still_held_by_its_locked_period(self, monkeypatch):
        refusal = cc.RefusalError("derive_unsupported_shape")
        _, skipped, calls = await self._prepare(monkeypatch, reallocation_code="period_locked", zero_total=refusal)
        assert "not prepared: derive_unsupported_shape" in skipped[0]["payload"]["reason"]
        assert calls[-1] == {"period is locked": 1}

    async def test_a_blocker_card_read_on_the_orders_session_reaches_the_chats_session(self, monkeypatch):
        # Review round 5 (F1): each order is read on its own session, closed before the agent could show its card.
        card = {
            "card_id": "fix-blocker:R123456789:source_not_final",
            "rows": [["11", "x", "order", "-1.00", None]],
            "columns": [{"key": "id"}],
        }
        seen = []
        await self._prepare(
            monkeypatch, reallocation_code="no_verified_exemplar", zero_total=None, queued=card, seen=seen
        )
        assert seen == [card]

    async def test_a_crashed_zero_total_attempt_keeps_the_member_and_its_evidence(self, monkeypatch):
        _, skipped, calls = await self._prepare(
            monkeypatch, reallocation_code="period_locked", zero_total=RuntimeError("provider timeout")
        )
        assert calls[-1] == {"no exact fix yet": 1}
        reason = skipped[0]["payload"]["reason"]
        assert "not prepared: error:RuntimeError" in reason
        assert "Preparation needs review" not in reason


def test_set_aside_labels_name_the_zero_total_refusals():
    from app.services.transaction_ops import accounting_group as group

    assert group.set_aside_label(code="foreign_currency_unsupported") == "foreign currency not supported yet"
    assert group.set_aside_label(code="derive_unsupported_shape") == "no exact fix yet"
