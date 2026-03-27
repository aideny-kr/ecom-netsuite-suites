"""Agent schemas for API responses."""

from pydantic import BaseModel


class AgentSummary(BaseModel):
    agent_id: str
    display_name: str
    description: str
