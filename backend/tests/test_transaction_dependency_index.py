"""Persist observed dependencies without granting freshness or write authority."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import set_tenant_context
from app.models.transaction_netsuite_dependency import TransactionNetSuiteDependency as Dependency
from app.models.transaction_ops import TransactionFinding
from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import dependency_index as service
from app.services.transaction_ops import state_service as state
from tests.test_transaction_ops_state import config_input
from tests.test_transaction_ops_state_db import setup_state as _setup_state_fixture

setup_state = _setup_state_fixture


def report(run, reference="R123456789", root_id="1"):
    c = run.config_snapshot
    account = c["netsuite_account_id"].replace("_", "-").lower()
    return {
        "order_reference": reference,
        "targets": [
            {
                "system": "netsuite",
                "account_id": account,
                "record_type": "salesorder",
                "record_id": root_id,
                "subsidiary_id": c["subsidiary_id"],
                "order_reference": reference,
            }
        ],
        "refund_evidence": {
            "target": {
                "complete": True,
                "provider": "netsuite",
                "account_id": account,
                "connection_id": c["netsuite_connection_id"],
                "subsidiary_id": c["subsidiary_id"],
                "order_reference": reference,
                "dependency_manifest": {
                    "version": 1,
                    "order_id": root_id,
                    "transaction_ids": [root_id, "2", "3", "4", "999"],
                    "refund_requests": ["20"],
                    "truncated": False,
                },
            }
        },
    }


async def save(db, actor, run, value):
    token = run.lease_token or await state.claim_run(db, actor.tenant_id, run.id)
    return await state.record_finding(db, actor.tenant_id, run.id, value["order_reference"], value, lease_token=token)


async def test_finding_indexes_dependencies_idempotently_and_cascades_with_evidence(db, setup_state):
    actor, config, run = setup_state
    value = report(run)
    finding = await save(db, actor, run, value)
    await save(db, actor, run, value)
    assert await db.scalar(select(func.count()).select_from(Dependency)) == 6
    for key in (("transaction", "999"), ("customrecord_fw_refund_requests", "20")):
        result = await service.affected_order_references(db, actor.tenant_id, config.id, [key])
        assert result == {"order_references": ["R123456789"], "has_more": False, "next_after_reference": None}
    # Equal numeric IDs from different NetSuite tables cannot alias each other.
    assert not (await service.affected_order_references(db, actor.tenant_id, config.id, [("transaction", "20")]))[
        "order_references"
    ]
    await db.execute(delete(TransactionFinding).where(TransactionFinding.id == finding.id))
    assert await db.scalar(select(func.count()).select_from(Dependency)) == 0


async def test_change_lookup_deduplicates_and_pages_shared_dependencies(db, setup_state):
    actor, config, run = setup_state
    await save(db, actor, run, report(run, "R123456788", "10"))
    await save(db, actor, run, report(run))
    keys = [("transaction", "4"), ("transaction", "999")]
    first = await service.affected_order_references(db, actor.tenant_id, config.id, keys, page_size=1)
    assert first == {"order_references": ["R123456788"], "has_more": True, "next_after_reference": "R123456788"}
    last = await service.affected_order_references(
        db, actor.tenant_id, config.id, keys, page_size=1, after_reference=first["next_after_reference"]
    )
    assert last == {"order_references": ["R123456789"], "has_more": False, "next_after_reference": None}
    with pytest.raises(state.StateError, match="not_found"):
        await service.affected_order_references(db, uuid4(), config.id, keys)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "9999999"),
        ("connection_id", str(uuid4())),
        ("subsidiary_id", "999"),
        ("order_reference", "R999999999"),
        ("complete", False),
    ],
)
async def test_wrong_refund_scope_cannot_index_its_linked_records(setup_state, field, value):
    _, _, run = setup_state
    value_report = report(run)
    value_report["refund_evidence"]["target"][field] = value
    assert service.observed_dependencies(value_report, run.config_snapshot, "R123456789") == {("transaction", "1")}


async def test_missing_or_truncated_inventory_never_claims_completeness(setup_state):
    _, _, run = setup_state
    value = report(run)
    value["refund_evidence"]["target"].pop("dependency_manifest")
    assert service.observed_dependencies(value, run.config_snapshot, "R123456789") == {("transaction", "1")}
    value = report(run)
    value["refund_evidence"]["target"]["dependency_manifest"]["truncated"] = True
    # Known positive dependencies remain useful even if the inventory is partial.
    before = deepcopy(value)
    assert ("transaction", "999") in service.observed_dependencies(value, run.config_snapshot, "R123456789")
    assert value == before
    assert "complete" not in value["refund_evidence"]["target"]["dependency_manifest"]


async def test_old_dependencies_survive_a_later_partial_checkpoint(db, setup_state):
    actor, config, run = setup_state
    value = report(run)
    await save(db, actor, run, value)
    value["refund_evidence"]["target"] = {"complete": False}
    await save(db, actor, run, value)
    assert (await service.affected_order_references(db, actor.tenant_id, config.id, [("transaction", "4")]))[
        "order_references"
    ] == ["R123456789"]


async def test_same_connection_and_record_do_not_leak_between_entity_configs(db, setup_state):
    actor, config, run = setup_state
    other = await state.create_config(
        db,
        actor.tenant_id,
        config_input(
            source_step_id=config.source_step_id,
            netsuite_connection_id=config.netsuite_connection_id,
            subsidiary_id="2",
        ),
        actor=actor,
    )
    other_run = await state.create_run(
        db,
        actor.tenant_id,
        other.id,
        RunCreate(evaluation_key="other-entity", order_references=["R987654321"]),
        actor=actor,
    )
    await save(db, actor, run, report(run))
    await save(db, actor, other_run, report(other_run, "R987654321"))
    for scoped_config, expected in ((config, "R123456789"), (other, "R987654321")):
        result = await service.affected_order_references(
            db, actor.tenant_id, scoped_config.id, [("transaction", "999")]
        )
        assert result["order_references"] == [expected]


@pytest.mark.parametrize(
    "keys",
    [
        [],
        [("transaction", True)],
        [("transaction", "1 OR 1=1")],
        [("CustRfnd", "4")],
        [("transaction", "1", "2")],
        [("transaction", "1")] * 501,
    ],
)
async def test_change_lookup_rejects_unbounded_or_invalid_keys(db, setup_state, keys):
    actor, config, _ = setup_state
    with pytest.raises(ValueError, match="invalid_dependency"):
        await service.affected_order_references(db, actor.tenant_id, config.id, keys)


async def test_dependency_foreign_key_rejects_cross_tenant_finding_attachment(db, setup_state):
    actor, _, run = setup_state
    await save(db, actor, run, report(run))
    with pytest.raises(DBAPIError, match="foreign key"):
        async with db.begin_nested():
            db.add(
                Dependency(
                    tenant_id=uuid4(),
                    run_id=run.id,
                    order_reference="R123456789",
                    connection_id=uuid4(),
                    account_id="6738075",
                    record_type="transaction",
                    record_id="1",
                )
            )
            await db.flush()


async def test_dependency_rls_uses_non_bypass_role(db, setup_state):
    actor, _, run = setup_state
    await save(db, actor, run, report(run))
    role = "dependency_test_" + uuid4().hex
    await db.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
    await db.execute(text(f"GRANT SELECT, INSERT ON transaction_netsuite_dependencies TO {role}"))
    try:
        await db.execute(text(f"SET LOCAL ROLE {role}"))
        await set_tenant_context(db, actor.tenant_id)
        assert await db.scalar(select(func.count()).select_from(Dependency)) == 6
        await set_tenant_context(db, uuid4())
        assert await db.scalar(select(func.count()).select_from(Dependency)) == 0
        with pytest.raises(DBAPIError, match="row-level security"):
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO transaction_netsuite_dependencies (id,tenant_id,run_id,order_reference,connection_id,account_id,record_type,record_id) VALUES (:id,:tenant,:run,'R123456789',:connection,'6738075','transaction','777')"
                    ),
                    {"id": uuid4(), "tenant": actor.tenant_id, "run": run.id, "connection": uuid4()},
                )
    finally:
        await db.execute(text("RESET ROLE"))
        await db.execute(text(f"DROP OWNED BY {role}"))
        await db.execute(text(f"DROP ROLE {role}"))
