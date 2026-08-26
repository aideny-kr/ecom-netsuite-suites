"""The rate-limit check must not block the event loop.

`app.core.rate_limit` uses the SYNCHRONOUS redis client, and Redis here is remote
(Upstash), so the round-trip is milliseconds rather than microseconds. Calling it
directly from an `async def` handler stalls that worker's entire event loop, not
just the caller -- and `governed_execute` runs on every single MCP tool call, with
uvicorn configured for 4 workers (backend/Dockerfile).

Before the shared-window change this was free: the limiter was an in-memory dict.
Making it a network call without moving it off the loop turned a microsecond
lookup into a loop-blocking RTT on the hottest path we have. These tests pin the
fix (the repo's existing `asyncio.to_thread` idiom, cf. api/v1/chat_runs.py).
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.mcp import governance

# How long the faked limiter stalls, and how long we let the loop tick alongside it.
STALL_SECONDS = 0.3
TICK_SECONDS = 0.02


@pytest.mark.asyncio
async def test_governed_execute_does_not_block_the_event_loop(monkeypatch):
    """A slow limiter must not stop other coroutines from running.

    Goes red against a direct synchronous call: the ticker cannot advance while
    the loop is blocked, so it records ~1 tick instead of ~15.
    """

    def slow_limiter(tenant_id, tool_name, limit):
        time.sleep(STALL_SECONDS)  # stands in for a remote Redis RTT
        return True

    monkeypatch.setattr(governance, "check_mcp_tool_limit", slow_limiter)

    ticks = 0
    # The deadline is fixed BEFORE the blocking call starts. Computing it inside
    # the ticker made this test worthless: a blocked loop simply delayed the
    # ticker's start, and it then ticked freely in its own fresh window.
    deadline = time.monotonic() + STALL_SECONDS

    async def ticker():
        nonlocal ticks
        while time.monotonic() < deadline:
            await asyncio.sleep(TICK_SECONDS)
            ticks += 1

    async def execute_fn(*args, **kwargs):
        return {"ok": True}

    await asyncio.gather(
        governance.governed_execute(
            tool_name="netsuite.suiteql",
            params={},
            tenant_id=str(uuid.uuid4()),
            actor_id=None,
            execute_fn=execute_fn,
            db=None,
        ),
        ticker(),
    )

    # A free loop manages ~STALL/TICK ticks. A blocked one manages at most 1.
    assert ticks > 5, f"event loop was blocked during the rate-limit check (only {ticks} ticks)"


@pytest.mark.asyncio
async def test_governed_execute_still_denies_when_limiter_says_no(monkeypatch):
    """Moving the call off the loop must not lose the deny result."""

    def deny(tenant_id, tool_name, limit):
        return False

    monkeypatch.setattr(governance, "check_mcp_tool_limit", deny)

    called = False

    async def execute_fn(*args, **kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    result = await governance.governed_execute(
        tool_name="netsuite.suiteql",
        params={},
        tenant_id=str(uuid.uuid4()),
        actor_id=None,
        execute_fn=execute_fn,
        db=None,
    )

    assert called is False, "the tool must not run when the limiter denies"
    assert result == {"error": "Rate limit exceeded", "tool": "netsuite.suiteql"}
