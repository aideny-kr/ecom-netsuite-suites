import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated

import anthropic
import openai
from fastapi import APIRouter, Depends, Header, HTTPException, Query as FastAPIQuery, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user, require_feature, require_permission
from app.models.chat import ChatMessage, ChatSession
from app.models.user import User
from app.services import audit_service
from app.services.chat.orchestrator import run_chat_turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# --- Schemas ---


class CreateSessionRequest(BaseModel):
    title: str | None = None
    workspace_id: str | None = None


class UpdateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class SendMessageRequest(BaseModel):
    content: str = Field(..., max_length=4000)


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    tool_calls: list | None = None
    citations: list | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model_used: str | None = None
    provider_used: str | None = None
    is_byok: bool | None = None
    confidence_score: float | None = None
    query_importance: int | None = None
    user_feedback: str | None = None
    structured_output: dict | None = None
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
    if msg.confidence_score is not None:
        result["confidence_score"] = float(msg.confidence_score)
    if msg.query_importance is not None:
        result["query_importance"] = msg.query_importance
    if msg.user_feedback is not None:
        result["user_feedback"] = msg.user_feedback
    if msg.structured_output is not None:
        result["structured_output"] = msg.structured_output
    return result


# --- Endpoints ---


@router.post("/sessions", status_code=status.HTTP_201_CREATED, response_model=SessionListItem)
async def create_session(
    body: CreateSessionRequest,
    user: Annotated[User, Depends(require_feature("chat"))],
    db: Annotated[AsyncSession, Depends(get_db)],
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

    q = q.order_by(ChatSession.updated_at.desc()).limit(50)
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
    user: Annotated[User, Depends(require_feature("chat"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    wizard_step: str | None = None,
    x_timezone: str | None = Header(None),
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

    # Save user message *before* calling the pipeline so it persists on errors.
    # Explicitly set created_at in Python (not server_default) to guarantee the
    # user message timestamp is strictly before the assistant message, preventing
    # ordering issues when both land in the same DB transaction.
    user_msg = ChatMessage(
        tenant_id=user.tenant_id,
        session_id=session.id,
        role="user",
        content=body.content,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user_msg)
    await db.flush()

    async def stream_generator():
        import asyncio

        _SENTINEL = object()
        partial_text_parts: list[str] = []
        stream_completed = False
        queue: asyncio.Queue = asyncio.Queue()

        async def _producer():
            """Run the chat pipeline and put chunks into the queue."""
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
                    await queue.put(chunk)
            except ValueError as exc:
                await db.commit()
                logger.warning("Chat configuration error: %s", exc)
                await queue.put({"type": "error", "error": "Chat service is not configured. Contact your administrator."})
            except (anthropic.AuthenticationError, openai.AuthenticationError):
                await db.commit()
                logger.warning("AI provider API key is invalid")
                await queue.put({"type": "error", "error": "Chat API key is invalid."})
            except (anthropic.APIError, openai.APIError, Exception) as exc:
                await db.commit()
                logger.exception("Chat pipeline error: %s", exc)
                await queue.put({"type": "error", "error": "Chat service temporarily unavailable. Please try again."})
            finally:
                await queue.put(_SENTINEL)

        producer_task = asyncio.create_task(_producer())

        # Send padding to force Cloudflare Tunnel to start streaming
        yield f": {' ' * 8192}\n\n"
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue

                if chunk is _SENTINEL:
                    stream_completed = True
                    break

                if isinstance(chunk, dict) and chunk.get("type") == "text":
                    partial_text_parts.append(chunk.get("content", ""))

                yield f"data: {json.dumps(chunk)}\n\n"
        finally:
            producer_task.cancel()
            # Save partial assistant message if stream was interrupted
            if not stream_completed and partial_text_parts:
                try:
                    partial_content = "".join(partial_text_parts).strip()
                    if partial_content and len(partial_content) > 10:
                        partial_msg = ChatMessage(
                            tenant_id=user.tenant_id,
                            session_id=session.id,
                            role="assistant",
                            content=partial_content + "\n\n*(Response interrupted)*",
                            created_at=datetime.now(timezone.utc),
                        )
                        db.add(partial_msg)
                        await db.commit()
                        logger.info("Saved partial assistant message on disconnect (%d chars)", len(partial_content))
                except Exception:
                    logger.warning("Failed to save partial message on disconnect", exc_info=True)

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx/proxy buffering
        },
    )


@router.patch("/sessions/{session_id}", response_model=SessionListItem)
async def update_session(
    session_id: uuid.UUID,
    body: UpdateSessionRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update a chat session's title."""
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

    if body.title is not None:
        session.title = body.title

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="chat",
        action="chat.session_update",
        actor_id=user.id,
        resource_type="chat_session",
        resource_id=str(session_id),
    )
    await db.commit()
    await db.refresh(session)
    return _serialize_session(session)


class UpdateMessageImportance(BaseModel):
    query_importance: int = Field(ge=1, le=4)


@router.patch("/messages/{message_id}/importance")
async def update_message_importance(
    message_id: uuid.UUID,
    body: UpdateMessageImportance,
    user: Annotated[User, Depends(require_permission("chat_api.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin-only: override the auto-classified importance tier on a message."""
    result = await db.execute(
        select(ChatMessage).where(
            ChatMessage.id == message_id,
            ChatMessage.tenant_id == user.tenant_id,
        )
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    old_tier = msg.query_importance
    msg.query_importance = body.query_importance

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="chat",
        action="chat.importance_override",
        actor_id=user.id,
        resource_type="chat_message",
        resource_id=str(message_id),
        payload={"old_tier": old_tier, "new_tier": body.query_importance},
    )
    await db.commit()
    return {"id": str(msg.id), "query_importance": msg.query_importance}


@router.patch("/messages/{message_id}/feedback")
async def set_message_feedback(
    message_id: uuid.UUID,
    feedback: str = FastAPIQuery(..., pattern=r"^(helpful|not_helpful)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set user feedback (helpful/not_helpful) on an assistant message."""
    result = await db.execute(
        select(ChatMessage).where(
            ChatMessage.id == message_id,
            ChatMessage.tenant_id == user.tenant_id,
        )
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    old_feedback = msg.user_feedback
    msg.user_feedback = feedback

    from app.services.query_pattern_service import process_feedback

    await process_feedback(
        db=db,
        tenant_id=user.tenant_id,
        message=msg,
        feedback=feedback,
    )

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="chat",
        action="chat.feedback",
        actor_id=user.id,
        resource_type="chat_message",
        resource_id=str(message_id),
        payload={"feedback": feedback, "old_feedback": old_feedback},
    )
    await db.commit()

    return {"id": str(msg.id), "feedback": feedback}


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a chat session and its messages."""
    from sqlalchemy import delete

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

    # Delete messages first (FK constraint)
    await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
    await db.delete(session)

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="chat",
        action="chat.session_delete",
        actor_id=user.id,
        resource_type="chat_session",
        resource_id=str(session_id),
    )
    await db.commit()


@router.get("/health")
async def chat_health():
    """Check chat service configuration (no auth required)."""
    return {
        "anthropic_configured": bool(settings.ANTHROPIC_API_KEY),
        "voyage_configured": bool(settings.VOYAGE_API_KEY),
        "model": settings.ANTHROPIC_MODEL,
    }
