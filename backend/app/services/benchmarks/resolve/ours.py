"""Today's agent (UnifiedAgent) resolving one benchmark task headless, every tool call taped.

Setup reuses the vs-MCP agent runner's context assembly, so the agent sees what chat
gives it. Like that runner, it needs the platform's model key, so it runs in the staging
container only. Approval cards come from `confirmation_required` events and become
`Proposal`s. The reply graded for brevity is the model's final text (`AgentResult.data`)
less any server-written approval note, which the agent emits just before a card. When
the server prepares the card itself, its `data` IS that note, and none of it is the
model's to count.

The agent acts as `actor_id`, a real active user of the tenant. Native accounting tools
authorize the actor, so a synthetic id would record nothing but refusals.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from app.services.benchmarks import agent_runner
from app.services.benchmarks.resolve.graders import Attempt, proposal_from_card
from app.services.benchmarks.resolve.tape import TapedDispatcher, installed

WALL_CLOCK_SECONDS = 600.0


def _usage(results, field_name):
    return sum(int(getattr(getattr(r, "tokens_used", None), field_name, 0) or 0) for r in results)


def attempt_from_run(events, dispatcher: TapedDispatcher, *, wall_ms: int, error: str | None = None) -> Attempt:
    """Every turn's usage and tool calls are charged; the reply graded is the last turn's."""
    cards = [payload for kind, payload in events if kind == "confirmation_required" and isinstance(payload, dict)]
    results = [payload for kind, payload in events if kind == "response"]
    result = results[-1] if results else None
    reply = str(getattr(result, "data", "") or "") if result is not None else ""
    notes = [
        text.strip()
        for (kind, text), (next_kind, _) in zip(events, events[1:], strict=False)
        if kind == "text" and next_kind == "confirmation_required" and isinstance(text, str) and text.strip()
    ]
    for note in notes:
        reply = reply.replace(note, "")
    return Attempt(
        reply_text=reply.strip(),
        proposals=[proposal_from_card(card) for card in cards],
        resolution=None,  # today's agent declares no structured resolution
        writes_reached_dispatcher=len(dispatcher.writes),
        tape_misses=dispatcher.misses,
        environment_errors=dispatcher.environment_errors,
        refused_tools=len(dispatcher.refused),
        input_tokens=_usage(results, "input_tokens"),
        output_tokens=_usage(results, "output_tokens"),
        cache_tokens=_usage(results, "cache_creation_input_tokens") + _usage(results, "cache_read_input_tokens"),
        tool_calls=sum(len(getattr(r, "tool_calls_log", None) or []) for r in results),
        wall_ms=wall_ms,
        error=error
        or (None if result is not None and getattr(result, "success", False) else "agent_produced_no_response"),
    )


async def run_ours(
    task, trial, *, db, tenant_id: uuid.UUID, actor_id: uuid.UUID, tape, mode: str, model: str
) -> Attempt:
    """One trial. `mode` is "replay" (default for scoring) or "record" (staging, first run)."""
    from app.core.config import settings
    from app.services.chat import tools

    dispatcher = TapedDispatcher(tape, mode=mode, live=tools._execute_tool_call_once if mode == "record" else None)
    start = time.monotonic()
    events: list = []
    error = None
    try:
        adapter = agent_runner._build_adapter(provider="anthropic", api_key=settings.ANTHROPIC_API_KEY)
        metadata = await agent_runner.get_active_metadata(db, tenant_id)
        tenant_config = await agent_runner._load_tenant_config(db, tenant_id)
        context = await agent_runner._assemble_context(
            db=db,
            tenant_id=tenant_id,
            question=task.prompt,
            adapter=adapter,
            entity_resolver_model=model,
            tenant_config=tenant_config,
        )
        agent = agent_runner.UnifiedAgent(
            tenant_id=tenant_id,
            user_id=actor_id,
            correlation_id=f"resolve-bench:{task.ref}:{trial}",
            metadata=metadata,
            policy=None,
            context_need="data",
        )

        async def turn(the_agent, text, turn_context, history=None):
            last = None
            async for kind, payload in the_agent.run_streaming(
                task=text, context=turn_context, db=db, adapter=adapter, model=model, conversation_history=history
            ):
                events.append((kind, payload))
                if kind == "response":
                    last = payload
            return last

        async def drive():
            first = await turn(agent, task.prompt, context)
            if not agent_runner._asks_for_source(first):
                return
            # Answer "which data source?" once, as a person would (the vs-MCP runner's rule).
            history = agent_runner._source_reply_history(task.prompt, first)
            reply = agent_runner._SOURCE_REPLY
            again = agent_runner.UnifiedAgent(
                tenant_id=tenant_id,
                user_id=actor_id,
                correlation_id=f"resolve-bench:{task.ref}:{trial}",
                metadata=metadata,
                policy=None,
                context_need="data",
            )
            reply_context = {
                **context,
                "source_selection_task": reply,
                "source_selection_history": [*history, {"role": "user", "content": reply}],
            }
            await turn(again, reply, reply_context, [{"role": m["role"], "content": m["content"]} for m in history])

        with installed(dispatcher):
            await asyncio.wait_for(drive(), timeout=WALL_CLOCK_SECONDS)
    except TimeoutError:
        error = f"timeout after {WALL_CLOCK_SECONDS:.0f}s"
    except Exception as exc:  # a crashed trial is graded as failed, never dropped
        error = f"{type(exc).__name__}: {exc}"
    return attempt_from_run(events, dispatcher, wall_ms=int((time.monotonic() - start) * 1000), error=error)
