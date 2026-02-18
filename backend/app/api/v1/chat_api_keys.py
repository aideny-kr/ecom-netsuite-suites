import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_entitlement, require_permission
from app.models.user import User
from app.schemas.chat_api_key import ChatApiKeyCreate, ChatApiKeyCreated, ChatApiKeyResponse
from app.services import chat_api_key_service

router = APIRouter(prefix="/chat-api-keys", tags=["chat-api-keys"])


def _serialize_key(k) -> dict:
    return {
        "id": str(k.id),
        "tenant_id": str(k.tenant_id),
        "name": k.name,
        "key_prefix": k.key_prefix,
        "scopes": k.scopes,
        "rate_limit_per_minute": k.rate_limit_per_minute,
        "is_active": k.is_active,
        "expires_at": k.expires_at,
        "created_by": str(k.created_by) if k.created_by else None,
        "last_used_at": k.last_used_at,
        "created_at": k.created_at,
    }


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ChatApiKeyCreated)
async def create_api_key(
    body: ChatApiKeyCreate,
    user: User = Depends(require_permission("chat_api.manage")),
    _ent: User = Depends(require_entitlement("chat_api")),
    db: AsyncSession = Depends(get_db),
):
    api_key, raw_key = await chat_api_key_service.create_key(
        db=db,
        tenant_id=user.tenant_id,
        name=body.name,
        scopes=body.scopes,
        user_id=user.id,
        rate_limit_per_minute=body.rate_limit_per_minute,
        expires_at=body.expires_at,
    )
    await db.commit()
    return {
        "id": str(api_key.id),
        "name": api_key.name,
        "key_prefix": api_key.key_prefix,
        "raw_key": raw_key,
        "scopes": api_key.scopes,
        "rate_limit_per_minute": api_key.rate_limit_per_minute,
        "expires_at": api_key.expires_at,
        "created_at": api_key.created_at,
    }


@router.get("", response_model=list[ChatApiKeyResponse])
async def list_api_keys(
    user: User = Depends(require_permission("chat_api.manage")),
    db: AsyncSession = Depends(get_db),
):
    keys = await chat_api_key_service.list_keys(db, user.tenant_id)
    return [_serialize_key(k) for k in keys]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: uuid.UUID,
    user: User = Depends(require_permission("chat_api.manage")),
    db: AsyncSession = Depends(get_db),
):
    try:
        await chat_api_key_service.revoke_key(
            db=db,
            key_id=key_id,
            tenant_id=user.tenant_id,
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    await db.commit()
