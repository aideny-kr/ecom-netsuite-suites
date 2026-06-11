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

    def resolver(rid: str) -> dict:
        # 1) PRIMARY: the in-turn full-payload sidecar (this turn or a recent turn
        #    within TTL).
        if conversation_id is not None:
            cached = get_full_payload(str(conversation_id), rid)
            if cached is not None:
                return cached
        # 2) FALLBACK: persisted ChatMessage tool_calls (cross-turn / regeneration).
        return resolve_payload_from_messages(fallback_messages, rid)

    return await compose_report(
        db,
        tenant_id=tenant_id,
        title=params["title"],
        sections=params["sections"],
        resolver=resolver,
        created_by=ctx.get("actor_id"),
        source_run_id=conversation_id,
    )
