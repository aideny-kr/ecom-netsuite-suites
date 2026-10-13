"""A config revision must not strand work already done under the config it replaced.

Staging, 2026-10-13: Codex revised all four Framework configs (rounding tolerance, BV tax
accounts). CM12127 (R231821517) was posted under the old Inc config and still awaited its
readback; recovery refused the now-disabled config, and on that refusal the card recovery
escalates the ledger row to a terminal needs_review. Recovery and the follow-up recheck verify
work that already exists, so they read under the config's enabled successor with the same
scope. Executing a NEW write stays strict: its permit check still refuses a disabled config.
"""

from uuid import uuid4

import pytest

from app.models.transaction_ops import TransactionConfig
from app.services.transaction_ops import state_service as state
from tests.test_transaction_ops_state_db import seed_config

_COPIED_OUT = {"id", "config_key", "supersedes_config_id", "created_at", "updated_at"}


async def _revise(db, previous, **changes):
    """What a config revision does: a successor with the same settings, the old row disabled."""
    values = {
        c.name: getattr(previous, c.name) for c in TransactionConfig.__table__.columns if c.name not in _COPIED_OUT
    }
    values.update(enabled=True, **changes)
    successor = TransactionConfig(**values, config_key=uuid4().hex, supersedes_config_id=previous.id)
    db.add(successor)
    previous.enabled, previous.schedule_enabled = False, False
    await db.flush()
    return successor


async def test_a_config_means_itself_while_it_is_enabled(db, admin_user):
    actor = admin_user[0]
    config = await seed_config(db, actor.tenant_id, actor)
    config.enabled = True
    await db.flush()
    assert (await state.current_config(db, actor.tenant_id, config.id)).id == config.id


async def test_a_superseded_config_means_its_enabled_successor_through_a_chain(db, admin_user):
    actor = admin_user[0]
    first = await seed_config(db, actor.tenant_id, actor)
    first.enabled = True
    second = await _revise(db, first)
    third = await _revise(db, second)
    assert (await state.current_config(db, actor.tenant_id, first.id)).id == third.id


@pytest.mark.parametrize("change", ["subsidiary", "no_successor", "successor_disabled"])
async def test_no_current_config_when_the_successor_changed_scope_or_none_is_enabled(db, admin_user, change):
    actor = admin_user[0]
    old = await seed_config(db, actor.tenant_id, actor)
    old.enabled = True
    if change == "subsidiary":
        await _revise(db, old, subsidiary_id="99")
    elif change == "successor_disabled":
        successor = await _revise(db, old)
        successor.enabled = False
        await db.flush()
    else:
        old.enabled = False
        await db.flush()
    assert await state.current_config(db, actor.tenant_id, old.id) is None


# --- the recovery and recheck paths ------------------------------------------------------------

import json  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.models.transaction_ops import TransactionRun  # noqa: E402
from app.services.transaction_ops import accounting_recovery  # noqa: E402
from app.services.transaction_ops import recovery as proposal_recovery  # noqa: E402
from tests import test_transaction_ops_executor as execution_fixtures  # noqa: E402
from tests.test_accounting_card_adapter import _run, build, claimed  # noqa: E402,F401
from tests.test_accounting_recheck import approved_credit  # noqa: E402,F401
from tests.test_accounting_recovery import interrupted_credit, providers  # noqa: E402,F401
from tests.test_chat_confirmation_source import authorized  # noqa: E402,F401
from tests.test_transaction_ops_executor import operation  # noqa: E402
from tests.test_transaction_ops_recovery import mock_recovery, unknown_case  # noqa: E402,F401

execution_case = execution_fixtures.execution_case


@pytest.mark.parametrize("interrupted_credit", ["credit"], indirect=True)
async def test_a_card_whose_config_was_revised_is_still_verified_by_recovery(
    db,
    interrupted_credit,  # noqa: F811
    claimed,  # noqa: F811
    providers,  # noqa: F811
):
    """CM12127's exact state: accepted with a receipt, readback unproven, then the config was
    revised. Recovery reads under the successor and verifies instead of escalating."""
    actor, config, _, message, _, _ = interrupted_credit
    adapter = build(
        message,
        dispatch=AsyncMock(return_value=json.dumps({"success": True, "recordId": 30, "total": 12.5})),
        readback=AsyncMock(return_value={"status": "needs_review", "reason": "KeyError"}),
    )
    result, _ = await _run(db, claimed, adapter)
    assert result["status"] == "committed_unverified"
    successor = await _revise(db, config)
    providers.return_value = {"status": "verified", "credit_memo_id": "30"}
    op = await state.operation_for_approval(db, actor.tenant_id, "chat_confirmation", message.id)
    out = await accounting_recovery.recover_card(
        db, actor.tenant_id, op, now=datetime.now(timezone.utc) + timedelta(minutes=10)
    )
    assert out == {"termination_reason": "done", "financial_writes": 0}
    op = await state.operation_for_approval(db, actor.tenant_id, "chat_confirmation", message.id)
    assert op.status == "verified"
    runs = (
        (
            await db.execute(
                select(TransactionRun).where(
                    TransactionRun.tenant_id == actor.tenant_id, TransactionRun.origin == "recovery"
                )
            )
        )
        .scalars()
        .all()
    )
    assert runs and {r.config_id for r in runs} == {successor.id}  # the readback and the case recheck
    assert adapter.dispatch.await_count == 1  # nothing sent again


async def test_a_proposal_write_whose_config_was_revised_is_still_reconciled(db, unknown_case, monkeypatch):  # noqa: F811
    row = await operation(db, unknown_case)
    tenant = unknown_case.actor.tenant_id
    proposal = await state.get_proposal(db, tenant, row.proposal_id)
    successor = await _revise(db, await state.get_config(db, tenant, proposal.config_id))
    mock_recovery(monkeypatch, unknown_case)
    result = await proposal_recovery.recover_operation(db, tenant, row.id)
    assert result["status"] == "verified"
    runs = (
        (
            await db.execute(
                select(TransactionRun).where(TransactionRun.tenant_id == tenant, TransactionRun.origin == "recovery")
            )
        )
        .scalars()
        .all()
    )
    assert runs and {r.config_id for r in runs} == {successor.id}  # the recovery read and the settlement check


async def test_executing_a_new_write_still_refuses_a_revised_config(db, admin_user):
    """Only verification of work already done follows a successor; a new write needs approval
    under the config that is current, so its permit check is unchanged."""
    import inspect

    source = inspect.getsource(state._proposal_still_authorized)
    assert "current_config" not in source and "if not config.enabled" in source
