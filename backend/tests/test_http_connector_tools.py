"""Saved HTTP connections become tenant-bound, approved agent tools."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.core.encryption import encrypt_credentials
from app.services.chat import http_connector_tools as http_tools
from app.services.chat.mutation_guard import classify_connector_mutation
from app.services.chat.tools import build_all_tool_definitions, execute_tool_call


@pytest.fixture
def connection():
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        provider="api",
        label="Warehouse API",
        status="active",
        metadata_json={"base_url": "https://api.example.com/v1/", "test_path": "orders"},
        encrypted_credentials=encrypt_credentials(
            {
                "base_url": "https://api.example.com/v1/",
                "test_path": "orders",
                "auth_type": "bearer",
                "token": "secret-token",
            }
        ),
    )


def database(row):
    db = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = row
    db.execute.return_value.scalars.return_value.all.return_value = [row] if row else []
    return db


async def test_http_connection_is_present_in_agent_inventory(connection, monkeypatch):
    monkeypatch.setattr("app.services.chat.tools.build_local_tool_definitions", lambda: [])
    monkeypatch.setattr(
        "app.services.mcp_connector_service.get_active_connectors_for_tenant", AsyncMock(return_value=[])
    )
    db = database(connection)
    tools = await build_all_tool_definitions(db, connection.tenant_id)
    definition = next(t for t in tools if t["name"] == f"http__{connection.id.hex}__get")
    assert "Warehouse API" in definition["description"] and "orders" in definition["description"]
    assert "secret-token" not in json.dumps(tools)
    sql = str(
        db.execute.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert str(connection.tenant_id) in sql and "active" in sql and "provider" in sql


async def test_http_call_is_classified_and_cannot_bypass_approval(connection, monkeypatch):
    call = AsyncMock(return_value={"orders": []})
    monkeypatch.setattr("app.services.http_connector_service.read_json", call)
    name = f"http__{connection.id.hex}__get"
    assert await classify_connector_mutation(name, database(connection), connection.tenant_id) == "execute"
    output = await execute_tool_call(
        name, {"path": "orders"}, connection.tenant_id, uuid4(), "test", database(connection)
    )
    assert json.loads(output)["hitl_required"]
    call.assert_not_called()


async def test_approved_http_call_rechecks_actor_and_connection_and_redacts_secrets(connection, monkeypatch):
    monkeypatch.setattr(http_tools, "_authorize_actor", AsyncMock(return_value=True))
    call = AsyncMock(return_value={"orders": [{"total": "12.30"}], "echo": "Bearer secret-token", "password": "hidden"})
    monkeypatch.setattr("app.services.http_connector_service.read_json", call)
    db = database(connection)
    output = await execute_tool_call(
        f"http__{connection.id.hex}__get",
        {"path": "orders?page=1"},
        connection.tenant_id,
        uuid4(),
        "test",
        db,
        human_approved=True,
    )
    data = json.loads(output)
    assert data["data"]["orders"] == [{"total": "12.30"}]
    assert "secret-token" not in output and "hidden" not in output and data["redacted"] is True
    assert call.await_args.args[1] == "orders?page=1"
    sql = str(
        db.execute.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert str(connection.tenant_id) in sql and str(connection.id) in sql and "active" in sql


@pytest.mark.parametrize(
    "params",
    [
        {"path": "https://evil.test"},
        {"path": "../secrets"},
        {"path": "%252e%252e/secrets"},
        {"path": "orders", "method": "POST"},
        {"path": "orders", "headers": {"Host": "evil.test"}},
    ],
)
async def test_http_tool_rejects_unsafe_or_extra_parameters(connection, params, monkeypatch):
    monkeypatch.setattr(http_tools, "_authorize_actor", AsyncMock(return_value=True))
    call = AsyncMock()
    monkeypatch.setattr("app.services.http_connector_service.read_json", call)
    result = await http_tools.execute(
        connection.id, params, connection.tenant_id, uuid4(), database(connection), human_approved=True
    )
    assert result["error"] == "invalid_parameters"
    call.assert_not_called()


@pytest.mark.parametrize("available, authorized", [(False, True), (True, False)])
async def test_deleted_cross_tenant_or_unauthorized_connection_never_executes(
    connection, available, authorized, monkeypatch
):
    monkeypatch.setattr(http_tools, "_authorize_actor", AsyncMock(return_value=authorized))
    call = AsyncMock()
    monkeypatch.setattr("app.services.http_connector_service.read_json", call)
    result = await http_tools.execute(
        connection.id,
        {"path": "orders"},
        connection.tenant_id,
        uuid4(),
        database(connection if available else None),
        human_approved=True,
    )
    assert "error" in result
    call.assert_not_called()


async def test_large_results_are_not_fed_to_model(connection, monkeypatch):
    monkeypatch.setattr(http_tools, "_authorize_actor", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.http_connector_service.read_json", AsyncMock(return_value={"data": "x" * 25000}))
    result = await http_tools.execute(
        connection.id, {"path": "orders"}, connection.tenant_id, uuid4(), database(connection), human_approved=True
    )
    assert result["error"] == "response_too_large_for_chat" and len(json.dumps(result)) < 1000


async def test_approved_api_response_is_saved_in_conversation_for_agent_use(connection, monkeypatch):
    from app.services.chat.orchestrator import run_chat_turn
    from app.services.chat.write_confirmation_service import build_confirmation_payload
    from tests.test_write_confirm_orchestrator import (
        _TENANT_ID,
        _USER_ID,
        _make_db,
        _make_real_confirmation_msg,
        _make_session,
    )

    session_id = uuid4()
    name = f"http__{connection.id.hex}__get"
    card = build_confirmation_payload(
        mutation_type="execute",
        record_type="API GET · Warehouse",
        tool_name=name,
        tool_input={"path": "orders"},
        session_id=str(session_id),
    )
    message = _make_real_confirmation_msg(
        session_id, name, {"recordType": "customer", "body": {"companyName": "unused"}}
    )
    message.structured_output = card.model_dump()
    db = _make_db(message)
    execute = AsyncMock(
        return_value=json.dumps({"success": True, "data": {"orders": [{"number": "R123456789", "total": "12.30"}]}})
    )
    monkeypatch.setattr("app.services.chat.orchestrator.execute_tool_call", execute)
    monkeypatch.setattr("app.services.chat.orchestrator.log_event", AsyncMock())
    events = [
        event
        async for event in run_chat_turn(
            db=db,
            session=_make_session(session_id=str(session_id)),
            user_message="approve",
            user_id=_USER_ID,
            tenant_id=_TENANT_ID,
            write_confirm={"action": "approve", "confirmation_id": str(message.id)},
        )
    ]
    content = next(e["message"]["content"] for e in events if e.get("type") == "message")
    assert "R123456789" in content and "12.30" in content
    assert execute.await_args.kwargs["human_approved"] is True
    assert message.structured_output["status"] == "approved"


@pytest.mark.parametrize("actor_exists, permission", [(False, True), (True, False), (True, True)])
async def test_actor_authorization_checks_tenant_active_user_and_permission(
    connection, actor_exists, permission, monkeypatch
):
    actor_id = uuid4()
    db = database(actor_id if actor_exists else None)
    check = AsyncMock(return_value=permission)
    monkeypatch.setattr("app.core.dependencies.has_permission", check)
    assert await http_tools._authorize_actor(db, connection.tenant_id, actor_id) is (actor_exists and permission)
    sql = str(
        db.execute.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert str(actor_id) in sql and str(connection.tenant_id) in sql
    assert "actor_type" in sql and "is_active" in sql
    if actor_exists:
        check.assert_awaited_once_with(db, actor_id, "connections.view")
    else:
        check.assert_not_called()


def test_approved_result_display_is_bounded():
    from app.services.chat.write_confirmation_service import format_external_result

    assert "smaller page" in format_external_result({"rows": "x" * 25000})
    assert len(format_external_result({"rows": "x" * 25000})) < 200
