"""Historical observed IDs are seeded before change-only discovery can finish."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.transaction_netsuite_dependency import TransactionNetSuiteDependency as Dependency
from app.models.transaction_ops import TransactionFinding
from app.services.transaction_ops import dependency_index, dependency_seed
from app.services.transaction_ops import state_service as state
from tests.test_transaction_dependency_index import report
from tests.test_transaction_ops_state_db import setup_state as _setup_state_fixture

setup_state = _setup_state_fixture


def legacy(run, reference="R123456789"):
    value = report(run, reference)
    target = value["refund_evidence"]["target"]
    target.pop("dependency_manifest")
    target.update(
        record_ids=["3"],
        request_links=[{"request_id": "20", "credit_memo_id": "4", "refund_id": "3", "deposit_id": "5"}],
    )
    return value


async def test_seed_pages_commits_and_repeats_idempotently_without_changing_findings(db, setup_state):
    actor, config, run = setup_state
    for index in range(101):
        reference = f"R{index:09d}"
        db.add(
            TransactionFinding(
                tenant_id=actor.tenant_id, run_id=run.id, order_reference=reference, report_json=legacy(run, reference)
            )
        )
    await db.flush()
    first = await dependency_seed.advance(db, actor.tenant_id, config.id, {})
    assert first["complete"] is False and first["observations_scanned"] == 100
    assert (
        await db.scalar(select(func.count()).select_from(Dependency).where(Dependency.tenant_id == actor.tenant_id))
        == 500
    )
    assert not await db.scalar(
        select(AuditEvent.id).where(
            AuditEvent.tenant_id == actor.tenant_id, AuditEvent.action == "transaction_ops.dependency_index.seeded"
        )
    )
    await db.commit()
    # Repeating the last page after a checkpoint crash cannot duplicate IDs.
    again = await dependency_seed.advance(db, actor.tenant_id, config.id, {})
    assert again == first
    final = await dependency_seed.advance(db, actor.tenant_id, config.id, first)
    assert final["complete"] and final["observations_scanned"] == 101
    await db.commit()
    assert (
        await db.scalar(select(func.count()).select_from(Dependency).where(Dependency.tenant_id == actor.tenant_id))
        == 505
    )
    found = await dependency_index.affected_order_references(
        db, actor.tenant_id, config.id, [("transaction", "3")], page_size=100
    )
    assert len(found["order_references"]) == 100 and found["has_more"]
    # A later cycle uses the durable scoped receipt, not another history scan.
    receipt = await dependency_seed.advance(db, actor.tenant_id, config.id, {})
    assert receipt == {"complete": True, "scope_digest": final["scope_digest"]}
    finding = await db.scalar(select(TransactionFinding).where(TransactionFinding.run_id == run.id).limit(1))
    assert "dependency_manifest" not in finding.report_json["refund_evidence"]["target"]


async def test_seed_receipt_and_lookup_cannot_cross_tenants_or_configurations(db, setup_state):
    actor, config, run = setup_state
    db.add(
        TransactionFinding(
            tenant_id=actor.tenant_id, run_id=run.id, order_reference="R123456789", report_json=legacy(run)
        )
    )
    await db.flush()
    with pytest.raises(state.StateError, match="not_found"):
        await dependency_seed.advance(db, uuid4(), config.id, {})
    with pytest.raises(state.StateError, match="dependency_seed_scope_changed"):
        await dependency_seed.advance(db, actor.tenant_id, config.id, {"scope_digest": "other-scope"})
    result = await dependency_seed.advance(db, actor.tenant_id, config.id, {})
    assert result["complete"]
    assert ("transaction", "3") in dependency_index.observed_dependencies(
        legacy(run), run.config_snapshot, "R123456789"
    )


@pytest.mark.parametrize(
    "change", ["account", "connection", "subsidiary", "reference", "incomplete", "malformed", "oversized"]
)
async def test_legacy_positive_ids_require_scoped_complete_evidence(setup_state, change):
    _, _, run = setup_state
    value = deepcopy(legacy(run))
    target = value["refund_evidence"]["target"]
    if change == "account":
        target["account_id"] = "99999"
    elif change == "connection":
        target["connection_id"] = str(uuid4())
    elif change == "subsidiary":
        target["subsidiary_id"] = "999"
    elif change == "reference":
        target["order_reference"] = "R111111111"
    elif change == "incomplete":
        target["complete"] = False
    elif change == "malformed":
        target["record_ids"] = ["not-an-id"]
    else:
        target["record_ids"] = ["3"] * 601
    assert dependency_index.observed_dependencies(value, run.config_snapshot, "R123456789") == {("transaction", "1")}
