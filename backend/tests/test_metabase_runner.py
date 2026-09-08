"""Replica scans feed the existing runner; repair evidence still comes from the API."""

from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.transaction_ops import metabase_reader, runner
from app.services.transaction_ops.normalization import TransactionMapping
from tests.test_metabase_replica_reader import BINDING
from tests.test_transaction_ops_runner import NOW, REF, State, missing_target, source_order


def configured():
    state = State(window=True)
    state.run.config_snapshot["source_connection_id"] = str(uuid4())
    state.run.config_snapshot["source_step_id"] = None
    state.run.config_snapshot["mapping_json"]["metabase_replica"] = BINDING
    state.run.config_snapshot["mapping_json"]["solidus_refund_step_id"] = str(uuid4())
    return state


def test_mapping_accepts_explicit_pinned_replica_contract():
    mapping = TransactionMapping.model_validate(configured().run.config_snapshot["mapping_json"])
    assert mapping.metabase_replica.database_id == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("refund_only", [False, True])
async def test_replica_order_or_old_refund_scan_uses_full_api_evidence(monkeypatch, refund_only):
    state = configured()
    full = source_order()
    full["orders"][0]["completed_at"] = (NOW - timedelta(days=100)).isoformat()
    headers = [
        {
            k: v
            for k, v in full["orders"][0].items()
            if k in ("id", "number", "updated_at", "completed_at", "business_entity")
        }
    ]
    pages = AsyncMock(
        return_value={
            "orders": [] if refund_only else headers,
            "page_complete": True,
            "scan_complete": True,
            "next_after_id": None,
        }
    )
    refunds = AsyncMock(
        return_value={
            "orders": headers if refund_only else [],
            "refunds": [],
            "page_complete": True,
            "scan_complete": True,
            "next_after_id": None,
        }
    )
    monkeypatch.setattr(metabase_reader, "read_order_page", pages)
    monkeypatch.setattr(metabase_reader, "read_changed_refund_orders", refunds)
    canonical = AsyncMock(return_value=full)
    mirror = AsyncMock()
    legacy_page = AsyncMock(side_effect=AssertionError("Should use the bound replica scanner"))
    result = await runner.run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _clock=lambda: NOW,
        _enabled=AsyncMock(return_value=True),
        _page_reader=legacy_page,
        _source_reader=canonical,
        _target_reader=AsyncMock(return_value=missing_target()),
        _order_mirror=mirror,
        _source_refunds_reader=AsyncMock(
            return_value={"complete": True, "order_reference": REF, "currency": "USD", "amount": "0"}
        ),
    )
    assert result["termination_reason"] == "done"
    assert state.run.progress_json["processed"] == 1 and REF in state.reports
    legacy_page.assert_not_awaited()
    canonical.assert_awaited_once()
    assert mirror.call_args.args[3]["line_items"] == full["orders"][0]["line_items"]
    assert state.run.progress_json["scan_mode"] == "metabase"
    assert state.run.progress_json["refund_scan_complete"] is True


def test_replica_page_does_not_invent_total_and_rejects_bad_cursor():
    state = configured()
    progress = runner._initial_progress(state.run)
    order = source_order()["orders"][0]
    page = {"page_complete": True, "orders": [order], "scan_complete": False, "next_after_id": 1}
    runner._replica_page_progress(page, progress, state.run.params_json, state.run.config_snapshot)
    assert progress["expected_total"] is None and progress["scan_count"] == 1
    assert not progress["scan_complete"]
    with pytest.raises(runner.ScanChangedError):
        runner._replica_page_progress(page, progress, state.run.params_json, state.run.config_snapshot)


@pytest.mark.asyncio
async def test_configuration_rejects_another_tenants_replica(db, admin_user, admin_user_b):
    from app.models.mcp_connector import McpConnector
    from app.services.transaction_ops import state_service
    from tests.test_transaction_ops_state_db import seed_config

    owner = admin_user_b[0]
    actor = admin_user[0]
    connector = McpConnector(
        tenant_id=owner.tenant_id,
        provider="custom",
        label="Other tenant replica",
        server_url="https://analytics.example/api/metabase-mcp",
        auth_type="oauth2",
        status="active",
        is_enabled=True,
        encrypted_credentials="not-read",
    )
    db.add(connector)
    await db.flush()
    mapping = {"reference_field": "tranid", "metabase_replica": {**BINDING, "connector_id": str(connector.id)}}
    with pytest.raises(state_service.StateError, match="replica_unavailable"):
        await seed_config(db, actor.tenant_id, actor, mapping_json=mapping)


@pytest.mark.asyncio
async def test_config_snapshot_preserves_owned_replica_binding(db, admin_user):
    from app.models.mcp_connector import McpConnector
    from app.schemas.transaction_runs import RunCreate
    from app.services.transaction_ops import state_service
    from tests.test_transaction_ops_state_db import seed_config

    actor = admin_user[0]
    connector = McpConnector(
        tenant_id=actor.tenant_id,
        provider="custom",
        label="Replica",
        server_url="https://analytics.example/api/metabase-mcp",
        auth_type="oauth2",
        status="active",
        is_enabled=True,
        encrypted_credentials="not-read",
    )
    db.add(connector)
    await db.flush()
    mapping = {"reference_field": "tranid", "metabase_replica": {**BINDING, "connector_id": str(connector.id)}}
    config = await seed_config(db, actor.tenant_id, actor, mapping_json=mapping)
    run = await state_service.create_run(
        db, actor.tenant_id, config.id, RunCreate(evaluation_key="replica", order_references=[REF]), actor=actor
    )
    assert run.config_snapshot["mapping_json"]["metabase_replica"] == mapping["metabase_replica"]


@pytest.mark.asyncio
async def test_destination_only_change_rechecks_an_old_source_order(monkeypatch):
    state = configured()
    state.run.config_snapshot["mapping_json"]["reconciliation_policy"] = {}
    empty = {"orders": [], "page_complete": True, "scan_complete": True, "next_after_id": None}
    monkeypatch.setattr(metabase_reader, "read_order_page", AsyncMock(return_value=empty))
    monkeypatch.setattr(metabase_reader, "read_changed_refund_orders", AsyncMock(return_value=empty))
    native = AsyncMock(
        return_value={
            **empty,
            "orders": [{"id": 500, "number": REF, "updated_at": (NOW - timedelta(minutes=30)).isoformat()}],
        }
    )
    old_source = source_order()
    old_source["orders"][0]["updated_at"] = (NOW - timedelta(days=90)).isoformat()
    canonical = AsyncMock(return_value=old_source)
    result = await runner.run_investigation(
        None,
        state.tenant,
        state.run_id,
        _state=state,
        _clock=lambda: NOW,
        _enabled=AsyncMock(return_value=True),
        _source_reader=canonical,
        _target_reader=AsyncMock(return_value=missing_target()),
        _source_refunds_reader=AsyncMock(
            return_value={"complete": True, "order_reference": REF, "currency": "USD", "amount": "0"}
        ),
        _order_mirror=AsyncMock(),
        _destination_page_reader=native,
    )
    assert result["termination_reason"] == "done" and result["processed"] == 1
    assert state.run.progress_json["destination_scan_complete"] is True
    assert state.run.progress_json["destination_scan_count"] == 1
    canonical.assert_awaited_once()
    native.assert_awaited_once()
