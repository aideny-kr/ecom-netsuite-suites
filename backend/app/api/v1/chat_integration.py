"""External Chat Integration API — API key authenticated."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_auth import ApiKeyContext, get_api_key_context
from app.core.database import get_db
from app.models.chat import ChatMessage, ChatSession
from app.services.audit_service import log_event
from app.services.chat.orchestrator import run_chat_turn

router = APIRouter(prefix="/integration/chat", tags=["chat-integration"])


class IntegrationChatRequest(BaseModel):
    message: str = Field(..., max_length=4000)
    session_id: str | None = None


class IntegrationMessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    tool_calls: list | None = None
    citations: list | None = None
    created_at: str

    model_config = {"from_attributes": True}


class IntegrationSessionResponse(BaseModel):
    id: str
    title: str | None = None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


def _serialize_message(msg: ChatMessage) -> dict:
    result = {
        "id": str(msg.id),
        "session_id": str(msg.session_id),
        "role": msg.role,
        "content": msg.content,
        "tool_calls": msg.tool_calls,
        "citations": msg.citations,
        "created_at": msg.created_at.isoformat(),
    }
    return result


@router.post("", status_code=status.HTTP_201_CREATED, response_model=IntegrationMessageResponse)
async def integration_chat(
    body: IntegrationChatRequest,
    ctx: ApiKeyContext = Depends(get_api_key_context),
    db: AsyncSession = Depends(get_db),
):
    """Send a chat message via API key auth."""
    if "chat" not in ctx.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not have 'chat' scope",
        )

    # Get or create session
    session = None
    if body.session_id:
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.id == uuid.UUID(body.session_id),
                ChatSession.tenant_id == ctx.tenant_id,
            )
        )
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    else:
        session = ChatSession(
            tenant_id=ctx.tenant_id,
            user_id=None,
            title=None,
        )
        db.add(session)
        await db.flush()

    # Save user message
    user_msg = ChatMessage(
        tenant_id=ctx.tenant_id,
        session_id=session.id,
        role="user",
        content=body.message,
    )
    db.add(user_msg)
    await db.flush()

    try:
        assistant_msg = await run_chat_turn(
            db=db,
            session=session,
            user_message=body.message,
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),  # system actor for API key auth
            tenant_id=ctx.tenant_id,
            user_msg=user_msg,
        )
    except Exception:
        await db.commit()  # persist user message
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Chat service temporarily unavailable.",
        )

    await log_event(
        db=db,
        tenant_id=ctx.tenant_id,
        category="chat_api",
        action="chat_api.message_sent",
        resource_type="chat_session",
        resource_id=str(session.id),
        payload={"auth_method": "api_key"},
    )

    return _serialize_message(assistant_msg)


@router.get("/sessions", response_model=list[IntegrationSessionResponse])
async def list_integration_sessions(
    ctx: ApiKeyContext = Depends(get_api_key_context),
    db: AsyncSession = Depends(get_db),
):
    """List chat sessions created via API key."""
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.tenant_id == ctx.tenant_id)
        .order_by(ChatSession.created_at.desc())
        .limit(50)
    )
    sessions = result.scalars().all()
    return [
        {
            "id": str(s.id),
            "title": s.title,
            "created_at": s.created_at.isoformat(),
            "updated_at": s.updated_at.isoformat(),
        }
        for s in sessions
    ]


@router.get("/sessions/{session_id}/messages", response_model=list[IntegrationMessageResponse])
async def get_session_messages(
    session_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(get_api_key_context),
    db: AsyncSession = Depends(get_db),
):
    """Get messages for a specific session."""
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.tenant_id == ctx.tenant_id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    return [_serialize_message(m) for m in session.messages]
