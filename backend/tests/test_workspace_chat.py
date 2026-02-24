"""Phase 3C.1 Tests — Workspace Reindex + Workspace-Scoped Chat Sessions.

Test classes:
1. TestReindexEndpoint — reindex success, hash fix, 404, tenant isolation, audit
2. TestWorkspaceScopedSessions — create with workspace_id, backward compat, list filters
3. TestWorkspaceContextInjection — system prompt includes workspace files, tool auto-injection
4. TestTenantIsolation — cross-tenant workspace session access blocked
5. TestAuditEvents — reindex audit event, chat.turn includes workspace_id
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.models.chat import ChatSession
from app.models.workspace import Workspace, WorkspaceFile

_FAKE_AI_CONFIG = ("anthropic", "claude-3-haiku-20240307", "fake-key", False)
_ORCH = "app.services.chat.orchestrator"


# ---- Async generator helpers ----


async def _collect_stream_result(async_gen):
    """Consume the run_chat_turn async generator and return the final message dict."""
    result = None
    async for chunk in async_gen:
        if chunk.get("type") == "message":
            result = chunk["message"]
    return result


def _make_stream_side_effect(responses):
    call_count = 0

    async def stream_fn(**kwargs):
        nonlocal call_count
        resp = responses[call_count] if call_count < len(responses) else responses[-1]
        call_count += 1
        for text in resp.text_blocks:
            yield "text", text
        yield "response", resp

    return stream_fn


# ---- Fixtures ----


@pytest_asyncio.fixture
async def workspace_a(db: AsyncSession, tenant_a, admin_user):
    """Create a workspace with files for tenant A."""
    user, _ = admin_user
    ws = Workspace(
        tenant_id=tenant_a.id,
        name="Chat Test Workspace",
        created_by=user.id,
        status="active",
    )
    db.add(ws)
    await db.flush()

    # Add some files
    for path, name, content in [
        ("SuiteScripts/main.js", "main.js", "console.log('main');"),
        ("SuiteScripts/helper.js", "helper.js", "function help() {}"),
        ("Objects/record.xml", "record.xml", "<record/>"),
    ]:
        from app.services.workspace_service import _sha256

        f = WorkspaceFile(
            tenant_id=tenant_a.id,
            workspace_id=ws.id,
            path=path,
            file_name=name,
            content=content,
            sha256_hash=_sha256(content),
            size_bytes=len(content),
            is_directory=False,
        )
        db.add(f)
    await db.flush()
    return ws


@pytest_asyncio.fixture
async def workspace_b(db: AsyncSession, tenant_b, admin_user_b):
    """Create a workspace for tenant B."""
    user, _ = admin_user_b
    ws = Workspace(
        tenant_id=tenant_b.id,
        name="Tenant B Workspace",
        created_by=user.id,
        status="active",
    )
    db.add(ws)
    await db.flush()
    return ws


# ============================================================
# 1. TestReindexEndpoint
# ============================================================


class TestReindexEndpoint:
    @pytest.mark.asyncio
    async def test_reindex_success(self, client, admin_user, workspace_a):
        _, headers = admin_user
        resp = await client.post(
            f"/api/v1/workspaces/{workspace_a.id}/reindex",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["files_reindexed"] == 3
        assert data["hash_mismatches_fixed"] == 0

    @pytest.mark.asyncio
    async def test_reindex_fixes_hash_mismatch(self, db, client, admin_user, workspace_a):
        _, headers = admin_user
        # Corrupt a hash
        result = await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == workspace_a.id,
                WorkspaceFile.file_name == "main.js",
            )
        )
        f = result.scalar_one()
        f.sha256_hash = "corrupted_hash"
        await db.flush()

        resp = await client.post(
            f"/api/v1/workspaces/{workspace_a.id}/reindex",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["hash_mismatches_fixed"] == 1

    @pytest.mark.asyncio
    async def test_reindex_404_missing_workspace(self, client, admin_user):
        _, headers = admin_user
        fake_id = uuid.uuid4()
        resp = await client.post(
            f"/api/v1/workspaces/{fake_id}/reindex",
            headers=headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_reindex_tenant_isolation(self, client, admin_user_b, workspace_a):
        """Tenant B cannot reindex tenant A's workspace."""
        _, headers_b = admin_user_b
        resp = await client.post(
            f"/api/v1/workspaces/{workspace_a.id}/reindex",
            headers=headers_b,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_reindex_audit_event(self, db, client, admin_user, workspace_a):
        _, headers = admin_user
        await client.post(
            f"/api/v1/workspaces/{workspace_a.id}/reindex",
            headers=headers,
        )
        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "workspace.reindexed",
                AuditEvent.resource_id == str(workspace_a.id),
            )
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.payload["files_reindexed"] == 3


