"""MCP tool tests for the two-step gated SuiteCloud sandbox deploy.

Tests 21-24 in spec:
  21 — execute_deploy_sandbox (preview tool) returns confirmation_required
       shape — does NOT create a WorkspaceRun.
  22 — execute_deploy_sandbox_confirm happy path: verifies token, queues
       run, marks token consumed.
  23 — execute_deploy_sandbox_confirm rejects forged HMAC.
  24 — registry + governance entries exist for both tools.

Closes codex P1 #2 — the MCP bypass that let chat-agent deploys skip the
preview→confirm gate.
"""

from __future__ import annotations

import contextlib
import hashlib
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.tools import workspace_tools
from app.models.workspace import (
    Workspace,
    WorkspaceChangeSet,
    WorkspaceDeployToken,
    WorkspaceFile,
    WorkspaceRun,
)


_FAKE_MOD_KEY = "app.workers.tasks.workspace_run"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _mock_workspace_run_task():
    fake_module = ModuleType(_FAKE_MOD_KEY)
    fake_task = MagicMock()
    fake_task.delay = MagicMock()
    fake_module.workspace_run_task = fake_task

    old = sys.modules.get(_FAKE_MOD_KEY)
    sys.modules[_FAKE_MOD_KEY] = fake_module
    try:
        yield fake_task
    finally:
        if old is not None:
            sys.modules[_FAKE_MOD_KEY] = old
        else:
            sys.modules.pop(_FAKE_MOD_KEY, None)


@pytest_asyncio.fixture
async def mcp_deploy_eligible(db: AsyncSession, tenant_a, admin_user):
    """Approved changeset with passing gates so MCP tool can mint a token."""
    user, _ = admin_user
    ws = Workspace(
        tenant_id=tenant_a.id,
        name="MCP deploy WS",
        created_by=user.id,
        status="active",
    )
    db.add(ws)
    await db.flush()

    db.add(
        WorkspaceFile(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            path="SuiteScripts/mcp_deploy.js",
            file_name="mcp_deploy.js",
            content="console.log('mcp deploy');",
            sha256_hash=_sha256("console.log('mcp deploy');"),
            size_bytes=26,
            is_directory=False,
        )
    )
    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id,
        workspace_id=ws.id,
        title="MCP deploy cs",
        status="approved",
        proposed_by=user.id,
    )
    db.add(cs)
    await db.flush()

    db.add(
        WorkspaceRun(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            changeset_id=cs.id,
            run_type="suitecloud_validate",
            status="passed",
            triggered_by=user.id,
            has_errors=False,
            gate_status="pass",
        )
    )
    db.add(
        WorkspaceRun(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            changeset_id=cs.id,
            run_type="jest_unit_test",
            status="passed",
            triggered_by=user.id,
        )
    )
    await db.flush()
    return ws, cs, user


def _mcp_context(db, tenant_id, actor_id):
    return {"db": db, "tenant_id": str(tenant_id), "actor_id": str(actor_id)}


class TestMcpDeployPreviewBypassClosed:
    """Test 21: MCP preview tool returns confirmation_required, no run queued."""

    @pytest.mark.asyncio
    async def test_21_preview_tool_returns_confirmation_required_no_run(
        self, db: AsyncSession, mcp_deploy_eligible, tenant_a
    ):
        ws, cs, user = mcp_deploy_eligible
        ctx = _mcp_context(db, tenant_a.id, user.id)

        result = await workspace_tools.execute_deploy_sandbox(
            params={"changeset_id": str(cs.id), "sandbox_id": "6738075-sb1"},
            context=ctx,
        )

        assert result.get("confirmation_required") is True
        assert result["confirmation_type"] == "sandbox_deploy"
        assert result["row_count"] == 0
        assert "preview" in result
        preview = result["preview"]
        assert "jti" in preview
        assert "confirmation_token" in preview
        assert "manifest" in preview

        # Critically: NO WorkspaceRun has been created. Closes codex P1 #2.
        runs = await db.execute(
            select(WorkspaceRun).where(
                WorkspaceRun.tenant_id == tenant_a.id,
                WorkspaceRun.changeset_id == cs.id,
                WorkspaceRun.run_type == "deploy_sandbox",
            )
        )
        assert runs.scalar_one_or_none() is None, (
            "MCP preview MUST NOT queue a deploy run — it only mints a token"
        )


