"""Excel and CSV export endpoints."""

import csv
import io
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.services import audit_service
from app.services.excel_export_service import generate_excel

router = APIRouter(prefix="/exports", tags=["exports"])

EXPORT_DIR = Path(os.environ.get("EXPORT_DIR", "/tmp/exports"))


class ExcelExportRequest(BaseModel):
    columns: list[str] = Field(min_length=1)
    rows: list[list[Any]]
    title: str = "Query Results"
    metadata: dict[str, str] | None = None
    column_types: dict[str, str] | None = None


class QueryExportRequest(BaseModel):
    query_text: str = Field(min_length=1)
    title: str = "Query Results"
    format: Literal["xlsx", "csv"] = "xlsx"
    metadata: dict[str, str] | None = None
    column_types: dict[str, str] | None = None


async def _get_netsuite_connection(db: AsyncSession, tenant_id):
    """Resolve active NetSuite connection for a tenant."""
    from sqlalchemy import select as sa_select
    from app.models.connection import Connection

    result = await db.execute(
        sa_select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        ).limit(1)
    )
    return result.scalar_one_or_none()


@router.post("/excel")
async def export_excel(
    request: ExcelExportRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Generate and download a formatted Excel file from query results."""
    if len(request.rows) > 50_000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Excel export limited to 50,000 rows. Use CSV for larger datasets.",
        )

    buffer = generate_excel(
        columns=request.columns,
        rows=request.rows,
        title=request.title,
        metadata=request.metadata,
        column_types=request.column_types,
    )

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="export",
        action="export.excel",
        actor_id=user.id,
        resource_type="query_result",
    )
    await db.commit()

    safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in request.title)[:50]
    filename = f"{safe_title}.xlsx"

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/query-export")
async def query_export(
    request: QueryExportRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Re-execute a SuiteQL query with full pagination and export."""
    from app.mcp.tools.netsuite_suiteql import is_read_only_sql
    from app.services.netsuite_client import execute_suiteql_via_rest
    from app.services.netsuite_oauth_service import get_valid_token
    from app.core.encryption import decrypt_credentials

    # Validate read-only
    if not is_read_only_sql(request.query_text):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only SELECT queries are allowed for export.",
        )

    # Resolve connection
    connection = await _get_netsuite_connection(db, user.tenant_id)
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active NetSuite connection. Connect your NetSuite account first.",
        )

    access_token = await get_valid_token(db, connection)
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth token expired. Please re-authorize.",
        )

    creds = decrypt_credentials(connection.encrypted_credentials)
    account_id = (creds.get("account_id") or "").replace("_", "-").lower()

    # Preserve the original FETCH FIRST limit if present — user expects
    # the same row count they saw in the chat. Only use 50K as a safety cap.
    result = await execute_suiteql_via_rest(
        access_token, account_id, request.query_text.strip(),
        limit=50_000, paginate=True, timeout_seconds=120,
    )

    columns = result.get("columns", [])
    rows = result.get("rows", [])

    action = f"export.query_{request.format}"
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="export",
        action=action,
        actor_id=user.id,
        resource_type="query_result",
    )
    await db.commit()

    safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in request.title)[:50]

    if request.format == "xlsx":
        buffer = generate_excel(
            columns=columns,
            rows=rows,
            title=request.title,
            metadata={**(request.metadata or {}), "Total Rows": str(len(rows))},
            column_types=request.column_types,
        )
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{safe_title}.xlsx"'},
        )
    else:
        # CSV format
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(columns)
        writer.writerows(rows)
        content = output.getvalue().encode("utf-8")
        return StreamingResponse(
            io.BytesIO(content),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{safe_title}.csv"'},
        )


@router.get("/{file_name}")
async def download_export(
    file_name: str,
    user: Annotated[User, Depends(get_current_user)],
):
    """Download a previously generated export file."""
    # Security: prevent path traversal
    if ".." in file_name or "/" in file_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename")

    file_path = EXPORT_DIR / file_name
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export file not found")

    # Auto-detect content type
    if file_name.endswith(".xlsx"):
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        media_type = "text/csv"

    return FileResponse(file_path, media_type=media_type, filename=file_name)
