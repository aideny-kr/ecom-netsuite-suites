"""AgentRegistry — lifecycle management of specialized agents.

Loads YAML configs at startup, merges per-tenant DB overrides at runtime,
instantiates SpecializedAgent instances, and provides health checking.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.agents.agent_yaml_config import AgentYAMLConfig
from app.services.chat.agents.specialized_agent import SpecializedAgent

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Central registry for specialized agent configs."""

    def __init__(self) -> None:
        self.configs: dict[str, AgentYAMLConfig] = {}

    def load_configs(self, config_dir: Path) -> None:
        """Load all YAML agent configs from a directory."""
        self.configs.clear()
        for path in sorted(config_dir.glob("*.yaml")):
            try:
                config = AgentYAMLConfig.from_yaml(path)
                self.configs[config.agent_id] = config
                logger.info("Loaded agent config: %s", config.agent_id)
            except Exception:
                logger.exception("Failed to load agent config: %s", path)

    async def get_enabled_agents(self, db: AsyncSession, tenant_id: uuid.UUID) -> list[AgentYAMLConfig]:
        """Return configs for agents enabled for this tenant.

        Merges YAML defaults with DB overrides from agent_configs table.
        Agents disabled in DB (is_enabled=False) are excluded. Agents whose
        requires_connector isn't satisfied by any active connector are excluded.
        """
        # Query DB for tenant-specific overrides
        try:
            result = await db.execute(
                text("SELECT agent_id, is_enabled, override_config FROM agent_configs WHERE tenant_id = :tid"),
                {"tid": str(tenant_id)},
            )
            db_rows = result.all()
        except Exception:
            logger.warning("agent_configs table not available, using YAML defaults")
            db_rows = []

        # Build override map
        overrides: dict[str, Any] = {}
        disabled: set[str] = set()
        for row in db_rows:
            agent_id = row.agent_id
            if not row.is_enabled:
                disabled.add(agent_id)
            if row.override_config:
                overrides[agent_id] = row.override_config

        # Resolve the set of active connectors for this tenant. Fail-open on error
        # so a transient DB hiccup doesn't silently hide agents.
        try:
            active_connectors = await _get_active_connectors(db, tenant_id)
            connector_filter_active = True
        except Exception:
            logger.warning(
                "Failed to fetch active connectors for tenant %s; skipping connector filter",
                tenant_id,
            )
            active_connectors = set()
            connector_filter_active = False

        # Merge and filter
        enabled: list[AgentYAMLConfig] = []
        for agent_id, config in self.configs.items():
            if agent_id in disabled:
                continue
            if connector_filter_active and config.requires_connector:
                if not any(c in active_connectors for c in config.requires_connector):
                    logger.info(
                        "Filtering out agent %s for tenant %s — no active connector matches %s",
                        agent_id,
                        tenant_id,
                        config.requires_connector,
                    )
                    continue
            if agent_id in overrides:
                config = config.merge(overrides[agent_id])
            enabled.append(config)

        return enabled

    def instantiate(
        self,
        agent_id: str,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        correlation_id: str,
        overrides: dict[str, Any] | None = None,
        knowledge: list[str] | None = None,
        user_instructions: str | None = None,
    ) -> SpecializedAgent:
        """Create a SpecializedAgent instance from a registered config.

        Raises KeyError if agent_id is not registered.
        """
        config = self.configs[agent_id]  # KeyError if not found

        if overrides:
            config = config.merge(overrides)

        # Load prompt text from file if configured
        prompt_text = ""
        if config.prompt_file:
            prompt_path = Path(__file__).parent / "prompts" / config.prompt_file
            if prompt_path.is_file():
                prompt_text = prompt_path.read_text()

        # Prepend user instructions (from Agent Hub) — takes priority over defaults
        if user_instructions:
            prompt_text = (
                "<user_instructions>\n"
                "The user has configured the following instructions for this agent. "
                "ALWAYS follow these instructions. They take priority over default behavior.\n\n"
                f"{user_instructions}\n"
                "</user_instructions>\n\n"
                f"{prompt_text}"
            )

        return SpecializedAgent(
            config=config,
            prompt_text=prompt_text,
            knowledge=knowledge or [],
            tenant_id=tenant_id,
            user_id=user_id,
            correlation_id=correlation_id,
        )

    def is_healthy(self, error_count: int, success_count: int) -> bool:
        """Check if an agent is healthy based on error rate.

        Circuit breaker trips at >5% error rate over recent calls.
        """
        total = error_count + success_count
        if total == 0:
            return True
        if error_count / total > 0.05:
            return False
        return True


async def _get_active_connectors(db: AsyncSession, tenant_id: uuid.UUID) -> set[str]:
    """Return the set of connector providers the tenant has usable today.

    Usable = is_enabled=True AND status='active' in mcp_connectors.
    Revoked / needs_reauth / error / expired connectors are excluded.
    """
    from sqlalchemy import select

    from app.models.mcp_connector import McpConnector

    result = await db.execute(
        select(McpConnector.provider).where(
            McpConnector.tenant_id == tenant_id,
            McpConnector.is_enabled == True,  # noqa: E712
            McpConnector.status == "active",
        )
    )
    return {row[0] for row in result.all()}
