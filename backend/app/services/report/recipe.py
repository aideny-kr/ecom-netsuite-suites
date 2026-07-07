"""Recipe capture — Slice A of live-dashboard reports.

``build_recipe`` assembles the ``reports.recipe_json`` value at compose time: the
LLM's compose sections VERBATIM (pre-resolution — Slice B feeds them back into
``assemble_spec`` unchanged) plus, per referenced result_id, the EXECUTED
``{tool, params, connection_id}`` triple recovered from the same two places payload
resolution reads (the in-turn sidecar first, the persisted tool_calls as the
cross-turn fallback) — so meta availability tracks payload availability, and the
recipe is always server-captured from tool calls that actually ran, never
model-authored post-hoc.

Trust boundary (spec §4A): READ-ONLY allowlisted tools only, keyed off the single
category registry (``tool_categories.categorize`` via ``is_stamped_data_tool``) so
there is no second tool-name list to drift; mutation tools are structurally excluded
(they never earn a result_id) AND explicitly rejected (defense in depth, per the
HITL invariants). Fail closed: ONE rid whose meta is unrecoverable or whose tool is
ineligible ⇒ the WHOLE recipe is omitted (``None``) — never a partial recipe.

``connection_id``: for external MCP tools the connector UUID is parsed from the
executed tool name (``ext__<hex32>__…`` via ``parse_external_tool_name`` — never a
hand-rolled regex) and stored as its dashed string form. LOCAL tools (SuiteQL /
financial report / metric / BigQuery) carry ``None``: they resolve the tenant's
active connection at execution time, and a Slice-B replay calls the same
``execute()`` and re-resolves identically. Multi-active-connection support would
require local tools to surface the resolved connection id — a Slice-B+ decision.

Capture is best-effort by contract: ``build_recipe`` NEVER raises — any internal
failure logs a warning and returns ``None``, and the report composes exactly as a
plain snapshot (zero behavior change).
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

RECIPE_SCHEMA_VERSION = 1

# pivot_query_result replays against an EPHEMERAL prior-message result — a recipe
# containing it can never be re-executed standalone. Fail closed rather than store a
# permanently-unrefreshable recipe. (Both the LLM-facing and registry spellings.)
_RECIPE_INELIGIBLE_TOOLS = frozenset({"pivot_query_result", "pivot.query_result"})


def is_recipe_eligible(tool_name: str) -> bool:
    """True iff ``tool_name`` may enter a recipe: a READ-ONLY, standalone-replayable,
    result_id-bearing data tool. Reuses the category registry (via
    ``is_stamped_data_tool`` — exactly the population that can bear a result_id), so
    the allowlist can never drift from the id space."""
    from app.services.chat.mutation_guard import is_mutation_tool
    from app.services.chat.tool_call_results import is_stamped_data_tool

    if is_mutation_tool(tool_name):
        # Defense in depth: structurally unreachable (mutation tools never earn a
        # result_id), but the HITL invariant deserves its own explicit gate.
        return False
    if tool_name in _RECIPE_INELIGIBLE_TOOLS:
        return False
    return is_stamped_data_tool(tool_name)


def _connection_id_of(tool_name: str) -> str | None:
    """The connector UUID (dashed string) for ext__ tools; None for local tools."""
    from app.services.chat.tools import parse_external_tool_name

    parsed = parse_external_tool_name(tool_name)
    return str(parsed[0]) if parsed else None


def _meta_for(rid: str, conversation_id: Any, fallback_meta: dict[str, dict]) -> dict[str, Any] | None:
    """The executed {tool, params} for ``rid`` — sidecar entry first (same-turn),
    else the persisted cross-turn map. None when neither has usable meta."""
    from app.services.chat.result_cache import get_full_payload_entry

    if conversation_id is not None:
        entry = get_full_payload_entry(str(conversation_id), rid)
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("tool"), str)
            and entry.get("tool")
            and isinstance(entry.get("params"), dict)
        ):
            return {"tool": entry["tool"], "params": entry["params"]}
    return fallback_meta.get(rid)


def build_recipe(
    *,
    sections: list[dict],
    conversation_id: Any,
    fallback_messages: list[Any],
) -> dict[str, Any] | None:
    """Build the recipe_json dict for a compose, or ``None`` (= snapshot-only).

    ``sections`` is the LLM's compose input, deep-copied verbatim; ``fallback_messages``
    is the SAME persisted snapshot the payload resolver was given (already loaded —
    no extra query). NEVER raises."""
    try:
        from app.services.chat.tool_call_results import collect_tool_meta_from_messages
        from app.services.report.report_service import referenced_result_ids

        rids = referenced_result_ids(sections)
        if not rids:
            return None  # nothing to re-execute — a narrative-only report stays a snapshot

        fallback_meta = collect_tool_meta_from_messages(fallback_messages or [])
        sources: dict[str, dict[str, Any]] = {}
        for rid in rids:
            meta = _meta_for(rid, conversation_id, fallback_meta)
            if meta is None:
                logger.warning("report.recipe.skipped reason=meta_unrecoverable rid=%s", rid)
                return None
            if not is_recipe_eligible(meta["tool"]):
                logger.warning("report.recipe.skipped reason=tool_not_allowlisted rid=%s tool=%s", rid, meta["tool"])
                return None
            sources[rid] = {
                "tool": meta["tool"],
                "params": meta["params"],
                "connection_id": _connection_id_of(meta["tool"]),
            }
        return {
            "schema_version": RECIPE_SCHEMA_VERSION,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "sections": copy.deepcopy(sections),
            "sources": sources,
        }
    except Exception:
        # Capture must never break compose — the report simply stays snapshot-only.
        logger.warning("report.recipe.skipped reason=capture_error", exc_info=True)
        return None
