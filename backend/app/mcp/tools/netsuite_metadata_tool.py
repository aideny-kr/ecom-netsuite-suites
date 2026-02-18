"""MCP tool to trigger NetSuite metadata re-discovery from the chat interface.

Allows users to say "refresh my NetSuite metadata" in the chat and
have the system re-discover custom fields, record types, and org hierarchy.
"""

from __future__ import annotations


async def execute(params: dict, context: dict | None = None, **kwargs) -> dict:
    """Trigger an async metadata discovery run for the current tenant."""
    if not context:
        return {"error": True, "message": "Missing context — tenant_id required."}

    tenant_id = context.get("tenant_id")
    if not tenant_id:
        return {"error": True, "message": "Missing tenant_id in context."}

    actor_id = context.get("actor_id")

    from app.workers.tasks.metadata_discovery import netsuite_metadata_discovery

    task = netsuite_metadata_discovery.delay(
        tenant_id=str(tenant_id),
        user_id=str(actor_id) if actor_id else None,
    )

    return {
        "status": "discovery_queued",
        "task_id": task.id,
        "message": (
            "NetSuite metadata discovery has been queued. "
            "Custom field definitions, record types, subsidiaries, departments, "
            "classes, and locations will be refreshed in approximately 30 seconds. "
            "The system prompt and knowledge base will be automatically updated."
        ),
    }


async def execute_get_metadata(params: dict, context: dict | None = None, **kwargs) -> dict:
    """Return the latest discovered metadata summary for the current tenant."""
    if not context:
        return {"error": True, "message": "Missing context — tenant_id and db required."}

    tenant_id = context.get("tenant_id")
    db = context.get("db")
    if not tenant_id or not db:
        return {"error": True, "message": "Missing tenant_id or db in context."}

    from app.services.netsuite_metadata_service import get_active_metadata

    metadata = await get_active_metadata(db, tenant_id)
    if metadata is None:
        return {
            "status": "not_discovered",
            "message": (
                "No metadata has been discovered yet. Use the netsuite.refresh_metadata tool to trigger discovery."
            ),
        }

    summary = {
        "status": metadata.status,
        "version": metadata.version,
        "discovered_at": metadata.discovered_at.isoformat() if metadata.discovered_at else None,
        "total_fields": metadata.total_fields_discovered,
        "queries_succeeded": metadata.query_count,
        "categories": {},
    }

    for attr, label in [
        ("transaction_body_fields", "Transaction body fields (custbody_*)"),
        ("transaction_column_fields", "Transaction line fields (custcol_*)"),
        ("entity_custom_fields", "Entity custom fields (custentity_*)"),
        ("item_custom_fields", "Item custom fields (custitem_*)"),
        ("custom_record_types", "Custom record types"),
        ("custom_lists", "Custom lists"),
        ("subsidiaries", "Subsidiaries"),
        ("departments", "Departments"),
        ("classifications", "Classes"),
        ("locations", "Locations"),
    ]:
        val = getattr(metadata, attr, None)
        summary["categories"][label] = len(val) if isinstance(val, list) else 0

    if metadata.discovery_errors:
        summary["errors"] = metadata.discovery_errors

    return summary
