"""Workspace agent post-validate narration + auto-propose.

Covers Task 10 of the workspace-validate-ux plan:
- _batch_hits_by_family groups ValidationHits by code
- _maybe_auto_propose_fix dispatches workspace_propose_patch ONLY for fixable
  codes, under loop budget, with fingerprint dedup
- workspace_run_validate is in the agent's tool allowlist
- system prompt advertises the post_validate_workflow block
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.workspace import ValidationHit
from app.services.chat.agents.workspace_agent import (
    _HIT_FAMILY_CITATION_CAP,
    WorkspaceAgent,
    _batch_hits_by_family,
    _maybe_auto_propose_fix,
)


def _make_hit(
    code: str | None,
    message: str = "msg",
    fingerprint: str | None = None,
) -> ValidationHit:
    return ValidationHit(
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        file_path="x.js",
        line=1,
        severity="warning",
        code=code,
        rule_id=None,
        message=message,
        fingerprint=fingerprint or (uuid.uuid4().hex + uuid.uuid4().hex)[:64],
    )


def test_batches_hits_by_code_family() -> None:
    hits = [
        _make_hit("OWASP-A03"),
        _make_hit("OWASP-A03"),
        _make_hit("SUITESCRIPT-DEPRECATED-2X"),
    ]
    families = _batch_hits_by_family(hits)
    assert set(families.keys()) == {"OWASP-A03", "SUITESCRIPT-DEPRECATED-2X"}
    assert len(families["OWASP-A03"]) == 2
    assert len(families["SUITESCRIPT-DEPRECATED-2X"]) == 1


def test_batches_uncoded_hits_under_uncoded_key() -> None:
    """A hit with code=None should land under 'UNCODED' family."""
    families = _batch_hits_by_family([_make_hit(None)])
    assert "UNCODED" in families


@pytest.mark.asyncio
async def test_auto_propose_called_only_for_fixable_codes() -> None:
    propose_mock = AsyncMock(return_value={"changeset_id": str(uuid.uuid4())})
    fixable_hit = _make_hit("SUITESCRIPT-DEPRECATED-2X", "nlapi deprecated", fingerprint="a" * 64)
    changeset_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()

    with (
        patch("app.services.chat.agents.workspace_agent.get_orchestrator") as orch_mock,
        patch("app.mcp.tools.workspace_tools.execute_propose_patch", new=propose_mock),
    ):
        orch = orch_mock.return_value
        orch.under_budget = lambda _cs: True
        orch.should_auto_propose = lambda _cs, _fp: True
        orch.record_auto_propose = lambda _cs, _fp: None
        orch.record_auto_fix = lambda _cs: None

        await _maybe_auto_propose_fix(
            hit=fixable_hit,
            changeset_id=changeset_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
    propose_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_propose_skipped_for_owasp() -> None:
    propose_mock = AsyncMock()
    owasp_hit = _make_hit("OWASP-A03", "injection", fingerprint="b" * 64)
    with (
        patch("app.services.chat.agents.workspace_agent.get_orchestrator") as orch_mock,
        patch("app.mcp.tools.workspace_tools.execute_propose_patch", new=propose_mock),
    ):
        orch = orch_mock.return_value
        orch.under_budget = lambda _cs: True
        orch.should_auto_propose = lambda _cs, _fp: True

        await _maybe_auto_propose_fix(
            hit=owasp_hit,
            changeset_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
    propose_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_propose_skipped_when_over_budget() -> None:
    """Once LOOP_BUDGET is exhausted, fixable hits also become narrate-only."""
    propose_mock = AsyncMock()
    fixable_hit = _make_hit("SUITESCRIPT-DEPRECATED-2X", "nlapi deprecated", fingerprint="c" * 64)
    with (
        patch("app.services.chat.agents.workspace_agent.get_orchestrator") as orch_mock,
        patch("app.mcp.tools.workspace_tools.execute_propose_patch", new=propose_mock),
    ):
        orch = orch_mock.return_value
        orch.under_budget = lambda _cs: False  # over budget

        await _maybe_auto_propose_fix(
            hit=fixable_hit,
            changeset_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
    propose_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_propose_skipped_when_fingerprint_already_proposed() -> None:
    """Same fingerprint in same changeset should not be auto-proposed twice."""
    propose_mock = AsyncMock()
    fixable_hit = _make_hit("SUITESCRIPT-DEPRECATED-2X", "nlapi deprecated", fingerprint="d" * 64)
    with (
        patch("app.services.chat.agents.workspace_agent.get_orchestrator") as orch_mock,
        patch("app.mcp.tools.workspace_tools.execute_propose_patch", new=propose_mock),
    ):
        orch = orch_mock.return_value
        orch.under_budget = lambda _cs: True
        orch.should_auto_propose = lambda _cs, _fp: False  # already proposed

        await _maybe_auto_propose_fix(
            hit=fixable_hit,
            changeset_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
    propose_mock.assert_not_awaited()


def test_workspace_agent_allows_run_validate_tool() -> None:
    agent = WorkspaceAgent(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="t")
    tool_names = {t["name"] for t in agent.tool_definitions}
    assert "workspace_run_validate" in tool_names


def test_post_validate_workflow_in_system_prompt() -> None:
    agent = WorkspaceAgent(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="t")
    assert "post_validate_workflow" in agent.system_prompt
    assert str(_HIT_FAMILY_CITATION_CAP) in agent.system_prompt
