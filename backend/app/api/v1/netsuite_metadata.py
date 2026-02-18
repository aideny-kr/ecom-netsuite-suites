"""API endpoints for NetSuite metadata discovery and retrieval."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/netsuite/metadata", tags=["netsuite-metadata"])


@router.post("/discover")
async def trigger_metadata_discovery(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Queue an async metadata discovery task.

    Runs 10 SuiteQL queries to discover custom fields, record types,
    subsidiaries, departments, classes, and locations. Results are used
    to enrich the AI chat's system prompt and RAG knowledge base.
    """
    from app.workers.tasks.metadata_discovery import netsuite_metadata_discovery

    task = netsuite_metadata_discovery.delay(
        tenant_id=str(user.tenant_id),
        user_id=str(user.id),
    )
    return {"task_id": task.id, "status": "queued"}


@router.get("")
async def get_metadata(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the latest discovered metadata for the tenant."""
    from app.services.netsuite_metadata_service import get_active_metadata

    metadata = await get_active_metadata(db, user.tenant_id)
    if metadata is None:
        return {"status": "not_discovered", "message": "No metadata discovered yet."}

    return {
        "id": str(metadata.id),
        "version": metadata.version,
        "status": metadata.status,
        "discovered_at": metadata.discovered_at.isoformat() if metadata.discovered_at else None,
        "total_fields_discovered": metadata.total_fields_discovered,
        "queries_succeeded": metadata.query_count,
        "discovery_errors": metadata.discovery_errors,
        "categories": {
            "transaction_body_fields": len(metadata.transaction_body_fields)
            if isinstance(metadata.transaction_body_fields, list)
            else 0,
            "transaction_column_fields": len(metadata.transaction_column_fields)
            if isinstance(metadata.transaction_column_fields, list)
            else 0,
            "entity_custom_fields": len(metadata.entity_custom_fields)
            if isinstance(metadata.entity_custom_fields, list)
            else 0,
            "item_custom_fields": len(metadata.item_custom_fields)
            if isinstance(metadata.item_custom_fields, list)
            else 0,
            "custom_record_types": len(metadata.custom_record_types)
            if isinstance(metadata.custom_record_types, list)
            else 0,
            "custom_lists": len(metadata.custom_lists) if isinstance(metadata.custom_lists, list) else 0,
            "subsidiaries": len(metadata.subsidiaries) if isinstance(metadata.subsidiaries, list) else 0,
            "departments": len(metadata.departments) if isinstance(metadata.departments, list) else 0,
            "classifications": len(metadata.classifications) if isinstance(metadata.classifications, list) else 0,
            "locations": len(metadata.locations) if isinstance(metadata.locations, list) else 0,
        },
    }


@router.get("/fields/{category}")
async def get_metadata_fields(
    category: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the raw field data for a specific metadata category.

    Valid categories: transaction_body_fields, transaction_column_fields,
    entity_custom_fields, item_custom_fields, custom_record_types,
    custom_lists, subsidiaries, departments, classifications, locations.
    """
    valid_categories = {
        "transaction_body_fields",
        "transaction_column_fields",
        "entity_custom_fields",
        "item_custom_fields",
        "custom_record_types",
        "custom_lists",
        "subsidiaries",
        "departments",
        "classifications",
        "locations",
    }
    if category not in valid_categories:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category. Valid: {', '.join(sorted(valid_categories))}",
        )

    from app.services.netsuite_metadata_service import get_active_metadata

    metadata = await get_active_metadata(db, user.tenant_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="No metadata discovered yet.")

    data = getattr(metadata, category, None)
    return {
        "category": category,
        "count": len(data) if isinstance(data, list) else 0,
        "data": data or [],
    }
