"""B4: the native reference, Claude Opus 5.5 at high thinking + the NetSuite MCP, in a plain loop.

Spec 2026-10-01 §7 and the 2026-10-08 definition of done (G2): our agent must match or beat
this on Framework's cases. It gets what a person working with Claude + the NetSuite MCP would
give it, and nothing of our agent's brain:

- the same task prompt, the same recorded tape, the same token meter and IO watcher;
- tools: ``case_open`` (the facts-only case file, B5: saved evidence with copied values and
  no diagnosis) and the Framework NetSuite MCP's record and SuiteQL tools. Never our card
  builders or ``transaction_ops_accounting_evidence``, which return our own prepared fix;
- its writes (``ns_createRecord``/``ns_updateRecord``) are its proposals, as each would
  wait for a person's approval: recorded, answered "pending approval", never sent.
"""

from __future__ import annotations

import asyncio
import json
import time

from app.services.benchmarks.resolve.graders import Attempt
from app.services.benchmarks.resolve.meter import ModelMeter, metered
from app.services.benchmarks.resolve.ours import WALL_CLOCK_SECONDS
from app.services.benchmarks.resolve.outcome import write_from_call
from app.services.benchmarks.resolve.tape import TapedDispatcher, installed

MODEL = "claude-opus-5-5"
THINKING = "high"
MAX_STEPS = 30
MAX_TOKENS = 16384
CASE_OPEN = "case_open"
# The NetSuite MCP's record and query tools; its report and selector widgets are not reads a
# case needs and render apps rather than data.
NETSUITE_VERBS = frozenset(
    {
        "ns_runCustomSuiteQL",
        "ns_getRecord",
        "ns_getRecordTypeMetadata",
        "ns_getSuiteQLMetadata",
        "ns_getSubsidiaries",
        "ns_getAccountingBooks",
        "ns_createRecord",
        "ns_updateRecord",
    }
)
SYSTEM = (
    "You resolve one reconciliation case between Solidus (Framework's order system) and NetSuite. "
    "Read the case with case_open, inspect NetSuite with the NetSuite tools, and decide what is right. "
    "If NetSuite is already right, say so and change nothing. If NetSuite needs a change, propose the exact "
    "record by calling ns_createRecord or ns_updateRecord with the full payload; a person approves every write "
    "before it runs. Answer briefly."
)
CASE_OPEN_TOOL = {
    "name": CASE_OPEN,
    "description": "Open the reconciliation case: the Solidus order, the NetSuite documents saved for it, "
    "the comparison between the two, refunds and earlier fixes. Saved evidence only; it suggests no answer.",
    "input_schema": {
        "type": "object",
        "properties": {"case_id": {"type": "string", "description": "the case id from the task"}},
        "required": ["case_id"],
    },
}
PENDING = json.dumps({"status": "pending_approval", "note": "A person approves this write before it runs."})


def native_tools(mcp_tools: list[dict]) -> list[dict]:
    return [CASE_OPEN_TOOL] + [t for t in mcp_tools if str(t.get("name")).rsplit("__", 1)[-1] in NETSUITE_VERBS]


async def _mcp_tools(db, tenant_id) -> list[dict]:
    from app.services.benchmarks.baseline_runner import _build_baseline_tools

    return await _build_baseline_tools(db, tenant_id)


def _client():
    import anthropic

    from app.core.config import settings

    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


def _request(messages, tools) -> dict:
    from app.services.chat.adapters.anthropic_adapter import _apply_thinking

    kwargs = {"model": MODEL, "max_tokens": MAX_TOKENS, "system": SYSTEM, "messages": messages, "tools": tools}
    _apply_thinking(kwargs, MODEL, MAX_TOKENS, THINKING, None)
    return kwargs


async def _call(name, tool_input, *, db, tenant_id, actor_id, correlation_id) -> str:
    if name == CASE_OPEN:
        from app.services.transaction_ops import case_file

        try:
            body = await case_file.open_case(db, tenant_id, case_id=(tool_input or {}).get("case_id"))
        except Exception as exc:  # noqa: BLE001 - the model sees the failure, as it would natively
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        return json.dumps(body, default=str)
    from app.services.chat import tools

    return await tools.execute_tool_call(
        tool_name=name,
        tool_input=tool_input,
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        db=db,
        perf_guard=False,  # plain Claude + MCP: our query guards must not help it
    )


async def run_reference(task, trial, *, db, tenant_id, actor_id, tape, mode: str) -> Attempt:
    """One trial of the native reference. ``mode`` as for our agent ("replay" or "record")."""
    from app.services.chat import tools as chat_tools

    dispatcher = TapedDispatcher(tape, mode=mode, live=chat_tools._execute_tool_call_once if mode == "record" else None)
    start, meter, writes = time.monotonic(), ModelMeter(), []
    reply, error, tool_calls = "", None, 0
    correlation_id = f"resolve-bench-reference:{task.ref}:{trial}"

    async def loop():
        nonlocal reply, tool_calls
        client = _client()
        toolset = native_tools(await _mcp_tools(db, tenant_id))
        messages = [{"role": "user", "content": task.prompt}]
        for _ in range(MAX_STEPS):
            # An explicit timeout: high thinking raises max_tokens past the SDK's non-streaming
            # estimate, and the trial's own wall clock bounds the request anyway.
            response = await client.messages.create(**_request(messages, toolset), timeout=WALL_CLOCK_SECONDS)
            blocks = [block.model_dump(exclude_none=True) for block in response.content]
            texts = [b["text"] for b in blocks if b.get("type") == "text"]
            if texts:
                reply = "\n".join(texts).strip()
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not uses:
                return None
            # Thinking blocks go back unchanged: tool use with thinking requires them.
            messages.append({"role": "assistant", "content": blocks})
            results = []
            for use in uses:
                tool_calls += 1
                write = write_from_call(use["name"], use.get("input"))
                if write is not None:
                    writes.append(write)
                    result = PENDING
                else:
                    result = await _call(
                        use["name"],
                        use.get("input"),
                        db=db,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        correlation_id=correlation_id,
                    )
                results.append({"type": "tool_result", "tool_use_id": use["id"], "content": str(result)})
            messages.append({"role": "user", "content": results})
        return f"max_steps ({MAX_STEPS})"

    try:
        with installed(dispatcher), metered(meter):
            error = await asyncio.wait_for(loop(), timeout=WALL_CLOCK_SECONDS)
    except TimeoutError:
        error = f"timeout after {WALL_CLOCK_SECONDS:.0f}s"
    except Exception as exc:  # a crashed trial is graded as failed, never dropped
        error = f"{type(exc).__name__}: {exc}"
    return Attempt(
        reply_text=reply,
        shown_text=reply,
        writes=writes,
        writes_are_proposals=True,
        writes_reached_dispatcher=len(dispatcher.writes),
        tape_misses=dispatcher.misses,
        environment_errors=dispatcher.environment_errors,
        unreplayable=dispatcher.unreplayable,
        network_blocked=dispatcher.network_blocked,
        io_failures=dispatcher.io_failures,
        unmetered_model_calls=meter.unmetered,
        embedding_tokens=meter.embedding_tokens,
        refused_tools=len(dispatcher.refused),
        input_tokens=meter.input_tokens,
        output_tokens=meter.output_tokens,
        cache_tokens=meter.cache_tokens,
        tool_calls=tool_calls,
        wall_ms=int((time.monotonic() - start) * 1000),
        error=error,
    )
