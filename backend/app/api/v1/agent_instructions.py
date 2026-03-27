"""Agent instructions API — per-tenant custom instructions for specialized agents."""
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.agent_config import AgentConfig
from app.models.user import User
from app.services import audit_service

router = APIRouter(prefix="/agents", tags=["agents"])


class InstructionsResponse(BaseModel):
    agent_id: str
    instructions: str | None
    updated_at: datetime | None
    updated_by: str | None


class UpdateInstructionsRequest(BaseModel):
    instructions: str = Field(max_length=5000)


@router.get("/{agent_id}/instructions", response_model=InstructionsResponse)
async def get_instructions(
    agent_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(AgentConfig).where(
            AgentConfig.tenant_id == user.tenant_id,
            AgentConfig.agent_id == agent_id,
        )
    )
    config = result.scalar_one_or_none()
    return InstructionsResponse(
        agent_id=agent_id,
        instructions=config.user_instructions if config else None,
        updated_at=config.instructions_updated_at if config else None,
        updated_by=str(config.instructions_updated_by) if config and config.instructions_updated_by else None,
    )


@router.put("/{agent_id}/instructions", response_model=InstructionsResponse)
async def update_instructions(
    agent_id: str,
    request: UpdateInstructionsRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(AgentConfig).where(
            AgentConfig.tenant_id == user.tenant_id,
            AgentConfig.agent_id == agent_id,
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        config = AgentConfig(
            tenant_id=user.tenant_id,
            agent_id=agent_id,
        )
        db.add(config)

    config.user_instructions = request.instructions
    config.instructions_updated_at = datetime.now(timezone.utc)
    config.instructions_updated_by = user.id

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="agent",
        action="agent.instructions_update",
        actor_id=user.id,
        resource_type="agent_config",
        resource_id=agent_id,
    )
    await db.commit()
    await db.refresh(config)
    return InstructionsResponse(
        agent_id=agent_id,
        instructions=config.user_instructions,
        updated_at=config.instructions_updated_at,
        updated_by=str(config.instructions_updated_by) if config.instructions_updated_by else None,
    )