# ============================================================
# 2. TestWorkspaceScopedSessions
# ============================================================


class TestWorkspaceScopedSessions:
    @pytest.mark.asyncio
    async def test_create_session_with_workspace_id(self, client, admin_user, workspace_a):
        _, headers = admin_user
        resp = await client.post(
            "/api/v1/chat/sessions",
            json={"workspace_id": str(workspace_a.id)},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["workspace_id"] == str(workspace_a.id)

    @pytest.mark.asyncio
    async def test_create_session_without_workspace_id(self, client, admin_user):
        """Backward compat: sessions without workspace_id still work."""
        _, headers = admin_user
        resp = await client.post(
            "/api/v1/chat/sessions",
            json={},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["workspace_id"] is None

    @pytest.mark.asyncio
    async def test_list_sessions_filters_by_workspace(self, client, admin_user, workspace_a):
        _, headers = admin_user
        # Create one workspace session and one general session
        await client.post(
            "/api/v1/chat/sessions",
            json={"workspace_id": str(workspace_a.id)},
            headers=headers,
        )
        await client.post(
            "/api/v1/chat/sessions",
            json={},
            headers=headers,
        )

        # List workspace sessions
        resp = await client.get(
            f"/api/v1/chat/sessions?workspace_id={workspace_a.id}",
            headers=headers,
        )
        assert resp.status_code == 200
        sessions = resp.json()
        assert len(sessions) == 1
        assert sessions[0]["workspace_id"] == str(workspace_a.id)

    @pytest.mark.asyncio
    async def test_list_sessions_general_only_when_no_filter(self, client, admin_user, workspace_a):
        _, headers = admin_user
        # Create one workspace session and one general session
        await client.post(
            "/api/v1/chat/sessions",
            json={"workspace_id": str(workspace_a.id)},
            headers=headers,
        )
        await client.post(
            "/api/v1/chat/sessions",
            json={"title": "General chat"},
            headers=headers,
        )

        # List without workspace_id filter — should only return general sessions
        resp = await client.get(
            "/api/v1/chat/sessions",
            headers=headers,
        )
        assert resp.status_code == 200
        sessions = resp.json()
        assert all(s["workspace_id"] is None for s in sessions)


# ============================================================
# 3. TestWorkspaceContextInjection
# ============================================================


class TestWorkspaceContextInjection:
    @pytest.mark.asyncio
    async def test_system_prompt_includes_workspace_files(self, db, tenant_a, admin_user, workspace_a):
        """Orchestrator injects workspace file listing into system prompt."""
        user, _ = admin_user

        # Create a workspace-scoped session
        session = ChatSession(
            tenant_id=tenant_a.id,
            user_id=user.id,
            workspace_id=workspace_a.id,
        )
        db.add(session)
        await db.flush()
        await db.refresh(session, ["messages"])

        # Mock the LLM adapter to capture what's sent
        mock_response = MagicMock()
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=20)
        mock_response.tool_use_blocks = []
        mock_response.text_blocks = ["Hello from workspace!"]

        captured_kwargs = {}

        async def _capturing_stream(**kwargs):
            captured_kwargs.update(kwargs)
            for text in mock_response.text_blocks:
                yield "text", text
            yield "response", mock_response

        mock_adapter = AsyncMock()
        mock_adapter.create_message = AsyncMock(return_value=mock_response)
        mock_adapter.stream_message = _capturing_stream
        mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": "test"})

        with (
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.get_tenant_ai_config", return_value=_FAKE_AI_CONFIG),
            patch(f"{_ORCH}.get_active_template", return_value="You are helpful."),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(f"{_ORCH}.deduct_chat_credits", new_callable=AsyncMock, return_value=None),
        ):
            from app.services.chat.orchestrator import run_chat_turn

            async for _ in run_chat_turn(
                db=db,
                session=session,
                user_message="List workspace files",
                user_id=user.id,
                tenant_id=tenant_a.id,
            ):
                pass

            # Verify stream_message was called with system prompt containing workspace info
            system_prompt = captured_kwargs["system"]
            assert "WORKSPACE CONTEXT" in system_prompt
            assert "Chat Test Workspace" in system_prompt
            assert "SuiteScripts/main.js" in system_prompt
            assert "SuiteScripts/helper.js" in system_prompt

    @pytest.mark.asyncio
    async def test_tool_auto_injection_adds_workspace_id(self, db, tenant_a, admin_user, workspace_a):
        """workspace_id is auto-injected into workspace_ tool calls."""
        user, _ = admin_user

        session = ChatSession(
            tenant_id=tenant_a.id,
            user_id=user.id,
            workspace_id=workspace_a.id,
        )
        db.add(session)
        await db.flush()
        await db.refresh(session, ["messages"])

        # Mock adapter: first call returns a tool_use, second returns text
        tool_block = MagicMock()
        tool_block.id = "tool_1"
        tool_block.name = "workspace_list_files"
        tool_block.input = {}  # No workspace_id — should be auto-injected

        response_with_tool = MagicMock()
        response_with_tool.usage = MagicMock(input_tokens=10, output_tokens=20)
        response_with_tool.tool_use_blocks = [tool_block]
        response_with_tool.text_blocks = []

        response_text = MagicMock()
        response_text.usage = MagicMock(input_tokens=10, output_tokens=20)
        response_text.tool_use_blocks = []
        response_text.text_blocks = ["Here are the files."]

        mock_adapter = AsyncMock()
        mock_adapter.create_message = AsyncMock(side_effect=[response_with_tool, response_text])
        mock_adapter.stream_message = _make_stream_side_effect([response_with_tool, response_text])
        mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": "test"})
        mock_adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": "tool result"})

        with (
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.get_tenant_ai_config", return_value=_FAKE_AI_CONFIG),
            patch(f"{_ORCH}.get_active_template", return_value="You are helpful."),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(f"{_ORCH}.build_all_tool_definitions", return_value=[]),
            patch(f"{_ORCH}.execute_tool_call", new_callable=AsyncMock, return_value='{"files": []}'),
            patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
            patch(f"{_ORCH}.deduct_chat_credits", new_callable=AsyncMock, return_value=None),
        ):
            from app.services.chat.orchestrator import run_chat_turn

            async for _ in run_chat_turn(
                db=db,
                session=session,
                user_message="Show me workspace files",
                user_id=user.id,
                tenant_id=tenant_a.id,
            ):
                pass

            # Verify workspace_id was injected into tool_block.input
            assert tool_block.input.get("workspace_id") == str(workspace_a.id)


# ============================================================
# 4. TestTenantIsolation
# ============================================================


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_tenant_b_cannot_list_tenant_a_workspace_sessions(
        self, client, admin_user, admin_user_b, workspace_a
    ):
        """Tenant B cannot see workspace sessions from tenant A."""
        _, headers_a = admin_user
        _, headers_b = admin_user_b

        # Create workspace session for tenant A
        resp = await client.post(
            "/api/v1/chat/sessions",
            json={"workspace_id": str(workspace_a.id)},
            headers=headers_a,
        )
        assert resp.status_code == 201

        # Tenant B lists sessions for same workspace — should see nothing
        resp = await client.get(
            f"/api/v1/chat/sessions?workspace_id={workspace_a.id}",
            headers=headers_b,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    @pytest.mark.asyncio
    async def test_tenant_b_cannot_access_tenant_a_session_detail(self, client, admin_user, admin_user_b, workspace_a):
        """Tenant B cannot fetch detail of tenant A's session."""
        _, headers_a = admin_user
        _, headers_b = admin_user_b

        resp = await client.post(
            "/api/v1/chat/sessions",
            json={"workspace_id": str(workspace_a.id)},
            headers=headers_a,
        )
        session_id = resp.json()["id"]

        resp = await client.get(
            f"/api/v1/chat/sessions/{session_id}",
            headers=headers_b,
        )
        assert resp.status_code == 404


# ============================================================
# 5. TestAuditEvents
# ============================================================


class TestAuditEvents:
    @pytest.mark.asyncio
    async def test_reindex_creates_audit_event(self, db, client, admin_user, workspace_a):
        _, headers = admin_user
        await client.post(
            f"/api/v1/workspaces/{workspace_a.id}/reindex",
            headers=headers,
        )

        result = await db.execute(select(AuditEvent).where(AuditEvent.action == "workspace.reindexed"))
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.resource_type == "workspace"
        assert event.resource_id == str(workspace_a.id)

    @pytest.mark.asyncio
    async def test_chat_turn_audit_includes_workspace_id(self, db, tenant_a, admin_user, workspace_a):
        """chat.turn audit event includes workspace_id when session is workspace-scoped."""
        user, _ = admin_user

        session = ChatSession(
            tenant_id=tenant_a.id,
            user_id=user.id,
            workspace_id=workspace_a.id,
        )
        db.add(session)
        await db.flush()
        await db.refresh(session, ["messages"])

        mock_response = MagicMock()
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=20)
        mock_response.tool_use_blocks = []
        mock_response.text_blocks = ["Response"]

        async def _fake_stream(**kwargs):
            for text in mock_response.text_blocks:
                yield "text", text
            yield "response", mock_response

        mock_adapter = AsyncMock()
        mock_adapter.create_message = AsyncMock(return_value=mock_response)
        mock_adapter.stream_message = _fake_stream

        with (
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.get_tenant_ai_config", return_value=_FAKE_AI_CONFIG),
            patch(f"{_ORCH}.get_active_template", return_value="You are helpful."),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(f"{_ORCH}.deduct_chat_credits", new_callable=AsyncMock, return_value=None),
        ):
            from app.services.chat.orchestrator import run_chat_turn

            async for _ in run_chat_turn(
                db=db,
                session=session,
                user_message="Hello",
                user_id=user.id,
                tenant_id=tenant_a.id,
            ):
                pass

        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "chat.turn",
                AuditEvent.resource_id == str(session.id),
            )
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.payload["workspace_id"] == str(workspace_a.id)


