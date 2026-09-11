"""Progressive disclosure of maintained application skills, never write authority."""

from hashlib import sha256

from app.services.chat.skills import get_all_skills_metadata, get_skill_instructions


async def execute(params: dict, context: dict | None = None, **kwargs) -> dict:
    if set(params) != {"slug"} or not isinstance(params["slug"], str):
        return {"success": False, "error": "Provide one skill slug from the available catalog."}
    slug = params["slug"]
    metadata = next((s for s in get_all_skills_metadata() if s["slug"] == slug), None)
    instructions = get_skill_instructions(slug) if metadata else None
    if not instructions:
        return {"success": False, "error": "Maintained skill unavailable."}
    revision = sha256(instructions.encode()).hexdigest()
    # Catalog membership, not a supplied path, determines the file to read.
    result = {
        "success": True,
        "slug": slug,
        "name": metadata["name"],
        "revision": revision,
        "instructions": instructions,
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
            payload={"slug": slug, "revision": revision, "financial_writes": 0},
        )
    return result
