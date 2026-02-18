import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_entitlement, require_permission
from app.models.user import User
from app.schemas.policy_profile import PolicyProfileCreate, PolicyProfileResponse, PolicyProfileUpdate
from app.services import policy_service

router = APIRouter(prefix="/policies", tags=["policies"])


def _serialize_policy(p) -> dict:
    return {
        "id": str(p.id),
        "tenant_id": str(p.tenant_id),
        "version": p.version,
        "name": p.name,
        "sensitivity_default": p.sensitivity_default,
        "is_active": p.is_active,
        "is_locked": p.is_locked,
        "read_only_mode": p.read_only_mode,
        "allowed_record_types": p.allowed_record_types,
        "blocked_fields": p.blocked_fields,
        "tool_allowlist": p.tool_allowlist,
        "max_rows_per_query": p.max_rows_per_query,
        "require_row_limit": p.require_row_limit,
        "custom_rules": p.custom_rules,
        "created_by": str(p.created_by) if p.created_by else None,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PolicyProfileResponse)
async def create_policy(
    body: PolicyProfileCreate,
    user: User = Depends(require_permission("policy.manage")),
    _ent: User = Depends(require_entitlement("policies")),
    db: AsyncSession = Depends(get_db),
):
    policy = await policy_service.create_policy(
        db=db,
        tenant_id=user.tenant_id,
        data=body.model_dump(exclude_none=True),
        user_id=user.id,
    )
    await db.commit()
    return _serialize_policy(policy)


@router.get("", response_model=list[PolicyProfileResponse])
async def list_policies(
    user: User = Depends(require_permission("policy.view")),
    db: AsyncSession = Depends(get_db),
):
    policies = await policy_service.list_policies(db, user.tenant_id)
    return [_serialize_policy(p) for p in policies]


@router.get("/{policy_id}", response_model=PolicyProfileResponse)
async def get_policy(
    policy_id: uuid.UUID,
    user: User = Depends(require_permission("policy.view")),
    db: AsyncSession = Depends(get_db),
):
    policy = await policy_service.get_policy(db, user.tenant_id, policy_id)
    if not policy:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")
    return _serialize_policy(policy)


@router.put("/{policy_id}", response_model=PolicyProfileResponse)
async def update_policy(
    policy_id: uuid.UUID,
    body: PolicyProfileUpdate,
    user: User = Depends(require_permission("policy.manage")),
    db: AsyncSession = Depends(get_db),
):
    try:
        policy = await policy_service.update_policy(
            db=db,
            tenant_id=user.tenant_id,
            policy_id=policy_id,
            data=body.model_dump(exclude_none=True),
            user_id=user.id,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = status.HTTP_409_CONFLICT if "locked" in detail.lower() else status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=detail)
    await db.commit()
    return _serialize_policy(policy)


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: uuid.UUID,
    user: User = Depends(require_permission("policy.manage")),
    db: AsyncSession = Depends(get_db),
):
    try:
        await policy_service.delete_policy(
            db=db,
            tenant_id=user.tenant_id,
            policy_id=policy_id,
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    await db.commit()