class TestMcpDeployConfirm:
    """Tests 22 + 23: execute_deploy_sandbox_confirm tool."""

    @pytest.mark.asyncio
    async def test_22_confirm_happy_path_queues_run(
        self, db: AsyncSession, mcp_deploy_eligible, tenant_a
    ):
        ws, cs, user = mcp_deploy_eligible
        ctx = _mcp_context(db, tenant_a.id, user.id)

        # 1. Mint via preview tool.
        preview_result = await workspace_tools.execute_deploy_sandbox(
            params={"changeset_id": str(cs.id), "sandbox_id": "6738075-sb1"},
            context=ctx,
        )
        preview = preview_result["preview"]

        # 2. Confirm via the new tool.
        with _mock_workspace_run_task() as fake_task:
            confirm_result = await workspace_tools.execute_deploy_sandbox_confirm(
                params={
                    "jti": preview["jti"],
                    "confirmation_token": preview["confirmation_token"],
                },
                context=ctx,
            )

            assert confirm_result["row_count"] == 1
            assert "run_id" in confirm_result
            assert fake_task.delay.called
            call_kwargs = fake_task.delay.call_args.kwargs
            assert (
                call_kwargs["extra_params"]["expected_snapshot_sha"]
                == preview["snapshot_sha"]
            )

        # Token row marked consumed + linked to the run.
        import uuid as _uuid

        token_row = await db.execute(
            select(WorkspaceDeployToken).where(
                WorkspaceDeployToken.id == _uuid.UUID(preview["jti"])
            )
        )
        row = token_row.scalar_one()
        assert row.consumed_at is not None
        assert str(row.consumed_run_id) == confirm_result["run_id"]

    @pytest.mark.asyncio
    async def test_23_confirm_rejects_forged_token(
        self, db: AsyncSession, mcp_deploy_eligible, tenant_a
    ):
        ws, cs, user = mcp_deploy_eligible
        ctx = _mcp_context(db, tenant_a.id, user.id)

        preview_result = await workspace_tools.execute_deploy_sandbox(
            params={"changeset_id": str(cs.id), "sandbox_id": "6738075-sb1"},
            context=ctx,
        )
        preview = preview_result["preview"]

        forged = "0" * 64

        confirm_result = await workspace_tools.execute_deploy_sandbox_confirm(
            params={
                "jti": preview["jti"],
                "confirmation_token": forged,
            },
            context=ctx,
        )

        # Tool returns error-shape (row_count=0, error message present).
        assert confirm_result["row_count"] == 0
        assert "error" in confirm_result


class TestMcpAuditRedaction:
    """Codex P2 regression: confirmation_token must NOT land in
    audit_events.payload. Token fingerprint (sha256(token)[:16]) is the
    only token-derived value safe to persist.
    """

    def test_governance_redacts_confirmation_token(self):
        from app.mcp.governance import _SENSITIVE_KEYS, redact_result

        assert "confirmation_token" in _SENSITIVE_KEYS
        redacted = redact_result({"confirmation_token": "abc" * 32, "jti": "x"})
        assert redacted["confirmation_token"] == "***REDACTED***"
        assert redacted["jti"] == "x"


class TestMcpRegistryAndGovernance:
    """Test 24: tool registry + governance carry both preview + confirm."""

    def test_24_registry_and_governance_have_both_tools(self):
        from app.mcp import governance, registry

        assert "workspace.deploy_sandbox" in registry.TOOL_REGISTRY
        assert "workspace.deploy_sandbox_confirm" in registry.TOOL_REGISTRY
        assert "workspace.deploy_sandbox" in governance.TOOL_CONFIGS
        assert "workspace.deploy_sandbox_confirm" in governance.TOOL_CONFIGS

        # Both require the workspace entitlement.
        for tool_name in ("workspace.deploy_sandbox", "workspace.deploy_sandbox_confirm"):
            assert (
                governance.TOOL_CONFIGS[tool_name]["requires_entitlement"] == "workspace"
            )

        # Confirm tool's allowlisted params are tightly scoped.
        confirm_gov = governance.TOOL_CONFIGS["workspace.deploy_sandbox_confirm"]
        assert set(confirm_gov["allowlisted_params"]) == {"jti", "confirmation_token"}
