"""Worker-parity regression for ``execute_tool_call`` success logging.

The celery worker hijacks the root logger to INFO, so module loggers that are
silent no-ops under uvicorn (default WARNING) actually EXECUTE their
``logger.info`` calls in the worker. A structlog-style kwargs call on a stdlib
logger raises ``TypeError: Logger._log() got an unexpected keyword argument``
AFTER the tool has already succeeded — the local branch converts the success
into an error payload; the external branch raises straight through. This broke
every unattended report auto-refresh (first worker-side caller of
``execute_tool_call``) while chat/API traffic never saw it.

These tests pin INFO-enabled logging (the worker environment) and assert a
successful tool result survives ``execute_tool_call`` intact.
"""

from __future__ import annotations

import json
import logging
import uuid
from unittest.mock import AsyncMock

import pytest

import app.services.chat.tools as tools_mod
from app.services.chat.tools import execute_tool_call

pytestmark = pytest.mark.asyncio

_TENANT_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _info_enabled_like_celery():
    """Celery's root-logger hijack enables INFO everywhere; reproduce that for
    the module under test so its success-log lines actually execute."""
    logger = logging.getLogger("app.services.chat.tools")
    previous = logger.level
    logger.setLevel(logging.INFO)
    yield
    logger.setLevel(previous)


@pytest.fixture(autouse=True)
def _mock_external_tool_audit(monkeypatch):
    """external_tool_audit.append_event opens its own async_session_factory() session per
    call, independent of the `db=object()` doubles this module passes around — by design
    (fail-closed audit, see external_tool_audit.py). These are routing/logging unit tests,
    not a DB integration test, so patch it rather than let it try a real session (which
    raises 'Event loop is closed' once this test's own event loop has torn down)."""
    mock = AsyncMock()
    monkeypatch.setattr("app.services.chat.external_tool_audit.append_event", mock)
    return mock


async def test_local_tool_success_survives_info_logging(monkeypatch):
    """A successful LOCAL tool call must return the tool result, not an error
    payload manufactured by the success-log line (system/worker actor path)."""

    async def fake_call_tool(**kwargs):
        return {"success": True, "rows": [["Cash", 42]], "row_count": 1}

    monkeypatch.setattr(tools_mod.mcp_server, "call_tool", fake_call_tool)

    tool_name = next(iter(tools_mod._LOCAL_NAME_MAP))
    result_str = await execute_tool_call(
        tool_name,
        {"query": "SELECT 1"},
        tenant_id=_TENANT_ID,
        actor_id=None,  # system actor, as the report auto-refresh sweep dispatches
        actor_type="system",
        correlation_id="test-corr",
        db=object(),
    )

    result = json.loads(result_str)
    assert "error" not in result, f"success turned into error payload: {result}"
    assert result["rows"] == [["Cash", 42]]


async def test_external_tool_success_survives_info_logging(monkeypatch, _mock_external_tool_audit):
    """A successful EXTERNAL tool call must not raise from the success-log line
    (this branch has no try/except — a logging TypeError propagates)."""

    # Signature must track the real _execute_external_tool, which gained
    # `human_approved` when the HITL guard moved to the dispatcher. A double
    # that drifts from the function it stands in for fails on the call, not
    # on the behaviour under test.
    async def fake_external(connector_id, raw_tool_name, tool_input, tenant_id, db, human_approved=False):
        return {"data": [{"amount": 42}]}

    monkeypatch.setattr(tools_mod, "_execute_external_tool", fake_external)

    connector_hex = uuid.uuid4().hex
    result_str = await execute_tool_call(
        f"ext__{connector_hex}__ns_runReport",
        {"reportId": -203},
        tenant_id=_TENANT_ID,
        actor_id=_ACTOR_ID,
        correlation_id="test-corr",
        db=object(),
    )

    result = json.loads(result_str)
    assert result["data"] == [{"amount": 42}]
    requested = [c for c in _mock_external_tool_audit.await_args_list if c.kwargs["action"] == "tool.requested"]
    assert requested, "external call never reached external_tool_audit.append_event"
