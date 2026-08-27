"""No caller may execute a NetSuite mutation without human approval.

THE HOLE THIS CLOSES. `execute_tool_call` (chat/tools.py) is the single
dispatcher for every tool, and until now the HITL guard lived in ONE of its
callers — `base_agent.run_streaming`, which intercepts a mutation and yields a
confirmation card instead of executing. Every other caller reached the ERP
unguarded. Confirmed reachable:

  session.workspace_id set  -> workspace_context truthy (orchestrator.py:2900)
  -> `if not is_onboarding and not workspace_context:` (2933) is False
  -> the guarded unified-agent block is skipped (it ends in `return`, 4009)
  -> falls through to the single-agent loop (4016), whose toolset is
     build_all_tool_definitions (2824) -> build_external_tool_definitions,
     i.e. ns_createRecord / ns_updateRecord / ns_deleteRecord are present
  -> execute_tool_call at 4176 with the model's raw block.name/block.input,
     behind only policy_evaluate, which inspects SQL params and row limits and
     never classifies mutations
  -> `classify_mutation` appears ZERO times in orchestrator.py

So a workspace-attached chat session could create, update or delete a
production NetSuite record with no confirmation card, no HMAC token and no
human approval — violating the product's stated core invariant (CLAUDE.md:
"ns_createRecord/ns_updateRecord MUST NOT auto-execute").

`.claude/rules/agent-graph.md` #3 already named the shape — "execute_tool_call
is the single dispatcher and has 7 callers; classify_mutation is called from
only one of them. Adding a caller must not be able to add a hole." — so the fix
is not another per-caller check. It is the guard moving to the dispatcher, with
approval as an explicit, default-denied argument, so that a caller added
tomorrow that knows nothing about any of this is REFUSED rather than trusted.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chat import tools as tools_mod

_HEX = "a" * 32


def _ext(name: str) -> str:
    return f"ext__{_HEX}__{name}"


async def _call(tool_name: str, **kwargs):
    return await tools_mod.execute_tool_call(
        tool_name=tool_name,
        tool_input={"recordType": "customer", "data": "{}"},
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        correlation_id="c",
        db=None,
        session_id="s",
        **kwargs,
    )


@pytest.mark.parametrize(
    "mutation_tool",
    ["ns_createRecord", "ns_updateRecord", "ns_deleteRecord", "ns_upsertRecord"],
)
@pytest.mark.asyncio
async def test_mutation_is_refused_without_approval(mutation_tool):
    """The default. A caller that says nothing about approval gets refused —
    which is what makes a future caller safe by construction."""
    spy = AsyncMock(return_value="{}")
    with patch.object(tools_mod, "_execute_external_tool", spy):
        out = json.loads(await _call(_ext(mutation_tool)))

    assert "error" in out
    spy.assert_not_awaited(), "the write must not reach the connector at all"


@pytest.mark.asyncio
async def test_refusal_names_the_reason_and_is_not_silent():
    """A refusal the model cannot understand becomes a retry loop. It has to
    say what happened and what the correct path is."""
    with patch.object(tools_mod, "_execute_external_tool", AsyncMock(return_value="{}")):
        out = json.loads(await _call(_ext("ns_createRecord")))
    blob = json.dumps(out).lower()
    assert "approval" in blob or "approved" in blob
    assert "confirm" in blob


@pytest.mark.asyncio
async def test_the_approve_path_still_executes():
    """The one legitimate caller: the orchestrator's approve branch, which has
    already HMAC-verified the payload a human accepted. If this regresses,
    every approved write silently stops working."""
    spy = AsyncMock(return_value={"success": True, "recordId": "1"})
    with patch.object(tools_mod, "_execute_external_tool", spy):
        raw = await _call(_ext("ns_createRecord"), human_approved=True)

    # The dispatcher re-encodes whatever the connector returned; what matters
    # here is that the call was DISPATCHED rather than refused.
    assert "recordId" in raw
    spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_reads_are_untouched():
    """The guard must be narrow. Metadata, subsidiary and SuiteQL lookups are
    what the write loop itself is built out of — breaking them would break
    validation, the slot form and the posting invariants."""
    for read_tool in ("ns_getRecordTypeMetadata", "ns_getSubsidiaries", "ns_runCustomSuiteQL", "ns_getRecord"):
        spy = AsyncMock(return_value=json.dumps({"ok": True}))
        with patch.object(tools_mod, "_execute_external_tool", spy):
            out = json.loads(await _call(_ext(read_tool)))
        assert "error" not in out, read_tool
        spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_celigo_write_verbs_are_covered_too():
    """Not a NetSuite-only guard, and that is deliberate. `classify_mutation`
    imports CELIGO_WRITE_VERBS, so a Celigo write is refused here as well.

    Celigo writes are already filtered out of the tool inventory and blocked in
    _execute_external_tool, so this is a third, redundant layer — which is the
    correct amount for an irreversible external write. If Celigo ever grows its
    own approval path it must pass human_approved like everything else."""
    spy = AsyncMock(return_value={"ok": True})
    with patch.object(tools_mod, "_execute_external_tool", spy):
        out = json.loads(await _call(_ext("upsert_flow")))
    assert out.get("hitl_required") is True
    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_mutation_external_tool_is_untouched():
    """The narrowness check: a read-shaped tool on any provider dispatches."""
    spy = AsyncMock(return_value={"ok": True})
    with patch.object(tools_mod, "_execute_external_tool", spy):
        raw = await _call(_ext("list_flows"))
    assert "error" not in raw
    spy.assert_awaited_once()
