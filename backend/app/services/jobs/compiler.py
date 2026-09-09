"""Compiler — turns a plain-language schedule instruction into a plan the
registry allow-lists (Slice 2, Task 2).

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§B3 (binding): ONE LLM call through the chat's own adapter/BYOK routing
(``app.services.chat.llm_adapter`` + ``app.services.chat.nodes.get_tenant_ai_config``
— the same resolution ``recon_resolution_agent`` uses for its own single
forced-tool call), with a structured-output schema derived from the registry
(``STEP_REGISTRY.plan_schema()`` — never a hand-maintained copy). The result is
VALIDATED against the registry AFTER the call (``registry.validate_plan``):
invalid -> one repair round (the errors fed back as a tool_result, exactly as
a real multi-turn tool conversation would) -> ``Clarification``.
``Clarification(question)`` is also returned directly when the model itself
decides a required detail cannot be derived from the instruction (the mock's
"which subsidiary" case) — it has a second tool, ``ask_clarification``, for
exactly that; both tools are offered every call via ``tool_choice: any`` so
the model always picks whichever fits.

The agent runs ONLY here — at compile time, on creation or an instruction
edit — never inside the executor (Task 4), which only ever replays
``plan_json``. That boundary is why this module returns data (``CompiledPlan``
| ``Clarification``) rather than writing anything to the ``schedules`` table
itself: persisting the compiled plan, bumping ``plan_version``, and deciding
``plan_json`` vs. ``pending_plan_json`` are the API layer's job (§B5, Task
3/5), which already has the existing schedule row those fields live on.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import audit_service
from app.services.chat.llm_adapter import BaseLLMAdapter, LLMResponse, ToolUseBlock, get_adapter
from app.services.chat.nodes import get_tenant_ai_config
from app.services.jobs.registry import (
    STEP_REGISTRY,
    PlanInvalid,
    StepContext,
    ValidatedPlan,
    plan_schema,
    validate_plan,
)

logger = logging.getLogger(__name__)

_MAX_TOKENS = 4096
_COMPILE_TOOL_NAME = "compile_plan"
_CLARIFY_TOOL_NAME = "ask_clarification"

_SYSTEM_PROMPT = """You compile a plain-language scheduled-job instruction into an explicit, \
deterministic plan of steps. This plan is what actually runs, every time, unattended — you \
are never in the loop again once it is approved, so it must not guess at anything the \
instruction does not say.

Every step must be exactly one of the registry's allow-listed types, given to you as the \
compile_plan tool's own schema — nothing outside that allow-list may ever appear (no \
NetSuite or Celigo write is in this registry; do not invent a step type that resembles one).

Order matters: when a step consumes an earlier step's output (for example drive.upload needs \
the id of the report.compose step that produced the report it delivers), that earlier step \
must come first in the steps array, and the later step must name its id in the matching param.