# ============================================================
# 6. TestOrchestratorToolTiming
# ============================================================


class TestOrchestratorToolTiming:
    @pytest.mark.asyncio
    async def test_duration_ms_in_tool_calls_log(self, db, tenant_a, admin_user, workspace_a):
        """Tool calls log entries include a positive duration_ms field."""
        user, _ = admin_user

        session = ChatSession(
            tenant_id=tenant_a.id,
            user_id=user.id,
            workspace_id=workspace_a.id,
        )
        db.add(session)
        await db.flush()
        await db.refresh(session, ["messages"])

        # Mock adapter: first call returns a tool_use, second returns text
        tool_block = MagicMock()
        tool_block.id = "tool_timing"
        tool_block.name = "workspace_list_files"
        tool_block.input = {}

        response_with_tool = MagicMock()
        response_with_tool.usage = MagicMock(input_tokens=10, output_tokens=20)
        response_with_tool.tool_use_blocks = [tool_block]
        response_with_tool.text_blocks = []

        response_text = MagicMock()
        response_text.usage = MagicMock(input_tokens=10, output_tokens=20)
        response_text.tool_use_blocks = []
        response_text.text_blocks = ["Done."]

        mock_adapter = AsyncMock()
        mock_adapter.create_message = AsyncMock(side_effect=[response_with_tool, response_text])
        mock_adapter.stream_message = _make_stream_side_effect([response_with_tool, response_text])
        mock_adapter.build_assistant_message = MagicMock(return_value={"role": "assistant", "content": "test"})
        mock_adapter.build_tool_result_message = MagicMock(return_value={"role": "user", "content": "result"})

        with (
            patch(f"{_ORCH}.get_adapter", return_value=mock_adapter),
            patch(f"{_ORCH}.get_tenant_ai_config", return_value=_FAKE_AI_CONFIG),
            patch(f"{_ORCH}.get_active_template", return_value="You are helpful."),
            patch(f"{_ORCH}.retriever_node", new_callable=AsyncMock),
            patch(f"{_ORCH}.build_all_tool_definitions", return_value=[]),
            patch(f"{_ORCH}.execute_tool_call", new_callable=AsyncMock, return_value='{"files": []}'),
            patch("app.services.policy_service.get_active_policy", new_callable=AsyncMock, return_value=None),
            patch(f"{_ORCH}.deduct_chat_credits", new_callable=AsyncMock, return_value=None),
        ):
            from app.services.chat.orchestrator import run_chat_turn

            result = await _collect_stream_result(
                run_chat_turn(
                    db=db,
                    session=session,
                    user_message="List files",
                    user_id=user.id,
                    tenant_id=tenant_a.id,
                )
            )

        # Verify tool_calls in the saved message contain duration_ms
        assert result["tool_calls"] is not None
        assert len(result["tool_calls"]) >= 1
        for tc in result["tool_calls"]:
            assert "duration_ms" in tc
            assert isinstance(tc["duration_ms"], int)
            assert tc["duration_ms"] >= 0
