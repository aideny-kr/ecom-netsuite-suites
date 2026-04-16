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
    """List agents — returns empty since v2.0 uses a single unified agent."""
    return []
