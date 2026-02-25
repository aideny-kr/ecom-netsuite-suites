import json
import logging
import uuid

import anthropic
import openai
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.chat import ChatMessage, ChatSession
from app.models.user import User
from app.services.chat.orchestrator import run_chat_turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# --- Schemas ---


class CreateSessionRequest(BaseModel):
    title: str | None = None
    workspace_id: str | None = None


class SendMessageRequest(BaseModel):
    content: str = Field(..., max_length=4000)


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    tool_calls: list | None = None
    citations: list | None = None
    created_at: str

    model_config = {"from_attributes": True}


class SessionListItem(BaseModel):
    id: str
    title: str | None = None
    workspace_id: str | None = None
    session_type: str = "chat"
    is_archived: bool
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class SessionDetailResponse(BaseModel):
    id: str
    title: str | None = None
    is_archived: bool
    messages: list[MessageResponse]
    created_at: str
    updated_at: str


# --- Helpers ---


def _serialize_session(session: ChatSession) -> dict:
    return {
        "id": str(session.id),
        "title": session.title,
        "workspace_id": str(session.workspace_id) if session.workspace_id else None,
        "session_type": session.session_type or "chat",
        "is_archived": session.is_archived,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }


def _serialize_message(msg: ChatMessage) -> dict:
    result = {
        "id": str(msg.id),
        "role": msg.role,
        "content": msg.content,
        "tool_calls": msg.tool_calls,
        "citations": msg.citations,
        "created_at": msg.created_at.isoformat(),
    }
    if msg.input_tokens is not None:
        result["input_tokens"] = msg.input_tokens
    if msg.output_tokens is not None:
        result["output_tokens"] = msg.output_tokens
    if msg.model_used:
        result["model_used"] = msg.model_used
    if msg.provider_used:
        result["provider_used"] = msg.provider_used
    if msg.is_byok is not None:
        result["is_byok"] = msg.is_byok
    return result


# --- Endpoints ---


@router.post("/sessions", status_code=status.HTTP_201_CREATED, response_model=SessionListItem)
async def create_session(
    body: CreateSessionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ws_id = uuid.UUID(body.workspace_id) if body.workspace_id else None
    session = ChatSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        title=body.title,
        workspace_id=ws_id,
        session_type="workspace" if ws_id else "chat",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return _serialize_session(session)


@router.get("/sessions", response_model=list[SessionListItem])
async def list_sessions(
    workspace_id: str | None = None,
    session_type: str | None = None,
    include_all: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(ChatSession).where(ChatSession.tenant_id == user.tenant_id, ChatSession.user_id == user.id)
    if include_all:
        pass  # No workspace_id filter — return everything
    elif workspace_id:
        try:
            ws_uuid = uuid.UUID(workspace_id)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid workspace_id")
        q = q.where(ChatSession.workspace_id == ws_uuid)
    else:
        # Default: general chats only (exclude workspace and onboarding sessions)
        q = q.where(ChatSession.workspace_id.is_(None))
        q = q.where(ChatSession.session_type == "chat")

    # Optional session_type filter (e.g., ?session_type=onboarding)
    if session_type:
        q = q.where(ChatSession.session_type == session_type)

    q = q.order_by(ChatSession.created_at.desc()).limit(50)
    result = await db.execute(q)
    sessions = result.scalars().all()
    return [_serialize_session(s) for s in sessions]


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.tenant_id == user.tenant_id,
            ChatSession.user_id == user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return {
        **_serialize_session(session),
        "messages": [_serialize_message(m) for m in session.messages],
    }


@router.post(
    "/sessions/{session_id}/messages",
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    session_id: uuid.UUID,
    body: SendMessageRequest,
    wizard_step: str | None = None,
    x_timezone: str | None = Header(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.tenant_id == user.tenant_id,
            ChatSession.user_id == user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Save user message *before* calling the pipeline so it persists on errors
    user_msg = ChatMessage(
        tenant_id=user.tenant_id,
        session_id=session.id,
        role="user",
        content=body.content,
    )
    db.add(user_msg)
    await db.flush()

    async def stream_generator():
        try:
            async for chunk in run_chat_turn(
                db=db,
                session=session,
                user_message=body.content,
                user_id=user.id,
                tenant_id=user.tenant_id,
                user_msg=user_msg,
                wizard_step=wizard_step,
                user_timezone=x_timezone,
            ):
                yield f"data: {json.dumps(chunk)}\n\n"
        except ValueError as exc:
            await db.commit()  # persist user message
            logger.warning("Chat configuration error: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'error': 'Chat service is not configured. Contact your administrator.'})}\n\n"
        except (anthropic.AuthenticationError, openai.AuthenticationError):
            await db.commit()
            logger.warning("AI provider API key is invalid")
            yield f"data: {json.dumps({'type': 'error', 'error': 'Chat API key is invalid.'})}\n\n"
        except (anthropic.APIError, openai.APIError, Exception) as exc:
            await db.commit()
            logger.exception("Chat pipeline error: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'error': 'Chat service temporarily unavailable. Please try again.'})}\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx/proxy buffering
        },
    )


@router.get("/health")
async def chat_health():
    """Check chat service configuration (no auth required)."""
    return {
        "anthropic_configured": bool(settings.ANTHROPIC_API_KEY),
        "voyage_configured": bool(settings.VOYAGE_API_KEY),
        "model": settings.ANTHROPIC_MODEL,
    }
