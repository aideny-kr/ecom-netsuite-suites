"""The agent-proposed credit reallocation through the existing approval path.

tool -> server outcome check -> bound approval card -> approval-time revalidation ->
readback -> subledger recheck, with the order's reads replaced by the verified fixtures.
"""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.mcp.tools import transaction_ops_tools as tools
from app.services.chat.write_payload import normalize_write_payload
from app.services.transaction_ops import accounting_recheck, tax_correction
from app.services.transaction_ops import credit_line_reallocation as reallocation
from app.services.transaction_ops.treatments import REGISTRY
from tests.test_credit_line_reallocation import BV_FIX, US_FIX, _bv, _gl, _us

TENANT = uuid4()
CONNECTOR = str(uuid4())
CASE = str(uuid4())


def _catalog():
    column = {
        "line": {"type": "integer"},
        "item": {"type": "object"},
        "quantity": {"type": "number"},
        "rate": {"type": "number"},
        "amount": {"type": "number"},
        "isTaxable": {"type": "boolean"},
        "taxCode": {"type": "object"},
    }
    return {
        "properties": {
            "item": {
                "properties": {
                    "items": {
                        "items": {
                            "properties": column,
                            "x-ns-sublistkey": {"value": {"new": ["orderLine"], "existing": ["line"]}},
                        }
                    }
                }
            }
        }
    }


def _context(facts):
    return {
        "case_id": CASE,
        "report": {"order_reference": facts["source"]["number"], "balance": {}},
        "review": {
            "scope": {
                "netsuite_account_id": "6738075",
                "subsidiary_id": facts["subsidiary_id"],
                "record_type": "salesorder",
            },
            "config_id": str(uuid4()),
            "netsuite_connection_id": str(uuid4()),
            "native_mcp_connector_id": CONNECTOR,
            "connection_active": True,
        },
        "order": {"id": facts["invoices"][0][0]["createdFrom"]["id"], "tranId": facts["source"]["number"]},
        "catalog": _catalog(),
        "refund_graph": {"amount": facts["credit"]["total"], "record_ids": ["1"], "dependency_manifest": {}},
    }


@pytest.fixture
def order(monkeypatch):
    """A switchable view of the order: tests mutate state["facts"] to simulate NetSuite changing."""
    state = {"facts": _us()}
    state["context"] = _context(state["facts"])

    async def gather(db, tenant_id, case_id, credit_memo_id):
        # The real gather always reads the currency's precision.
        return {"precision": 2, **deepcopy(state["facts"])}, deepcopy(state["context"])

    async def unrestricted(*args, **kwargs):
        return None

    monkeypatch.setattr(reallocation, "gather", gather)
    monkeypatch.setattr("app.services.transaction_ops.case_resolution_scope.validate", unrestricted)
    return state


def _db():
    return SimpleNamespace(info={})


def _tool_name():
    return f"ext__{CONNECTOR.replace('-', '')}__ns_updateRecord"


async def _proposed(order, lines=US_FIX):
    db = _db()
    p = await reallocation.propose(
        db, TENANT, CASE, order["facts"]["credit"]["id"], lines, "tax refund booked as return"
    )
    return db, p


def _written(facts, p):
    """The order as NetSuite would show it after the approved save."""
    after = deepcopy(facts)
    credit = after["credit"]
    credit["line_evidence"]["lines"] = [
        {
            "line": entry.get("line", 2),
            "item": {"id": entry["item"]["id"]},
            "quantity": "1.0",
            "rate": entry["rate"],
            "amount": entry["amount"],
        }
        for entry in p["proposed_fields"]["item"]["items"]
    ]
    rows = [(a, "debit", v) for a, v in p["expected_ledger"]["debit"].items()]
    rows += [(a, "credit", v) for a, v in p["expected_ledger"]["credit"].items()]
    after["credit_gl"] = _gl(*rows)
    return after


