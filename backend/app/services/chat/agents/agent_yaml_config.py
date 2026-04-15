"""YAML-loaded agent configuration.

Loaded from backend/app/services/chat/agents/configs/*.yaml at startup.
Tenant-specific overrides from agent_configs DB table are merged at runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class RoutingRule(BaseModel):
    """A single routing rule for tier-1 regex matching."""

    pattern: str
    priority: int = 0


class AgentYAMLConfig(BaseModel):
    """Agent configuration loaded from YAML."""

    agent_id: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=64)
    display_name: str = Field(max_length=128)
    description: str = Field(max_length=500)
    version: str = Field(default="1.0.0")

    # Routing
    routing_rules: list[RoutingRule] = Field(default_factory=list)
    semantic_examples: list[str] = Field(default_factory=list)

    # Tools & RAG
    tool_ids: list[str] = Field(default_factory=list)
    rag_partitions: list[str] = Field(default_factory=list)

    # Model
    model_preference: str | None = None
    max_steps: int = Field(default=6, ge=1, le=20)
    cost_budget: float | None = None

    # Prompt
    prompt_file: str | None = None

    # Behavior
    requires_confirmation: bool = False
    enabled_by_default: bool = True
    requires_connector: list[str] = Field(default_factory=list)

    @field_validator("requires_connector", mode="before")
    @classmethod
    def _normalize_requires_connector(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v

    @classmethod
    def from_yaml(cls, path: str | Path) -> AgentYAMLConfig:
        """Load agent config from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    def merge(self, overrides: dict[str, Any]) -> AgentYAMLConfig:
        """Return a new config with tenant-specific overrides applied."""
        base = self.model_dump()
        base.update({k: v for k, v in overrides.items() if v is not None})
        return AgentYAMLConfig(**base)
