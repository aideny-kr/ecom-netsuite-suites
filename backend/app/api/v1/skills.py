"""Saved SuiteQL Analytics skill endpoints."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_permission
from app.models.connection import Connection
from app.models.saved_query import SavedSuiteQLQuery
from app.models.user import User
from app.services import audit_service
from app.services.skills_service import delete_saved_query, get_saved_query, inject_fetch_limit, update_saved_query

router = APIRouter(prefix="/skills", tags=["skills"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PreviewRequest(BaseModel):
    query_id: str = Field(description="UUID of saved query to preview")


class PreviewResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


class ExportRequest(BaseModel):
    query_id: str = Field(description="UUID of saved query to export")


class ExportResponse(BaseModel):
    task_id: str
    status: str = "queued"


class SavedQueryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class SavedQueryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    query_text: str = Field(min_length=1)
    result_data: dict | None = None  # Snapshot: {columns, rows, row_count}


class SavedQueryResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None
    query_text: str
    result_data: dict | None = None
    created_at: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def execute_suiteql_for_tenant(*, db: AsyncSession, tenant_id: uuid.UUID, query: str) -> dict:
    """Resolve creds and execute SuiteQL for the given tenant."""
    from app.core.encryption import decrypt_credentials
    from app.services.netsuite_client import execute_suiteql
    from app.services.netsuite_oauth_service import get_valid_token

    result = await db.execute(
        select(Connection)
        .where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        )
        .order_by((Connection.auth_type == "oauth2").desc(), Connection.created_at.desc())
        .limit(1)
    )
    connection = result.scalar_one_or_none()
    if not connection:
        raise HTTPException(status_code=400, detail="No active NetSuite connection found.")

    creds = decrypt_credentials(connection.encrypted_credentials)
    account_id = creds.get("account_id", "") or creds.get("netsuite_account_id", "")
    if not account_id:
        raise HTTPException(status_code=400, detail="Connection missing account_id.")

    access_token = await get_valid_token(db, connection)
    if not access_token:
        raise HTTPException(
            status_code=502,
            detail="OAuth token expired and refresh failed. Please re-authorize.",
        )

    return await execute_suiteql(access_token, account_id, query)


# ---------------------------------------------------------------------------
# Agent skill catalog schemas
# ---------------------------------------------------------------------------


class AgentSkillMetadata(BaseModel):
    name: str
    description: str
    triggers: list[str]
    slug: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/catalog", response_model=list[AgentSkillMetadata])
async def list_agent_skills(
    user: Annotated[User, Depends(get_current_user)],
):
    """Return lean metadata for all available agent skills (slash commands)."""
    from app.services.chat.skills import get_all_skills_metadata

    skills = get_all_skills_metadata()
    return [
        AgentSkillMetadata(
            name=s["name"],
            description=s["description"],
            triggers=s["triggers"],
            slug=s["slug"],
        )
        for s in skills
    ]


@router.get("", response_model=list[SavedQueryResponse])
async def list_saved_queries(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all saved queries for the tenant."""
    result = await db.execute(
        select(SavedSuiteQLQuery)
        .where(SavedSuiteQLQuery.tenant_id == user.tenant_id)
        .order_by(SavedSuiteQLQuery.created_at.desc())
    )
    queries = result.scalars().all()
    return [
        SavedQueryResponse(
            id=str(q.id),
            tenant_id=str(q.tenant_id),
            name=q.name,
            description=q.description,
            query_text=q.query_text,
            result_data=q.result_data,
            created_at=q.created_at.isoformat(),
        )
        for q in queries
    ]


@router.post("", response_model=SavedQueryResponse, status_code=status.HTTP_201_CREATED)
async def create_saved_query(
    request: SavedQueryCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    query = SavedSuiteQLQuery(
        tenant_id=user.tenant_id,
        name=request.name,
        description=request.description,
        query_text=request.query_text,
        result_data=request.result_data,
    )
    db.add(query)

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="skills",
        action="skills.query_create",
        actor_id=user.id,
        resource_type="saved_suiteql_query",
        resource_id=str(query.id),
    )
    await db.commit()
    await db.refresh(query)
    return SavedQueryResponse(
        id=str(query.id),
        tenant_id=str(query.tenant_id),
        name=query.name,
        description=query.description,
        query_text=query.query_text,
        created_at=query.created_at.isoformat(),
    )


