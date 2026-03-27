"""Agent listing API — returns enabled specialized agents for the tenant."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.agent import AgentSummary

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[AgentSummary])
async def list_agents(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List enabled specialized agents for this tenant."""
    from app.services.chat.orchestrator import _agent_registry

    agents = await _agent_registry.get_enabled_agents(db, user.tenant_id)
    return [
        AgentSummary(
            agent_id=a.agent_id,
            display_name=a.display_name,
            description=a.description,
        )
        for a in agents
        if a.agent_id != "unified-agent"  # Exclude the default fallback
    ]