class TestProposeAndBind:
    async def test_accepted_proposal_becomes_the_bound_candidate(self, order):
        db, p = await _proposed(order)
        assert db.info["accounting_correction_candidate"] is p
        assert p["kind"] == reallocation.KIND and p["execution_transport"] == "mcp_record_api"
        assert json.loads(p["wire_record_json"]) == {
            "item": {
                "items": [
                    {"line": 1, "item": {"id": "5005"}, "quantity": 1, "rate": 2.8, "amount": 2.8, "isTaxable": False}
                ]
            }
        }
        assert p["resolution_plan"]["operation_key"]
        # The model's exact call binds; anything else does not.
        params = {"recordType": "creditMemo", "recordId": p["record_id"], "data": p["wire_record_json"]}
        card = tax_correction.review_for_card(db, TENANT, _tool_name(), "creditMemo", normalize_write_payload(params))
        assert card is p
        tampered = json.loads(p["wire_record_json"])
        tampered["item"]["items"][0]["amount"] = 2.7
        with pytest.raises(ValueError, match="credit_reallocation_binding_changed"):
            tax_correction.review_for_card(
                db,
                TENANT,
                _tool_name(),
                "creditMemo",
                normalize_write_payload({**params, "data": json.dumps(tampered)}),
            )

    async def test_card_expires(self, order):
        db, p = await _proposed(order)
        p["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        params = {"recordType": "creditMemo", "recordId": p["record_id"], "data": p["wire_record_json"]}
        with pytest.raises(ValueError, match="binding_changed"):
            tax_correction.review_for_card(db, TENANT, _tool_name(), "creditMemo", normalize_write_payload(params))

    async def test_bv_split_proposal(self, order):
        order["facts"] = _bv()
        order["context"] = _context(order["facts"])
        _, p = await _proposed(order, BV_FIX)
        items = json.loads(p["wire_record_json"])["item"]["items"]
        assert items[0]["line"] == 1 and items[0]["amount"] == 185.95 and "line" not in items[1]
        assert p["expected_ledger"] == {"debit": {"783": "185.95", "846": "39.05"}, "credit": {"119": "225.00"}}


class TestApproval:
    async def test_unchanged_order_revalidates(self, order):
        db, p = await _proposed(order)
        params = {"recordType": "creditMemo", "recordId": p["record_id"], "data": p["wire_record_json"]}
        await tax_correction.validate_approved(db, TENANT, _tool_name(), params, p)

    @pytest.mark.parametrize(
        "change,match",
        [
            (lambda f: f["source"].update(total="798.0", payment_total="798.0"), "source_changed"),
            (lambda f: f["period"].update(arLocked=True), "related_record_changed|refused:period_locked"),
            (lambda f: f["items"]["5005"].update(isInactive=True), "related_record_changed"),
            (lambda f: f["credit"]["application_evidence"]["lines"][0].update(amount="1.0"), "related_record_changed"),
        ],
    )
    async def test_changed_order_refuses_before_any_write(self, order, change, match):
        db, p = await _proposed(order)
        change(order["facts"])
        params = {"recordType": "creditMemo", "recordId": p["record_id"], "data": p["wire_record_json"]}
        with pytest.raises(ValueError, match=match):
            await tax_correction.validate_approved(db, TENANT, _tool_name(), params, p)


class TestReadback:
    async def test_verified_when_saved_lines_gl_and_balance_agree(self, order):
        _, p = await _proposed(order)
        order["facts"] = _written(order["facts"], p)
        result = await tax_correction.verify_after(_db(), TENANT, p, receipt={"id": p["record_id"]})
        assert result["status"] == "verified", result
        assert result["balance"]["booked"] == result["balance"]["source"]

    async def test_bv_readback_matches_new_line_by_content(self, order):
        order["facts"] = _bv()
        order["context"] = _context(order["facts"])
        _, p = await _proposed(order, BV_FIX)
        order["facts"] = _written(order["facts"], p)
        assert (await tax_correction.verify_after(_db(), TENANT, p))["status"] == "verified"

    async def test_needs_review_when_gl_differs(self, order):
        _, p = await _proposed(order)
        written = _written(order["facts"], p)
        written["credit_gl"] = _gl(("119", "credit", "2.8"), ("783", "debit", "2.8"))  # save ignored the item
        order["facts"] = written
        result = await tax_correction.verify_after(_db(), TENANT, p)
        assert result["status"] == "needs_review" and result["retry_allowed"] is False

    async def test_needs_review_on_conflicting_receipt(self, order):
        _, p = await _proposed(order)
        order["facts"] = _written(order["facts"], p)
        result = await tax_correction.verify_after(_db(), TENANT, p, receipt={"id": "999"})
        assert result["reason"] == "credit_reallocation_receipt_identity_conflict"


class TestRecheck:
    def test_registry_marks_both_existing_credit_amendments_for_the_subledger_recheck(self):
        assert REGISTRY[reallocation.KIND].subledger_recheck and REGISTRY["credit_tax_reallocation"].subledger_recheck
        assert not REGISTRY["invoice_tax"].subledger_recheck
        assert accounting_recheck.needs_subledger_recheck(
            {"kind": reallocation.KIND, "execution_transport": "mcp_record_api"}
        )
        assert not accounting_recheck.needs_subledger_recheck({"kind": reallocation.KIND})

    async def test_projection_uses_invoice_less_credits(self, order):
        from app.services.transaction_ops.accounting_credit_recheck import project

        _, p = await _proposed(order)
        written = _written(order["facts"], p)
        now = datetime.now(timezone.utc)
        report = {
            "order_reference": p["order_reference"],
            "source": {"observed_at": now.isoformat()},
            "balance": {
                "status": "difference",
                "amounts": {
                    "order_total": {"source": "799.0", "target": "801.8", "delta": "-2.8"},
                    "tax": {"source": "0.0", "target": "2.8", "delta": "-2.8"},
                },
            },
        }
        projected = project(
            p, report, ({"precision": 2, **written}, order["context"]), verified_at=now - timedelta(minutes=1), now=now
        )
        assert projected["balance"]["status"] == "matched"
        assert projected["balance"]["original_order_comparison"]["status"] == "difference"


class TestTool:
    @pytest.fixture
    def authorized(self, monkeypatch):
        db = _db()

        async def authorize(context, *, create, fresh=False):
            return db, TENANT, SimpleNamespace(id=uuid4())

        async def no_scope(*args, **kwargs):
            return None

        async def log(*args, **kwargs):
            log.calls.append(kwargs)
            return SimpleNamespace(id=uuid4())

        log.calls = []
        monkeypatch.setattr(tools, "_authorize", authorize)
        monkeypatch.setattr("app.services.transaction_ops.case_resolution_scope.load", no_scope)
        monkeypatch.setattr("app.services.audit_service.log_event", log)
        return db, log

    async def test_success_returns_the_exact_call_for_the_card(self, order, authorized):
        db, log = authorized
        out = await tools.execute_propose_credit_reallocation(
            {"case_id": CASE, "credit_memo_id": "15788939", "lines": US_FIX, "reason": "tax-only refund"},
            context={"db": db},
        )
        assert out["success"] and out["financial_writes"] == 0
        call = out["correction_candidate"]
        assert call["tool_name"] == _tool_name()
        assert call["params"]["data"] == db.info["accounting_correction_candidate"]["wire_record_json"]
        assert log.calls[-1]["action"] == "accounting.reallocation.proposed"

    async def test_refusal_is_specific_and_leaves_no_candidate(self, order, authorized):
        db, log = authorized
        lines = [{"line": 1, "item_id": "1603", "amount": "186.00"}, {"item_id": "4699", "amount": "39.00"}]
        order["facts"] = _bv()
        order["context"] = _context(order["facts"])
        out = await tools.execute_propose_credit_reallocation(
            {"case_id": CASE, "credit_memo_id": "15777793", "lines": lines}, context={"db": db}
        )
        assert out["success"] is False and out["refused"] == "outcome_does_not_match_source"
        assert out["detail"]["required"]["tax"] == "450.20" and "Never round" in out["guidance"]
        assert "accounting_correction_candidate" not in db.info
        assert log.calls[-1]["action"] == "accounting.reallocation.refused"

    async def test_unknown_parameters_are_rejected(self, order, authorized):
        db, _ = authorized
        out = await tools.execute_propose_credit_reallocation(
            {"case_id": CASE, "credit_memo_id": "1", "lines": [], "execute": True}, context={"db": db}
        )
        assert out["success"] is False and out["reason"] == "invalid_parameters"

    def test_registry_governance_and_chat_allowlist_agree(self):
        from app.mcp.governance import TOOL_CONFIGS
        from app.mcp.registry import TOOL_REGISTRY
        from app.services.chat.nodes import ALLOWED_CHAT_TOOLS
        from app.services.chat.tools import build_local_tool_definitions

        name = "transaction_ops.propose_credit_reallocation"
        assert set(TOOL_CONFIGS[name]["allowlisted_params"]) == set(TOOL_REGISTRY[name]["params_schema"])
        assert name in ALLOWED_CHAT_TOOLS
        definition = next(t for t in build_local_tool_definitions() if t["name"] == name.replace(".", "_"))
        assert definition["input_schema"]["properties"]["lines"]["items"]["required"] == ["item_id", "amount"]
        assert set(definition["input_schema"]["required"]) == {"case_id", "credit_memo_id", "lines"}


class TestGroup:
    async def test_member_is_derived_by_the_server_after_a_verified_exemplar(self, order, monkeypatch):
        async def exemplar(db, tenant_id, config_id):
            return "11111111-1111-1111-1111-111111111111"

        monkeypatch.setattr(reallocation, "verified_exemplar", exemplar)
        db = _db()
        p = await reallocation.prepare_group_member(db, TENANT, CASE)
        assert db.info["accounting_correction_candidate"] is p
        assert p["lines"] == US_FIX and p["exemplar_confirmation_id"].startswith("1111")
        assert "derived from this order's own source" in p["reason"]

    async def test_no_group_member_before_one_verified_correction(self, order, monkeypatch):
        async def none(db, tenant_id, config_id):
            return None

        monkeypatch.setattr(reallocation, "verified_exemplar", none)
        db = _db()
        with pytest.raises(reallocation.RefusalError, match="no_verified_exemplar"):
            await reallocation.prepare_group_member(db, TENANT, CASE)
        assert "accounting_correction_candidate" not in db.info

    @pytest.mark.parametrize("outcome", ["prepared", "refused", "crashed"])
    async def test_group_preparation_uses_the_reallocation_when_no_recipe_fits(self, monkeypatch, outcome):
        import asyncio
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from app.services.chat.write_confirmation_service import WriteConfirmationPayload
        from app.services.transaction_ops import accounting_group as group
        from tests.test_accounting_group import group_fixture

        so, session = group_fixture(1)
        member = so["accounting_group"]["members"][0]
        selection = {
            "group_id": "g",
            "scope": {},
            "members": [{"case_id": member["case_id"], "order_reference": member["order_reference"]}],
        }
        db = AsyncMock()
        db.add = MagicMock()
        db.info = {"accounting_group_selection": selection}

        @asynccontextmanager
        async def factory():
            child = AsyncMock()
            child.add = MagicMock()
            child.info = {}
            yield child

        async def evidence(*args, **kwargs):
            return {"success": True, "accounting_evidence": {}}

        async def derive(child_db, tenant_id, case_id):
            if outcome == "refused":
                raise reallocation.RefusalError("no_verified_exemplar")
            if outcome == "crashed":
                raise RuntimeError("provider timeout")
            child_db.info["accounting_correction_candidate"] = {"case_id": case_id}

        async def candidate(**kwargs):
            if not kwargs["db"].info.get("accounting_correction_candidate"):
                return None
            return WriteConfirmationPayload(**member["card"]), "test"

        monkeypatch.setattr(group, "async_session_factory", factory)
        monkeypatch.setattr(group, "set_tenant_context", AsyncMock())
        monkeypatch.setattr(group, "log_event", AsyncMock())
        monkeypatch.setattr("app.mcp.tools.transaction_ops_tools.execute_accounting_evidence", evidence)
        monkeypatch.setattr("app.services.transaction_ops.tax_correction.candidate_confirmation", candidate)
        monkeypatch.setattr(reallocation, "prepare_group_member", derive)
        monkeypatch.setattr("app.services.transaction_ops.group_investigation.summarize", lambda e: {})
        result = await asyncio.wait_for(
            group.prepare_group_confirmation(
                db=db,
                tenant_id=session.tenant_id,
                actor_id=session.user_id,
                session_id=str(session.id),
                correlation_id="t",
                tools=[],
                policy=None,
            ),
            5,
        )
        skipped = [
            c.kwargs
            for c in group.log_event.await_args_list
            if c.kwargs.get("action") == "accounting_group.case.skipped"
        ]
        if outcome == "prepared":
            card, _ = result
            assert card.accounting_group["members"][0].get("card") and not skipped
        else:
            reason = skipped[0]["payload"]["reason"]
            expected = (
                "needs one approved and verified correction of this kind"
                if outcome == "refused"
                else "Existing-credit reallocation not prepared: error:RuntimeError"
            )
            assert expected in reason
            assert "Preparation needs review" not in reason


class TestReviewRoundOneFlow:
    """Identity scope findings of the 2026-09-25 T2 gate round 1 (wf_caf4e844-a12)."""

    @staticmethod
    def _two_line_bv(order):
        facts = _bv()
        credit = facts["credit"]
        first = credit["line_evidence"]["lines"][0]
        first.update(amount="200.0", rate="200.0")
        second = {**deepcopy(first), "line": 2, "amount": "25.0", "rate": "25.0", "lineUniqueKey": "65500002"}
        credit["line_evidence"]["lines"].append(second)
        order["facts"] = facts
        order["context"] = _context(facts)
        return [{"line": 1, "item_id": "1603", "amount": "185.95"}, {"line": 2, "item_id": "4699", "amount": "39.05"}]

    async def test_an_edit_to_the_credits_lines_before_approval_refuses(self, order):
        lines = self._two_line_bv(order)
        db, p = await _proposed(order, lines)
        edited = order["facts"]["credit"]["line_evidence"]["lines"]
        edited[0].update(amount="100.0", rate="100.0")
        edited[1].update(amount="125.0", rate="125.0")  # same total, same accounts: aggregates unchanged
        params = {"recordType": "creditMemo", "recordId": p["record_id"], "data": p["wire_record_json"]}
        with pytest.raises(ValueError, match="related_record_changed"):
            await tax_correction.validate_approved(db, TENANT, _tool_name(), params, p)

    async def test_recheck_accepts_the_credit_after_its_item_was_replaced(self, order):
        from app.services.transaction_ops.accounting_credit_recheck import project

        _, p = await _proposed(order)
        written = _written(order["facts"], p)
        # A fresh gather reads only the items the credit now uses plus the configured tax items.
        written["items"] = {"5005": written["items"]["5005"]}
        now = datetime.now(timezone.utc)
        report = {
            "order_reference": p["order_reference"],
            "source": {"observed_at": now.isoformat()},
            "balance": {
                "status": "difference",
                "amounts": {
                    "order_total": {"source": "799.0", "target": "801.8", "delta": "-2.8"},
                    "tax": {"source": "0.0", "target": "2.8", "delta": "-2.8"},
                },
            },
        }
        projected = project(p, report, ({"precision": 2, **written}, order["context"]), verified_at=now, now=now)
        assert projected["balance"]["status"] == "matched"
        order["facts"] = written
        assert (await tax_correction.verify_after(_db(), TENANT, p))["status"] == "verified"

    async def test_readback_refuses_a_credit_moved_to_another_period(self, order):
        _, p = await _proposed(order)
        written = _written(order["facts"], p)
        written["credit"]["postingPeriod"] = {"id": "172"}
        written["period"] = {"id": "172", "closed": False, "arLocked": False, "allLocked": False}
        order["facts"] = written
        result = await tax_correction.verify_after(_db(), TENANT, p)
        assert result["status"] == "needs_review"

    async def test_readback_matches_renumbered_lines_by_content(self, order):
        order["facts"] = _bv()
        order["context"] = _context(order["facts"])
        _, p = await _proposed(order, BV_FIX)
        written = _written(order["facts"], p)
        lines = written["credit"]["line_evidence"]["lines"]
        lines[0]["line"], lines[1]["line"] = 2, 1  # NetSuite renumbered on save
        order["facts"] = written
        assert (await tax_correction.verify_after(_db(), TENANT, p))["status"] == "verified"

    async def test_success_result_hands_the_model_no_computed_figures(self, order, monkeypatch):
        db = _db()

        async def authorize(context, *, create, fresh=False):
            return db, TENANT, SimpleNamespace(id=uuid4())

        async def none(*args, **kwargs):
            return None

        async def log(*args, **kwargs):
            return SimpleNamespace(id=uuid4())

        monkeypatch.setattr(tools, "_authorize", authorize)
        monkeypatch.setattr("app.services.transaction_ops.case_resolution_scope.load", none)
        monkeypatch.setattr("app.services.audit_service.log_event", log)
        out = await tools.execute_propose_credit_reallocation(
            {"case_id": CASE, "credit_memo_id": "15788939", "lines": US_FIX}, context={"db": db}
        )
        assert out["success"]
        assert not {"verified_outcome", "expected_ledger", "approval_basis"} & set(out)
        assert (
            out["correction_candidate"]["params"]["data"]
            == db.info["accounting_correction_candidate"]["wire_record_json"]
        )


class TestReviewRoundTwoFlow:
    """Findings of the 2026-09-25 T2 gate round 2 (wf_67359078-97c)."""

    @pytest.mark.parametrize("key", ["native_mcp_connector_id", "netsuite_connection_id"])
    async def test_approval_refuses_when_the_connection_binding_changed(self, order, key):
        db, p = await _proposed(order)
        order["context"]["review"][key] = str(uuid4())
        params = {"recordType": "creditMemo", "recordId": p["record_id"], "data": p["wire_record_json"]}
        with pytest.raises(ValueError, match="connection_scope_changed"):
            await tax_correction.validate_approved(db, TENANT, _tool_name(), params, p)

    async def test_readback_refuses_a_saved_line_with_another_quantity(self, order):
        _, p = await _proposed(order)
        written = _written(order["facts"], p)
        written["credit"]["line_evidence"]["lines"][0]["quantity"] = "2.0"
        order["facts"] = written
        assert (await tax_correction.verify_after(_db(), TENANT, p))["status"] == "needs_review"

    async def test_group_explains_the_first_correction_rule(self, order, monkeypatch):
        from app.services.transaction_ops import accounting_group as group

        assert "verified" in group.reallocation_reason("no_verified_exemplar").lower()
        assert group.reallocation_reason("no_difference") is None
