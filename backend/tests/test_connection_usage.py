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


def test_drive_delivery_warns_for_google_sheets_access():
    from app.services.connection_usage import schedule_use

    schedule = Schedule(
        schedule_type="job", plan_json={"steps": [{"type": "drive.upload", "params": {"report_step": "report"}}]}
    )
    assert (
        schedule_use(schedule, "mcp", SimpleNamespace(id=uuid.uuid4(), provider="google_sheets"))
        == "provider requirement"
    )


def test_financial_report_does_not_claim_bigquery_is_its_source():
    from app.services.connection_usage import schedule_use

    schedule = Schedule(
        schedule_type="job",
        plan_json={
            "steps": [
                {
                    "type": "report.compose",
                    "params": {"playbook_key": "income_statement", "params": {"period": "Sep 2026"}},
                }
            ]
        },
    )
    assert schedule_use(schedule, "mcp", SimpleNamespace(id=uuid.uuid4(), provider="bigquery")) is None
    assert (
        schedule_use(schedule, "api", SimpleNamespace(id=uuid.uuid4(), provider="netsuite")) == "provider requirement"
    )


def test_inventory_report_uses_bigquery_and_report_id_is_not_guessed():
    from app.services.connection_usage import schedule_use

    schedule = Schedule(
        schedule_type="job",
        plan_json={"steps": [{"type": "report.compose", "params": {"playbook_key": "inventory_aging", "params": {}}}]},
    )
    assert (
        schedule_use(schedule, "mcp", SimpleNamespace(id=uuid.uuid4(), provider="bigquery")) == "provider requirement"
    )
    schedule.plan_json = {"steps": [{"type": "report.compose", "params": {"report_id": str(uuid.uuid4())}}]}
    assert schedule_use(schedule, "mcp", SimpleNamespace(id=uuid.uuid4(), provider="bigquery")) is None


def test_tracking_financial_report_requires_netsuite_without_a_fixed_period():
    from app.services.connection_usage import schedule_use

    schedule = Schedule(
        schedule_type="job",
        plan_json={
            "steps": [
                {
                    "type": "report.compose",
                    "params": {"playbook_key": "income_statement", "mode": "tracking", "params": {}},
                }
            ]
        },
    )
    netsuite = SimpleNamespace(id=uuid.uuid4(), provider="netsuite")
    assert schedule_use(schedule, "api", netsuite) == "provider requirement"
    assert schedule_use(schedule, "mcp", SimpleNamespace(id=uuid.uuid4(), provider="bigquery")) is None
    schedule.plan_json["steps"][0]["params"]["playbook_key"] = "inventory_aging"
    assert schedule_use(schedule, "api", netsuite) is None
    schedule.plan_json["steps"][0]["params"]["playbook_key"] = "unknown"
    assert schedule_use(schedule, "api", netsuite) is None