Call compile_plan when every required param can be derived from the instruction and the \
tenant context below. Call ask_clarification with exactly ONE question when a required detail \
is genuinely missing and cannot be guessed (which of several subsidiaries, which report an \
edit refers to, and so on) — never guess at something the instruction does not say."""

_CLARIFY_TOOL = {
    "name": _CLARIFY_TOOL_NAME,
    "description": (
        "Ask the operator one question, instead of compiling a plan, when a required detail "
        "cannot be derived from the instruction or the given tenant context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"question": {"type": "string", "minLength": 1}},
        "required": ["question"],
        "additionalProperties": False,
    },
}


@dataclass
class CompilerLLM:
    """The adapter + model a compile call runs against — bundled together so
    ``compile_instruction``'s ``llm=`` param carries both in one place. Tests
    construct this directly with a fake adapter; production code leaves
    ``llm=None`` and ``compile_instruction`` resolves the tenant's real BYOK
    (or platform-default) provider via ``get_tenant_ai_config``."""

    adapter: BaseLLMAdapter
    model: str


@dataclass
class Clarification:
    question: str


@dataclass
class CompiledPlan:
    plan_json: dict
    summary_line: str
    kinds: set[str]
    model: str


@dataclass(frozen=True)
class DiffLine:
    kind: str  # "add" | "del" | "ctx"
    step: int | None
    text: str


async def _tenant_locations(db: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    """Distinct locations known from the tenant's inventory snapshot, fed to
    the compiler's prompt so it can ground an instruction like "for Dimerco,
    Fedex and Panurgy" against what the data actually has — via the SAME
    ``bigquery_sql`` executor a compiled plan itself would use (the registry's
    own read step is the "registry-provided context hook", not a bespoke
    BigQuery client the compiler would otherwise need its own access path
    for). Best-effort: BigQuery being unreachable narrows the compiler's
    context; it must never fail the whole compile over it."""
    ctx = StepContext(job_id=uuid.uuid4(), run_id=uuid.uuid4(), tenant_id=tenant_id, db=db)
    try:
        result = await STEP_REGISTRY["bigquery_sql"].executor(
            ctx,
            {"query": ("SELECT DISTINCT location FROM `frameworkreporting.inventory_snapshot` ORDER BY location")},
        )
    except Exception:
        logger.warning("jobs.compiler.tenant_locations_failed", exc_info=True)
        return []
    rows = result.get("rows") or []
    locations: list[str] = []
    for row in rows:
        loc = row.get("location") if isinstance(row, dict) else None
        if loc:
            locations.append(str(loc))
    return locations


def _user_message(instruction: str, locations: list[str]) -> str:
    lines = [f"Instruction: {instruction}"]
    if locations:
        lines.append(f"Locations known from the tenant's inventory snapshot: {', '.join(locations)}")
    return "\n".join(lines)


_COMPILE_TOOL_DESCRIPTION = (
    "Compile the instruction into an ordered list of steps, each one of the registry's "
    "allow-listed step types. Call this ONLY when every required param can be derived from "
    "the instruction and the given tenant context — otherwise call ask_clarification instead."
)


async def _one_call(
    llm: CompilerLLM, messages: list[dict]
) -> tuple[str | None, dict | None, ToolUseBlock | None, LLMResponse]:
    """One structured-output call, forced to pick compile_plan or
    ask_clarification (``tool_choice: any`` — never a free-text turn). The
    compile_plan schema is regenerated from the registry EVERY call
    (``plan_schema()``, spec §B2) rather than cached at import time, so it can
    never drift from whatever step types the registry currently allow-lists."""
    tools = [
        {"name": _COMPILE_TOOL_NAME, "description": _COMPILE_TOOL_DESCRIPTION, "input_schema": plan_schema()},
        _CLARIFY_TOOL,
    ]
    response = await llm.adapter.create_message(
        model=llm.model,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=messages,
        tools=tools,
        tool_choice={"type": "any"},
    )
    for block in response.tool_use_blocks:
        if block.name in (_COMPILE_TOOL_NAME, _CLARIFY_TOOL_NAME):
            return block.name, block.input, block, response
    return None, None, None, response


def _build_compiled_plan(plan_json: dict, validated: ValidatedPlan, model: str) -> CompiledPlan:
    kinds = {STEP_REGISTRY[step.type].kind for step in validated.steps}
    labels: list[str] = []
    for step in validated.steps:
        label = STEP_REGISTRY[step.type].label
        if not labels or labels[-1] != label:
            labels.append(label)
    summary_line = f"{len(validated.steps)} steps · " + " → ".join(labels)
    return CompiledPlan(plan_json=plan_json, summary_line=summary_line, kinds=kinds, model=model)


async def _audit_compile(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    instruction: str,
    model: str,
    *,
    outcome: str,
) -> None:
    """One audit event per ``compile_instruction`` call, regardless of how
    many LLM hops (including a repair round) it took to get there — spec §B3
    "Every compile writes an audit event"."""
    await audit_service.log_event(
        db,
        tenant_id=tenant_id,
        category="jobs",
        action="jobs.compile",
        actor_id=actor_id,
        actor_type="user",
        resource_type="schedule_instruction",
        payload={
            "instruction_hash": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            "model": model,
            "outcome": outcome,  # "compiled" | "clarification"
        },
    )
    await db.commit()


def _repair_tool_result_content(errors: list[str]) -> str:
    return (
        "Invalid plan: "
        + "; ".join(errors)
        + ". Provide a corrected plan via compile_plan, or call ask_clarification if a "
        "required detail cannot be derived from the instruction."
    )


async def compile_instruction(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    instruction: str,
    actor_id: uuid.UUID | None,
    llm: CompilerLLM | None = None,
) -> CompiledPlan | Clarification:
    if llm is None:
        provider, model, api_key, _is_byok = await get_tenant_ai_config(db, tenant_id)
        llm = CompilerLLM(adapter=get_adapter(provider, api_key), model=model)

    locations = await _tenant_locations(db, tenant_id)
    messages: list[dict] = [{"role": "user", "content": _user_message(instruction, locations)}]

    tool_name, tool_input, block, response = await _one_call(llm, messages)

    if tool_name == _CLARIFY_TOOL_NAME:
        question = (tool_input or {}).get("question") or "Could you clarify the instruction?"
        await _audit_compile(db, tenant_id, actor_id, instruction, llm.model, outcome="clarification")
        return Clarification(question=question)

    errors: list[str]
    if tool_name == _COMPILE_TOOL_NAME:
        try:
            validated = validate_plan(tool_input)
        except PlanInvalid as exc:
            errors = exc.errors
        else:
            compiled = _build_compiled_plan(tool_input, validated, llm.model)
            await _audit_compile(db, tenant_id, actor_id, instruction, llm.model, outcome="compiled")
            return compiled
    else:
        errors = ["the model did not call compile_plan or ask_clarification"]

    # ---- one repair round (spec §B3: "invalid -> one repair round, then Clarification") ----
    if block is not None:
        messages.append(llm.adapter.build_assistant_message(response))
        messages.append(
            llm.adapter.build_tool_result_message(
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _repair_tool_result_content(errors),
                        "is_error": True,
                    }
                ]
            )
        )
        tool_name, tool_input, block, response = await _one_call(llm, messages)

        if tool_name == _CLARIFY_TOOL_NAME:
            question = (tool_input or {}).get("question") or "Could you clarify the instruction?"
            await _audit_compile(db, tenant_id, actor_id, instruction, llm.model, outcome="clarification")
            return Clarification(question=question)

        if tool_name == _COMPILE_TOOL_NAME:
            try:
                validated = validate_plan(tool_input)
            except PlanInvalid as exc:
                errors = exc.errors
            else:
                compiled = _build_compiled_plan(tool_input, validated, llm.model)
                await _audit_compile(db, tenant_id, actor_id, instruction, llm.model, outcome="compiled")
                return compiled

    question = "I couldn't compile a valid plan for this instruction: " + "; ".join(errors)
    await _audit_compile(db, tenant_id, actor_id, instruction, llm.model, outcome="clarification")
    return Clarification(question=question)


# ---------------------------------------------------------------------------
# plan_diff — the pending-change panel's diff block (spec §B3 / the mock's
# "add Virtual" example).
# ---------------------------------------------------------------------------


def _render_params(params: dict) -> list[str]:
    """Deterministic multi-line text rendering of a step's params: one line
    per key, sorted for stable ordering. A multi-line string value (e.g. a
    SQL query) is rendered as its own lines so a change to a single line
    inside it diffs as just that line, not the whole field — this is what
    lets the bigquery_sql "location filter" hunk in the mock show only the
    changed WHERE-clause line rather than swapping the entire query."""
    lines: list[str] = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value, str) and "\n" in value:
            lines.extend(value.splitlines())
        elif isinstance(value, list):
            lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        else:
            lines.append(f"{key}: {value}")
    return lines


def _step_map(plan: dict | None) -> dict[str, dict]:
    steps = (plan or {}).get("steps") or []
    return {s["id"]: s for s in steps if isinstance(s, dict) and s.get("id")}


def plan_diff(old: dict, new: dict) -> list[DiffLine]:
    """One hunk (a ``ctx`` header line, then ``del``/``add`` lines) per step
    whose params or type changed between ``old`` and ``new`` — an unchanged
    step produces no lines at all, so a small instruction edit (like the
    mock's "and Virtual") only ever surfaces the steps it actually touched."""
    old_by_id = _step_map(old)
    new_steps = (new or {}).get("steps") or []

    lines: list[DiffLine] = []
    for idx, step in enumerate(new_steps, start=1):
        step_id = step.get("id")
        step_type = step.get("type")
        old_step = old_by_id.get(step_id) if step_id else None

        if old_step is None:
            lines.append(DiffLine(kind="ctx", step=idx, text=f"step {idx} · {step_type} · new step"))
            for text in _render_params(step.get("params") or {}):
                lines.append(DiffLine(kind="add", step=idx, text=text))
            continue

        old_lines = _render_params(old_step.get("params") or {})
        new_lines = _render_params(step.get("params") or {})
        if old_lines == new_lines and old_step.get("type") == step_type:
            continue

        lines.append(DiffLine(kind="ctx", step=idx, text=f"step {idx} · {step_type}"))
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            for text in old_lines[i1:i2]:
                lines.append(DiffLine(kind="del", step=idx, text=text))
            for text in new_lines[j1:j2]:
                lines.append(DiffLine(kind="add", step=idx, text=text))

    new_ids = {s["id"] for s in new_steps if isinstance(s, dict) and s.get("id")}
    for step_id, old_step in old_by_id.items():
        if step_id in new_ids:
            continue
        lines.append(DiffLine(kind="ctx", step=None, text=f"step {step_id} · {old_step.get('type')} · removed"))
        for text in _render_params(old_step.get("params") or {}):
            lines.append(DiffLine(kind="del", step=None, text=text))

    return lines
