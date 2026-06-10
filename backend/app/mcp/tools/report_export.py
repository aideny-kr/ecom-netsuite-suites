async def execute(params: dict, context: dict | None = None, **kwargs) -> dict:
    """Compose a publishable report from prior tool results in this turn.

    Repurposed from the old ``report.export`` stub. Resolves each data section's
    ``result_id`` to the FULL, uncapped frozen payload from the conversation's
    ``ChatMessage.tool_calls[].result_payload`` (NOT the 50-row Redis cache),
    fills templated-narrative placeholders, renders charts server-side, assembles
    ``spec_json`` and self-contained HTML, and persists an RLS-scoped report row.
    """
    from app.services.chat.tool_call_results import (
        load_conversation_tool_messages,
        resolve_payload_from_messages,
    )
    from app.services.report.report_service import compose_report

    ctx = context or {}
    db = ctx["db"]
    tenant_id = ctx["tenant_id"]
    conversation_id = ctx.get("conversation_id")

    # Load the full (uncapped) tool-call payloads for this conversation once, then
    # resolve synchronously from that in-memory snapshot.
    messages = await load_conversation_tool_messages(db, conversation_id)

    def resolver(rid: str) -> dict:
        return resolve_payload_from_messages(messages, rid)

    return await compose_report(
        db,
        tenant_id=tenant_id,
        title=params["title"],
        sections=params["sections"],
        resolver=resolver,
        created_by=ctx.get("actor_id"),
        source_run_id=conversation_id,
    )