@router.post("/preview", response_model=PreviewResponse)
async def preview_query(
    request: PreviewRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Execute a saved query with a 500-row limit for preview."""
    try:
        query_id = uuid.UUID(request.query_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query_id format.")

    saved_query = await get_saved_query(db, query_id, user.tenant_id)
    if not saved_query:
        raise HTTPException(status_code=404, detail="Saved query not found.")

    # If query is a snapshot (non-executable, e.g. financial reports), return stored data
    is_snapshot = saved_query.query_text.lstrip().startswith("--") or saved_query.result_data is not None
    if is_snapshot and saved_query.result_data:
        rd = saved_query.result_data
        rows = rd.get("rows", [])
        return PreviewResponse(
            columns=rd.get("columns", []),
            rows=rows[:500],
            row_count=rd.get("row_count", len(rows)),
            truncated=len(rows) > 500,
        )

    # Live execution for real SuiteQL queries
    limited_sql = inject_fetch_limit(saved_query.query_text, limit=500)
    result = await execute_suiteql_for_tenant(db=db, tenant_id=user.tenant_id, query=limited_sql)

    return PreviewResponse(
        columns=result.get("columns", []),
        rows=result.get("rows", []),
        row_count=result.get("row_count", 0),
        truncated=result.get("truncated", False),
    )


@router.post("/export", status_code=status.HTTP_202_ACCEPTED, response_model=ExportResponse)
async def trigger_export(
    request: ExportRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Trigger an async CSV export of a saved query."""
    from app.workers.tasks.suiteql_export import export_suiteql_to_csv

    try:
        query_id = uuid.UUID(request.query_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query_id format.")

    saved_query = await get_saved_query(db, query_id, user.tenant_id)
    if not saved_query:
        raise HTTPException(status_code=404, detail="Saved query not found.")

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="skills",
        action="skills.export_trigger",
        actor_id=user.id,
        resource_type="saved_suiteql_query",
        resource_id=str(saved_query.id),
    )
    await db.commit()

    # If snapshot data exists (financial reports), export directly without Celery
    is_snapshot = saved_query.query_text.lstrip().startswith("--") or saved_query.result_data is not None
    if is_snapshot and saved_query.result_data:
        import csv
        import io
        import os
        from datetime import datetime, timezone
        from pathlib import Path
        from uuid import uuid4

        rd = saved_query.result_data
        columns = rd.get("columns", [])
        rows = rd.get("rows", [])

        export_dir = Path(os.environ.get("EXPORT_DIR", "/tmp/exports"))
        export_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in saved_query.name)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{safe_name}_{timestamp}_{uuid4().hex[:8]}.csv"
        filepath = export_dir / filename

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows(rows)

        return ExportResponse(task_id=f"snapshot_{filename}", status="queued")

    task = export_suiteql_to_csv.delay(
        tenant_id=str(user.tenant_id),
        query_text=saved_query.query_text,
        query_name=saved_query.name,
    )

    return ExportResponse(task_id=task.id, status="queued")


@router.patch("/{query_id}", response_model=SavedQueryResponse)
async def update_saved_query_endpoint(
    query_id: uuid.UUID,
    request: SavedQueryUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update a saved query's name and/or description."""
    kwargs: dict = {}
    if request.name is not None:
        kwargs["name"] = request.name
    # Allow setting description to None (clearing it) or a new value
    kwargs["description"] = request.description

    updated = await update_saved_query(db, query_id, user.tenant_id, **kwargs)
    if not updated:
        raise HTTPException(status_code=404, detail="Saved query not found.")

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="skills",
        action="skills.query_update",
        actor_id=user.id,
        resource_type="saved_suiteql_query",
        resource_id=str(query_id),
    )
    await db.commit()
    await db.refresh(updated)
    return SavedQueryResponse(
        id=str(updated.id),
        tenant_id=str(updated.tenant_id),
        name=updated.name,
        description=updated.description,
        query_text=updated.query_text,
        created_at=updated.created_at.isoformat(),
    )


@router.delete("/{query_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_query_endpoint(
    query_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("skills.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a saved query by ID."""
    deleted = await delete_saved_query(db, query_id, user.tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved query not found.")

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="skills",
        action="skills.delete",
        actor_id=user.id,
        resource_type="saved_suiteql_query",
        resource_id=str(query_id),
    )
    await db.commit()
