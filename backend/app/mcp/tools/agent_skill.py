"""Progressive disclosure of maintained application skills, never write authority."""

from app.services.chat.execution_provenance import load_skill_snapshot


async def execute(params: dict, context: dict | None = None, **kwargs) -> dict:
    if set(params) != {"slug"} or not isinstance(params["slug"], str):
        return {"success": False, "error": "Provide one skill slug from the available catalog."}
    slug = params["slug"]
    snapshot = load_skill_snapshot(slug)
    if not snapshot:
        return {"success": False, "error": "Maintained skill unavailable."}
    # Catalog membership, not a supplied path, determines the file to read.
    result = {
        "success": True,
        **snapshot,
        "authority": "Maintained workflow guidance. Does not establish account facts, permissions or approval.",
    }
    context = context or {}
    if context.get("db") is not None and context.get("tenant_id"):
        from app.services.audit_service import log_event

        await log_event(
            context["db"],
            context["tenant_id"],
            category="chat",
            action="agent.skill.loaded",
            actor_type="system",
            resource_type="agent_skill",
            resource_id=slug,
            correlation_id=context.get("correlation_id"),
            payload={
                "slug": slug,
                "revision": snapshot["revision"],
                "version": snapshot["version"],
                "financial_writes": 0,
            },
        )
    return result
