"""API endpoints for SuiteScript file sync from NetSuite."""

from __future__ import annotations

import base64
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.connection import Connection
from app.models.user import User
from app.models.workspace import WorkspaceFile

router = APIRouter(prefix="/netsuite/scripts", tags=["netsuite-scripts"])


class PullPushRequest(BaseModel):
    workspace_id: uuid.UUID


@router.post("/sync")
async def trigger_script_sync(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Queue an async task to discover and load SuiteScript files from NetSuite."""
    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == user.tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        )
    )
    connection = result.scalar_one_or_none()
    if not connection:
        raise HTTPException(
            status_code=400,
            detail="No active NetSuite connection found. Please connect your NetSuite account first.",
        )

    from app.workers.tasks.suitescript_sync import netsuite_suitescript_sync

    task = netsuite_suitescript_sync.delay(
        tenant_id=str(user.tenant_id),
        connection_id=str(connection.id),
        user_id=str(user.id),
    )
    return {"task_id": task.id, "status": "queued"}


@router.get("/sync-status")
async def get_sync_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current SuiteScript sync state for the tenant."""
    from app.services.suitescript_sync_service import get_sync_status

    status = await get_sync_status(db, user.tenant_id)
    if status is None:
        return {"status": "not_started", "message": "SuiteScript sync has not been run yet."}

    return status


async def _get_netsuite_creds(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[Connection, str, str]:
    """Resolve NetSuite connection and get a valid token + account_id."""
    from app.core.encryption import decrypt_credentials
    from app.services.netsuite_oauth_service import get_valid_token

    result = await db.execute(
        select(Connection).where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        )
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
    return connection, access_token, account_id


def _resolve_workspace_file_id(raw: str) -> uuid.UUID:
    """Validate and parse a workspace file UUID from a path parameter."""
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID format.")


@router.post("/{workspace_file_id}/pull")
async def pull_single_file(
    workspace_file_id: str,
    body: PullPushRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pull a single file from NetSuite and update the workspace."""
    from app.services.netsuite_api_logger import log_netsuite_request
    from app.services.netsuite_client import _normalize_account_id

    ws_file_uuid = _resolve_workspace_file_id(workspace_file_id)
    connection, access_token, account_id = await _get_netsuite_creds(db, user.tenant_id)
    workspace_id = body.workspace_id

    # Look up the workspace file to get the NetSuite file ID
    result = await db.execute(
        select(WorkspaceFile).where(
            WorkspaceFile.id == ws_file_uuid,
            WorkspaceFile.workspace_id == workspace_id,
        )
    )
    workspace_file = result.scalar_one_or_none()
    if not workspace_file or not workspace_file.netsuite_file_id:
        raise HTTPException(
            status_code=404,
            detail="File not found or not linked to NetSuite.",
        )

    ns_file_id = workspace_file.netsuite_file_id
    slug = _normalize_account_id(account_id)
    url = f"https://{slug}.suitetalk.api.netsuite.com/services/rest/record/v1/file/{ns_file_id}"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        await log_netsuite_request(
            db=db,
            tenant_id=user.tenant_id,
            connection_id=connection.id,
            method="GET",
            url=url,
            response_time_ms=elapsed_ms,
            error_message=str(exc),
            source="single_file_pull",
        )
        await db.commit()
        raise HTTPException(status_code=502, detail=f"NetSuite API error: {str(exc)[:200]}")

    await log_netsuite_request(
        db=db,
        tenant_id=user.tenant_id,
        connection_id=connection.id,
        method="GET",
        url=url,
        response_status=resp.status_code,
        response_time_ms=elapsed_ms,
        source="single_file_pull",
    )

    data = resp.json()
    content_b64 = data.get("content", "")
    if not content_b64:
        raise HTTPException(status_code=404, detail="File has no content in NetSuite.")

    content = base64.b64decode(content_b64).decode("utf-8", errors="replace")

    # Update the workspace file content
    workspace_file.content = content
    workspace_file.size_bytes = len(content.encode("utf-8"))

    await db.commit()
    return {
        "status": "ok",
        "file_id": ns_file_id,
        "file_name": workspace_file.file_name,
        "size_bytes": len(content.encode("utf-8")),
    }


@router.post("/{workspace_file_id}/push")
async def push_single_file(
    workspace_file_id: str,
    body: PullPushRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Push workspace file content back to NetSuite via REST PATCH."""
    from app.services.netsuite_api_logger import log_netsuite_request
    from app.services.netsuite_client import _normalize_account_id

    ws_file_uuid = _resolve_workspace_file_id(workspace_file_id)
    connection, access_token, account_id = await _get_netsuite_creds(db, user.tenant_id)
    workspace_id = body.workspace_id

    # Look up the workspace file to get the NetSuite file ID
    result = await db.execute(
        select(WorkspaceFile).where(
            WorkspaceFile.id == ws_file_uuid,
            WorkspaceFile.workspace_id == workspace_id,
        )
    )
    workspace_file = result.scalar_one_or_none()
    if not workspace_file or not workspace_file.netsuite_file_id:
        raise HTTPException(
            status_code=404,
            detail="File not found or not linked to NetSuite.",
        )

    content = workspace_file.content
    if not content:
        raise HTTPException(status_code=400, detail="Workspace file has no content.")

    ns_file_id = workspace_file.netsuite_file_id
    content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

    slug = _normalize_account_id(account_id)
    url = f"https://{slug}.suitetalk.api.netsuite.com/services/rest/record/v1/file/{ns_file_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {"content": content_b64}

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.patch(url, headers=headers, json=payload)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        resp_status = getattr(getattr(exc, "response", None), "status_code", None)
        await log_netsuite_request(
            db=db,
            tenant_id=user.tenant_id,
            connection_id=connection.id,
            method="PATCH",
            url=url,
            request_body=f"[base64 content, {len(content)} bytes]",
            response_status=resp_status,
            response_time_ms=elapsed_ms,
            error_message=str(exc),
            source="single_file_push",
        )
        await db.commit()
        if resp_status == 405:
            detail = (
                "NetSuite REST API does not support file content updates via PATCH. "
                "A RESTlet deployment may be required for push functionality."
            )
        else:
            detail = f"NetSuite API error: {str(exc)[:200]}"
        raise HTTPException(status_code=502, detail=detail)

    await log_netsuite_request(
        db=db,
        tenant_id=user.tenant_id,
        connection_id=connection.id,
        method="PATCH",
        url=url,
        response_status=resp.status_code,
        response_time_ms=elapsed_ms,
        source="single_file_push",
    )
    await db.commit()

    return {
        "status": "ok",
        "file_id": ns_file_id,
        "file_name": workspace_file.file_name,
        "pushed_bytes": len(content.encode("utf-8")),
    }
