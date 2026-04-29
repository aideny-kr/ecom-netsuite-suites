"""Codex P1 regression: clarify gate must be robust to _setup_context rebuild.

The bug: ``UnifiedAgent._setup_context`` calls
``build_all_tool_definitions(db, tenant_id)`` WITHOUT passing
``plan_mode_enabled=True`` (line ~726). On the financial-ambiguous turn the
orchestrator passes ``plan_mode_clarify_only=True`` to ``run`` /
``run_streaming``. Both methods then run a filter:

    self._tool_defs = [t for t in (self._tool_defs or []) if t.get("name") == "clarify"]

If ``_setup_context`` produced a list without ``clarify`` (the common case —
since the orchestrator's ``build_all_tool_definitions`` call elsewhere is the
only one that registers clarify), this filter yields ``[]``. The provider then
gets ``tool_choice=clarify`` with NO clarify schema in the inventory →
silent gate failure, the LLM either errors or quietly skips the gate.

Fix: when ``plan_mode_clarify_only=True``, inject the canonical clarify schema
unconditionally, regardless of what ``_setup_context`` left behind.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_unified_agent():
    """Construct a UnifiedAgent with no metadata/policy."""
    from app.services.chat.agents.unified_agent import UnifiedAgent

    return UnifiedAgent(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        correlation_id="corr-1",
    )


@pytest.mark.asyncio
async def test_run_injects_clarify_when_setup_drops_it():
    """``run()`` with plan_mode_clarify_only=True must produce a clarify-only
    inventory even when ``_setup_context`` populates ``_tool_defs`` without it.

    Reproduces the codex P1 finding — ``_setup_context`` calls
    ``build_all_tool_definitions(db, tenant_id)`` (no plan_mode_enabled flag),
    so the rebuilt list lacks the ``clarify`` schema. The filter then yields
    ``[]`` → silent gate failure.
    """
    agent = _make_unified_agent()

    captured_tool_defs: dict[str, Any] = {}

    async def _fake_setup_context(self_inner: Any, task: str, context: dict, db: Any) -> str:
        # Simulate the bug: build_all_tool_definitions returns connector tools
        # but no clarify (because plan_mode_enabled=False is the default).
        self_inner._tool_defs = [
            {"name": "bigquery_sql", "description": "..."},
            {"name": "netsuite_suiteql", "description": "..."},
        ]
        return task

    async def _fake_super_run(*args: Any, **kwargs: Any) -> Any:
        captured_tool_defs["after"] = list(agent._tool_defs or [])
        return MagicMock()  # AgentResult-shaped sentinel

    with patch(
        "app.services.chat.agents.unified_agent.UnifiedAgent._setup_context",
        new=_fake_setup_context,
    ):
        with patch(
            "app.services.chat.agents.base_agent.BaseSpecialistAgent.run",
            new=AsyncMock(side_effect=_fake_super_run),
        ):
            await agent.run(
                task="What's our revenue this quarter?",
                context={},
                db=AsyncMock(),
                adapter=MagicMock(),
                model="claude-sonnet-4-6",
                plan_mode_clarify_only=True,
            )

    # After the filter, the agent's tool inventory MUST contain exactly one
    # tool: the clarify schema. The bug used to leave it empty.
    after = captured_tool_defs["after"]
    assert len(after) == 1, f"expected exactly one tool (clarify); got {len(after)}: {after}"
    assert after[0]["name"] == "clarify", f"expected clarify; got {after[0].get('name')!r}"

    # Schema must be valid (the canonical one, not a stub) — has input_schema.
    assert "input_schema" in after[0], "clarify schema is missing input_schema (not the canonical tool)"


@pytest.mark.asyncio
async def test_run_streaming_injects_clarify_when_setup_drops_it():
    """Same fix in ``run_streaming()``. Codex flagged BOTH paths."""
    agent = _make_unified_agent()

    captured_tool_defs: dict[str, Any] = {}

    async def _fake_setup_context(self_inner: Any, task: str, context: dict, db: Any) -> str:
        self_inner._tool_defs = [
            {"name": "bigquery_sql", "description": "..."},
            {"name": "netsuite_suiteql", "description": "..."},
        ]
        return task

    async def _fake_super_run_streaming(*args: Any, **kwargs: Any):
        captured_tool_defs["after"] = list(agent._tool_defs or [])
        if False:
            yield  # make this an async generator

    with patch(
        "app.services.chat.agents.unified_agent.UnifiedAgent._setup_context",
        new=_fake_setup_context,
    ):
        with patch(
            "app.services.chat.agents.base_agent.BaseSpecialistAgent.run_streaming",
            new=_fake_super_run_streaming,
        ):
            async for _ in agent.run_streaming(
                task="What's our revenue this quarter?",
                context={},
                db=AsyncMock(),
                adapter=MagicMock(),
                model="claude-sonnet-4-6",
                plan_mode_clarify_only=True,
            ):
                pass

    after = captured_tool_defs["after"]
    assert len(after) == 1, f"expected exactly one tool (clarify); got {len(after)}: {after}"
    assert after[0]["name"] == "clarify", f"expected clarify; got {after[0].get('name')!r}"
    assert "input_schema" in after[0], "clarify schema missing input_schema"


@pytest.mark.asyncio
async def test_run_injects_clarify_when_setup_returns_empty_list():
    """Edge case: ``_setup_context`` leaves ``_tool_defs`` empty (e.g. tool
    discovery threw). The gate must STILL produce a clarify-only inventory.
    """
    agent = _make_unified_agent()

    captured: dict[str, Any] = {}

    async def _fake_setup_context(self_inner: Any, task: str, context: dict, db: Any) -> str:
        self_inner._tool_defs = []  # empty — pretend tool discovery failed
        return task

    async def _fake_super_run(*args: Any, **kwargs: Any) -> Any:
        captured["after"] = list(agent._tool_defs or [])
        return MagicMock()

    with patch(
        "app.services.chat.agents.unified_agent.UnifiedAgent._setup_context",
        new=_fake_setup_context,
    ):
        with patch(
            "app.services.chat.agents.base_agent.BaseSpecialistAgent.run",
            new=AsyncMock(side_effect=_fake_super_run),
        ):
            await agent.run(
                task="What's our revenue?",
                context={},
                db=AsyncMock(),
                adapter=MagicMock(),
                model="claude-sonnet-4-6",
                plan_mode_clarify_only=True,
            )

    after = captured["after"]
    assert len(after) == 1
    assert after[0]["name"] == "clarify"
