"""Protocol definition for specialized agents.

All specialized agents must satisfy this protocol. The protocol defines
the contract between the agent registry, router, and individual agents.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentProtocol(Protocol):
    """Contract that all specialized agents must satisfy."""

    @property
    def agent_id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def routing_rules(self) -> list[str]: ...

    @property
    def tool_ids(self) -> list[str]: ...

    @property
    def rag_partitions(self) -> list[str]: ...

    @property
    def system_prompt(self) -> str: ...

    @property
    def max_steps(self) -> int: ...

    @property
    def model_preference(self) -> str | None: ...

    @property
    def cost_budget(self) -> float | None: ...

    @property
    def requires_confirmation(self) -> bool: ...

    # Lifecycle hooks
    async def pre_execute(self, task: str, context: dict[str, Any]) -> dict[str, Any]: ...

    async def post_tool(self, tool_name: str, tool_input: dict, tool_result: str) -> str: ...

    async def pre_response(self, response_text: str) -> str: ...

    async def on_error(self, error: Exception, context: dict[str, Any]) -> str | None: ...
