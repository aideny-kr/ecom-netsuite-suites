import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.user import User
from app.schemas.connection import (
    ConnectionCreate,
    ConnectionResponse,
    ConnectionTestResponse,
    ConnectionUpdate,
)
from app.services import audit_service, connection_service, entitlement_service

router = APIRouter(prefix="/connections", tags=["connections"])


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    user: Annotated[User, Depends(require_permission("connections.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    connections = await connection_service.list_connections(db, user.tenant_id)
    return [
        ConnectionResponse(
            id=str(c.id),
            tenant_id=str(c.tenant_id),
            provider=c.provider,
            label=c.label,
            status=c.status,
            auth_type=c.auth_type,
            encryption_key_version=c.encryption_key_version,
            metadata_json=c.metadata_json,
            created_at=c.created_at,
            created_by=str(c.created_by) if c.created_by else None,
        )
        for c in connections
    ]


@router.post("", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_connection(
    request: ConnectionCreate,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    # Check entitlement
    allowed = await entitlement_service.check_entitlement(db, user.tenant_id, "connections")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Connection limit reached for your plan",
        )

    connection = await connection_service.create_connection(
        db=db,
        tenant_id=user.tenant_id,
        provider=request.provider,
        label=request.label,
        credentials=request.credentials,
        created_by=user.id,
    )

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="connection.create",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection.id),
        payload={"provider": request.provider, "label": request.label},
    )
    await db.commit()
    await db.refresh(connection)

    return ConnectionResponse(
        id=str(connection.id),
        tenant_id=str(connection.tenant_id),
        provider=connection.provider,
        label=connection.label,
        status=connection.status,
        auth_type=connection.auth_type,
        encryption_key_version=connection.encryption_key_version,
        metadata_json=connection.metadata_json,
        created_at=connection.created_at,
        created_by=str(connection.created_by) if connection.created_by else None,
    )


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    deleted = await connection_service.delete_connection(db, connection_id, user.tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Connection not found")

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="connection.delete",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection_id),
    )
    await db.commit()


@router.patch("/{connection_id}", response_model=ConnectionResponse)
async def update_connection(
    connection_id: uuid.UUID,
    request: ConnectionUpdate,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    connection = await connection_service.get_connection(db, connection_id, user.tenant_id)
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    if request.label is not None:
        connection.label = request.label
    if request.auth_type is not None:
        connection.auth_type = request.auth_type

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="connection.update",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection_id),
        payload={"label": request.label, "auth_type": request.auth_type},
    )
    await db.commit()
    await db.refresh(connection)

    return ConnectionResponse(
        id=str(connection.id),
        tenant_id=str(connection.tenant_id),
        provider=connection.provider,
        label=connection.label,
        status=connection.status,
        auth_type=connection.auth_type,
        encryption_key_version=connection.encryption_key_version,
        metadata_json=connection.metadata_json,
        created_at=connection.created_at,
        created_by=str(connection.created_by) if connection.created_by else None,
    )


@router.post("/{connection_id}/reconnect", response_model=ConnectionResponse)
async def reconnect_connection(
    connection_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    connection = await connection_service.get_connection(db, connection_id, user.tenant_id)
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    connection.status = "active"

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="connection",
        action="connection.reconnect",
        actor_id=user.id,
        resource_type="connection",
        resource_id=str(connection_id),
    )
    await db.commit()
    await db.refresh(connection)

    return ConnectionResponse(
        id=str(connection.id),
        tenant_id=str(connection.tenant_id),
        provider=connection.provider,
        label=connection.label,
        status=connection.status,
        auth_type=connection.auth_type,
        encryption_key_version=connection.encryption_key_version,
        metadata_json=connection.metadata_json,
        created_at=connection.created_at,
        created_by=str(connection.created_by) if connection.created_by else None,
    )


@router.post("/{connection_id}/test", response_model=ConnectionTestResponse)
async def test_connection(
    connection_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("connections.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await connection_service.test_connection(db, connection_id, user.tenant_id)
    return ConnectionTestResponse(**result)
