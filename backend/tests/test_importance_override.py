"""Tests for the importance tier override endpoint (PATCH /messages/{id}/importance)."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.chat import UpdateMessageImportance, update_message_importance
from app.models.chat import ChatMessage
from app.models.user import User


def _make_user(tenant_id: uuid.UUID | None = None) -> MagicMock:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.tenant_id = tenant_id or uuid.uuid4()
    return user


def _make_message(tenant_id: uuid.UUID, importance: int = 2) -> MagicMock:
    msg = MagicMock(spec=ChatMessage)
    msg.id = uuid.uuid4()
    msg.tenant_id = tenant_id
    msg.query_importance = importance
    return msg


# --- Test 1: Schema validates tier range (1-4) ---


class TestUpdateMessageImportanceSchema:
    def test_valid_tiers(self):
        for t in [1, 2, 3, 4]:
            schema = UpdateMessageImportance(query_importance=t)
            assert schema.query_importance == t

    def test_tier_zero_rejected(self):
        with pytest.raises(ValidationError):
            UpdateMessageImportance(query_importance=0)

    def test_tier_five_rejected(self):
        with pytest.raises(ValidationError):
            UpdateMessageImportance(query_importance=5)

    def test_tier_negative_rejected(self):
        with pytest.raises(ValidationError):
            UpdateMessageImportance(query_importance=-1)


# --- Test 2: Non-existent message returns 404 ---


@pytest.mark.asyncio
async def test_nonexistent_message_returns_404():
    user = _make_user()
    db = AsyncMock()

    # Simulate no result from DB
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute.return_value = result_mock

    body = UpdateMessageImportance(query_importance=3)

    with pytest.raises(HTTPException) as exc_info:
        await update_message_importance(
            message_id=uuid.uuid4(),
            body=body,
            user=user,
            db=db,
        )
    assert exc_info.value.status_code == 404


# --- Test 3: Successful update changes query_importance ---


@pytest.mark.asyncio
async def test_successful_importance_update():
    user = _make_user()
    msg = _make_message(tenant_id=user.tenant_id, importance=2)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = msg
    db.execute.return_value = result_mock

    body = UpdateMessageImportance(query_importance=4)

    with patch("app.api.v1.chat.audit_service") as mock_audit:
        mock_audit.log_event = AsyncMock()
        response = await update_message_importance(
            message_id=msg.id,
            body=body,
            user=user,
            db=db,
        )

    assert msg.query_importance == 4
    assert response["query_importance"] == 4
    db.commit.assert_awaited_once()


# --- Test 4: Audit event is logged with correct action ---


@pytest.mark.asyncio
async def test_audit_event_logged():
    user = _make_user()
    msg = _make_message(tenant_id=user.tenant_id, importance=1)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = msg
    db.execute.return_value = result_mock

    body = UpdateMessageImportance(query_importance=3)

    with patch("app.api.v1.chat.audit_service") as mock_audit:
        mock_audit.log_event = AsyncMock()
        await update_message_importance(
            message_id=msg.id,
            body=body,
            user=user,
            db=db,
        )

        mock_audit.log_event.assert_awaited_once()
        call_kwargs = mock_audit.log_event.call_args.kwargs
        assert call_kwargs["action"] == "chat.importance_override"
        assert call_kwargs["category"] == "chat"
        assert call_kwargs["resource_type"] == "chat_message"
        assert call_kwargs["payload"]["old_tier"] == 1
        assert call_kwargs["payload"]["new_tier"] == 3


# --- Test 5: Endpoint uses chat_api.manage permission (not settings.manage) ---


def test_endpoint_uses_chat_api_manage_permission():
    """Verify the endpoint dependency uses chat_api.manage, not settings.manage."""
    import inspect

    source = inspect.getsource(update_message_importance)
    assert "chat_api.manage" in source, (
        "Endpoint should use require_permission('chat_api.manage'), "
        f"but the source contains: {source}"
    )
    assert "settings.manage" not in source, (
        "Endpoint should NOT use require_permission('settings.manage')"
    )


# --- Test 6: Response shape for optimistic cache update ---


@pytest.mark.asyncio
async def test_response_includes_id_and_new_tier():
    """Response must include 'id' and 'query_importance' so frontend can do optimistic update."""
    user = _make_user()
    msg = _make_message(tenant_id=user.tenant_id, importance=2)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = msg
    db.execute.return_value = result_mock

    body = UpdateMessageImportance(query_importance=3)

    with patch("app.api.v1.chat.audit_service") as mock_audit:
        mock_audit.log_event = AsyncMock()
        response = await update_message_importance(
            message_id=msg.id,
            body=body,
            user=user,
            db=db,
        )

    assert "id" in response
    assert "query_importance" in response
    assert response["id"] == str(msg.id)
    assert response["query_importance"] == 3


# --- Test 7: Same tier is idempotent (still commits) ---


@pytest.mark.asyncio
async def test_same_tier_still_commits():
    """Even if tier doesn't change, endpoint should succeed (idempotent)."""
    user = _make_user()
    msg = _make_message(tenant_id=user.tenant_id, importance=2)

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = msg
    db.execute.return_value = result_mock

    body = UpdateMessageImportance(query_importance=2)

    with patch("app.api.v1.chat.audit_service") as mock_audit:
        mock_audit.log_event = AsyncMock()
        response = await update_message_importance(
            message_id=msg.id,
            body=body,
            user=user,
            db=db,
        )

    assert response["query_importance"] == 2
    db.commit.assert_awaited_once()
