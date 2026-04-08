"""POST /disclosure-events — frontend reports UI interaction with disclosure footers."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.chat import ChatSession
from app.models.user import User
from app.services.chat.disclosure import disclosure_enabled_for_tenant, log_disclosure_event

router = APIRouter(prefix="/disclosure-events", tags=["disclosure"])


class DisclosureExpandedRequest(BaseModel):
    session_id: UUID
    message_id: UUID


@router.post("/expanded")
async def report_expanded(
    body: DisclosureExpandedRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    if not await disclosure_enabled_for_tenant(db, user.tenant_id):
        return {"status": "skipped"}

    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == body.session_id,
            ChatSession.tenant_id == user.tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    await log_disclosure_event(
        db,
        tenant_id=user.tenant_id,
        session_id=body.session_id,
        message_id=body.message_id,
        event_type="expanded",
    )
    return {"status": "ok"}
