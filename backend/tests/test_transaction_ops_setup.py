"""Read-only setup catalog unit checks; no database or provider I/O."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1.transaction_ops_setup import _option_queries, list_setup_options


@pytest.fixture(autouse=True)
def scoped_connection(monkeypatch):
    monkeypatch.setattr("app.api.v1.transaction_ops_setup.set_tenant_context", AsyncMock())


def _result(rows):
    return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))


@pytest.mark.asyncio
async def test_setup_lists_independent_pages_and_projects_only_safe_metadata():
    tenant = uuid4()
    source = {
        "id": uuid4(),
        "reference_name": "Get New Orders",
        "flow_name": "EU flow",
        "integration_name": "Orders",
        "connection_label": "Celigo",
        "mirrored_at": None,
        "sandbox": True,
        "record_type": None,
        "operation": None,
    }
    target = {
        **source,
        "id": uuid4(),
        "reference_name": "Create Sales Order",
        "record_type": "salesorder",
        "operation": "add",
    }
    connection = {"id": uuid4(), "label": "NS sandbox", "account_id": "EXAMPLE_SB1", "status": "active"}
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _result([source, {**source, "id": uuid4()}]),
                _result([target]),
                _result([connection]),
                _result([]),
            ]
        )
    )
    response = await list_setup_options(SimpleNamespace(tenant_id=tenant), db, offset=0, limit=1)
    assert response.source_has_more is True
    assert response.target_has_more is False
    assert response.connection_has_more is False
    assert response.source_steps[0].sandbox is True
    assert response.source_steps[0].provider_verified is False
    assert response.netsuite_connections[0].account_id == "EXAMPLE_SB1"
    assert "raw_json" not in response.model_dump_json()
    assert "credentials" not in response.model_dump_json()


def test_catalog_queries_confine_every_join_and_apply_independent_bounds():
    tenant = uuid4()
    queries = _option_queries(tenant, offset=100, limit=100)
    assert len(queries) == 4
    for query in queries:
        compiled = query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        sql = str(compiled)
        assert str(tenant) in sql
        assert "LIMIT 101 OFFSET 100" in sql
        assert "connections.tenant_id" in sql
        assert "encrypted_credentials" not in sql
        assert "raw_json" not in sql
        assert "active" in sql and "healthy" in sql
    for query in queries[:2]:
        sql = str(query.compile(dialect=postgresql.dialect()))
        assert "celigo_flow_steps.tenant_id" in sql
        assert "celigo_flows.tenant_id" in sql
        assert "celigo_integrations.tenant_id" in sql
        assert "celigo_flows.disabled IS NOT true" in sql
        assert "sandbox IS NOT" not in sql  # environments remain explicit options


@pytest.mark.asyncio
async def test_missing_account_metadata_remains_unknown():
    connection = {"id": uuid4(), "label": "Older connection", "account_id": None, "status": "healthy"}
    db = SimpleNamespace(execute=AsyncMock(side_effect=[_result([]), _result([]), _result([connection]), _result([])]))
    response = await list_setup_options(SimpleNamespace(tenant_id=uuid4()), db, offset=0, limit=100)
    assert response.netsuite_connections[0].account_id is None
