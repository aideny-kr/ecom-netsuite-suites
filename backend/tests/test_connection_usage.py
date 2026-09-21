import uuid
from types import SimpleNamespace

from app.models.pipeline import Schedule
from app.models.transaction_ops import TransactionConfig


def test_scope_matches_exact_access_method_not_another_method_or_source():
    from app.services.connection_usage import config_uses

    source, target, replica = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    config = TransactionConfig(
        source_connection_id=source,
        netsuite_connection_id=target,
        mapping_json={"metabase_replica": {"connector_id": str(replica)}},
    )
    assert config_uses(config, "api", source)
    assert config_uses(config, "api", target)
    assert config_uses(config, "mcp", replica)
    assert not config_uses(config, "mcp", source)
    assert not config_uses(config, "api", uuid.uuid4())


def test_workflow_provider_requirement_is_explicitly_potential_not_exact():
    from app.services.connection_usage import schedule_use

    schedule = Schedule(schedule_type="job", plan_json={"steps": [{"type": "bigquery_sql", "params": {}}]})
    assert (
        schedule_use(schedule, "mcp", SimpleNamespace(id=uuid.uuid4(), provider="bigquery")) == "provider requirement"
    )
    assert schedule_use(schedule, "api", SimpleNamespace(id=uuid.uuid4(), provider="netsuite")) is None


def test_pending_plan_and_legacy_exact_binding_are_included():
    from app.services.connection_usage import schedule_use

    cid = uuid.uuid4()
    schedule = Schedule(schedule_type="sync", parameters={"connection_id": str(cid)})
    assert schedule_use(schedule, "api", SimpleNamespace(id=cid, provider="netsuite")) == "exact binding"
    assert schedule_use(schedule, "mcp", SimpleNamespace(id=cid, provider="netsuite_mcp")) is None
    schedule = Schedule(schedule_type="job", pending_plan_json={"steps": [{"type": "recon.run", "params": {}}]})
    assert schedule_use(schedule, "api", SimpleNamespace(id=cid, provider="netsuite")) == "provider requirement"
