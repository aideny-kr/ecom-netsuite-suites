import logging

logger = logging.getLogger(__name__)


async def execute(params: dict, context: dict | None = None, **kwargs) -> dict:
    """Compose a publishable report from results produced THIS turn.

    Repurposed from the old ``report.export`` stub. Resolves each data section's
    ``result_id`` (r1, r2, ...) to the FULL, uncapped frozen payload.

    Resolution is CACHE-FIRST (gate cluster A):
      1. PRIMARY (same-turn) — the eager in-turn full-payload sidecar in Redis,
         keyed by (conversation_id, result_id). The orchestrator writes it the
         instant each data tool is intercepted, so a report.compose in the SAME
         agent loop can read the results just computed THIS turn — the current
         turn's assistant ``ChatMessage`` is not persisted until AFTER the loop.
      2. FALLBACK (cross-turn / regeneration) — the persisted
         ``ChatMessage.tool_calls[].result_payload`` via
         ``resolve_payload_from_messages``. By the time a LATER turn (or a report
         regeneration) composes, the prior turn's results are in the DB.

    Fills templated-narrative placeholders, renders charts server-side, assembles
    ``spec_json`` + self-contained HTML, and persists an RLS-scoped report row.
    The report row + audit are flushed (NOT committed) so they persist atomically
    with the rest of the turn under the orchestrator's single end-of-turn commit.
    """
    from app.services.chat.result_cache import get_full_payload
    from app.services.chat.tool_call_results import (
        load_conversation_tool_messages,
        resolve_payload_from_messages,
    )
    from app.services.report.recipe import build_recipe
    from app.services.report.report_service import compose_report

    ctx = context or {}
    db = ctx["db"]
    tenant_id = ctx["tenant_id"]
    conversation_id = ctx.get("conversation_id")

    # Pre-load the persisted fallback snapshot once (a single RLS-scoped SELECT) so
    # the resolver itself can stay synchronous — report_service's Resolver contract
    # (fill_placeholders / _resolve_data_section) calls it sync. The sidecar still
    # wins per-result_id; this is only consulted on a sidecar miss.
    fallback_messages = await load_conversation_tool_messages(db, conversation_id, tenant_id)

    # Memoized (gate r1): the pre-check below, per-section resolution, and narrative
    # placeholder fills would otherwise each re-hit Redis/the persisted walk for the
    # same rid — one resolution per rid per compose.
    resolved: dict[str, dict] = {}

    def resolver(rid: str) -> dict:
        if rid in resolved:
            return resolved[rid]
        payload = None
        # 1) PRIMARY: the in-turn full-payload sidecar (this turn or a recent turn
        #    within TTL).
        if conversation_id is not None:
            payload = get_full_payload(str(conversation_id), rid)
        if payload is None:
            # 2) FALLBACK: persisted ChatMessage tool_calls (cross-turn / regeneration).
            payload = resolve_payload_from_messages(fallback_messages, rid)
        resolved[rid] = payload
        return payload

    # FAIL LOUDLY on unresolvable DATA sections (live QA, 2026-07-09): assemble_spec
    # degrades a missing rid into a 'Data unavailable' section — right for VIEW-time
    # robustness, wrong at COMPOSE time, where it silently publishes a broken
    # financial artifact. Raising instead surfaces a tool error the agent can act on
    # in the SAME loop (re-run the source tool, compose again) — parity with refresh,
    # which fails closed on any missing source rather than rendering holes.
    # Scope (gate r1): ONLY data sections' result_id are hard dependencies. A rid
    # referenced solely inside narrative {{result:…}} placeholders keeps
    # fill_placeholders' graceful inline '[unresolved: …]' degradation — a stale
    # narrative reference must not block an otherwise-complete report.
    # Any resolution failure counts as missing (a Redis blip raises ConnectionError,
    # not KeyError — the agent-actionable refusal beats a raw 500 either way).
    # Hard dependencies come from DATA section types only (a stray result_id on a
    # heading/narrative is ignored by assemble_spec — gate r2). normalize_sections
    # first so the LLM's known aliases ("data" → table) classify correctly.
    from app.schemas.report import normalize_sections

    data_types = {"table", "chart", "metric_headline"}
    section_rids = list(
        dict.fromkeys(
            s["result_id"]
            for s in normalize_sections(params["sections"])
            if isinstance(s, dict)
            and s.get("type") in data_types
            and isinstance(s.get("result_id"), str)
            and s["result_id"]
        )
    )
    missing = []
    for rid in section_rids:
        try:
            resolver(rid)
        except Exception:
            # LOG the root cause (gate r2): a real resolver bug relabeled as a stale
            # rid would send the agent (and a debugging human) down the wrong path.
            logger.warning("report.compose precheck: rid=%s failed to resolve", rid, exc_info=True)
            missing.append(rid)
    if missing:
        raise ValueError(
            f"result(s) {', '.join(missing)} are no longer available to compose — "
            "re-run the source tool(s) to get fresh result_ids, then compose again"
        )

    # RECIPE CAPTURE (Slice A, live-dashboard reports): record the refresh recipe —
    # the verbatim pre-resolution sections + per-result_id {tool, params,
    # connection_id} from the SAME two meta sources the resolver reads (sidecar
    # first, then the already-loaded persisted snapshot — no extra query).
    # Best-effort + fail closed: any unrecoverable/ineligible rid ⇒ recipe is None
    # and the report composes exactly as a plain snapshot (never raises).
    recipe = build_recipe(
        sections=params["sections"],
        conversation_id=conversation_id,
        fallback_messages=fallback_messages,
    )

    return await compose_report(
        db,
        tenant_id=tenant_id,
        title=params["title"],
        sections=params["sections"],
        resolver=resolver,
        created_by=ctx.get("actor_id"),
        source_run_id=conversation_id,
        recipe_json=recipe,
    )
