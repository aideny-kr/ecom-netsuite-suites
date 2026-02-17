import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.chat import ChatMessage, ChatSession
from app.models.user import User
from app.services.chat.orchestrator import run_chat_turn

router = APIRouter(prefix="/chat", tags=["chat"])


# --- Schemas ---

class CreateSessionRequest(BaseModel):
    title: str | None = None


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
        "is_archived": session.is_archived,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }


def _serialize_message(msg: ChatMessage) -> dict:
    return {
        "id": str(msg.id),
        "role": msg.role,
        "content": msg.content,
        "tool_calls": msg.tool_calls,
        "citations": msg.citations,
        "created_at": msg.created_at.isoformat(),
    }


# --- Endpoints ---

@router.post("/sessions", status_code=status.HTTP_201_CREATED, response_model=SessionListItem)
async def create_session(
    body: CreateSessionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = ChatSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        title=body.title,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return _serialize_session(session)


@router.get("/sessions", response_model=list[SessionListItem])
async def list_sessions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.tenant_id == user.tenant_id, ChatSession.user_id == user.id)
        .order_by(ChatSession.created_at.desc())
        .limit(50)
    )
    sessions = result.scalars().all()
    return [_serialize_session(s) for s in sessions]


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession)
        .where(
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
    response_model=MessageResponse,
)
async def send_message(
    session_id: uuid.UUID,
    body: SendMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.id == session_id,
            ChatSession.tenant_id == user.tenant_id,
            ChatSession.user_id == user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    assistant_msg = await run_chat_turn(
        db=db,
        session=session,
        user_message=body.content,
        user_id=user.id,
        tenant_id=user.tenant_id,
    )
    return _serialize_message(assistant_msg)
