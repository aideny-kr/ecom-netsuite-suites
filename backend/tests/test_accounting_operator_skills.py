from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.mcp.tools.agent_skill import execute
from app.services.chat.agents.unified_agent import UnifiedAgent
from app.services.chat.skills import get_skill_instructions, match_skill
from app.services.chat.tools import build_external_tool_definitions, build_local_tool_definitions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slug", ["accounting_operations", "netsuite_subledger", "accounting_treatments", "accounting_verification"]
)
async def test_load_maintained_skill_and_revision(slug):
    result = await execute({"slug": slug})
    assert result["success"]
    assert result["instructions"] == get_skill_instructions(slug)
    assert result["revision"] == sha256(result["instructions"].encode()).hexdigest()
    assert "approval" in result["authority"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"slug": "../../../../.env"},
        {"slug": "unknown"},
        {"slug": []},
        {"slug": "netsuite_subledger", "path": "/tmp/secret"},
    ],
)
async def test_skill_rejects_unknown_paths_and_extra_arguments(params):
    assert (await execute(params))["success"] is False


@pytest.mark.asyncio
async def test_skill_read_audits_revision_in_supplied_tenant():
    tenant = uuid4()
    db = object()
    with patch("app.services.audit_service.log_event", new_callable=AsyncMock) as audit:
        result = await execute(
            {"slug": "netsuite_subledger"}, context={"db": db, "tenant_id": tenant, "correlation_id": "test"}
        )
    assert result["success"]
    assert audit.await_args.args == (db, tenant)
    assert audit.await_args.kwargs["payload"]["revision"] == result["revision"]
    assert audit.await_args.kwargs["payload"]["financial_writes"] == 0


def test_skill_is_available_through_actual_chat_inventory():
    tools = build_local_tool_definitions()
    assert any(t["name"] == "agent_skill" for t in tools)
    assert match_skill("Investigate transaction case abc")["slug"] == "accounting_operations"
    from app.services.transaction_ops.accounting_references import TOPICS

    reference = next(t for t in tools if t["name"] == "transaction_ops_accounting_reference")
    assert reference["input_schema"]["properties"]["topic"]["enum"] == list(TOPICS)


def test_transaction_prompt_loads_accounting_core_without_eager_metabase_manuals():
    connector = SimpleNamespace(
        id=uuid4(),
        provider="metabase_mcp",
        label="Metabase",
        server_url="https://bi.example.com/mcp",
        metadata_json=None,
        discovered_tools=[
            {"name": "execute_query", "description": "query", "input_schema": {"type": "object", "properties": {}}}
        ],
    )
    agent = UnifiedAgent(tenant_id=uuid4(), user_id=uuid4(), correlation_id="test")
    agent._connectors = [connector]
    agent._tool_defs = build_local_tool_definitions() + build_external_tool_definitions([connector])
    ordinary = agent.system_prompt
    assert "# Metabase SQL Analysis" in ordinary
    agent._transaction_workflow = True
    prompt = agent.system_prompt
    assert "# Accounting operations" in prompt
    assert "# Metabase SQL Analysis" not in prompt
    assert "# Metabase BI Analysis" not in prompt
    assert "netsuite_subledger" in prompt and "accounting_verification" in prompt
    assert "agent_skill" in prompt
    assert any(t["name"].endswith("execute_query") for t in agent._tool_defs)
    # Account selection and write constraints remain in the final prompt.
    assert "human approval" in prompt
    assert "exact record_links" in prompt
    agent._transaction_workflow = False
    assert "# Metabase SQL Analysis" in agent.system_prompt
