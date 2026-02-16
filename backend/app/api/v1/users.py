import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.security import hash_password
from app.models.user import Role, User, UserRole
from app.schemas.user import UserCreate, UserResponse, UserRoleAssign
from app.services import audit_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserResponse])
async def list_users(
    user: Annotated[User, Depends(require_permission("users.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(User)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .where(User.tenant_id == user.tenant_id)
        .order_by(User.created_at.desc())
    )
    users = result.scalars().all()
    return [
        UserResponse(
            id=str(u.id),
            tenant_id=str(u.tenant_id),
            email=u.email,
            full_name=u.full_name,
            actor_type=u.actor_type,
            is_active=u.is_active,
            roles=[ur.role.name for ur in u.user_roles],
        )
        for u in users
    ]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    request: UserCreate,
    user: Annotated[User, Depends(require_permission("users.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    # Check for existing user with same email in tenant
    existing = await db.execute(select(User).where(User.tenant_id == user.tenant_id, User.email == request.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="User with this email already exists")

    new_user = User(
        tenant_id=user.tenant_id,
        email=request.email,
        hashed_password=hash_password(request.password),
        full_name=request.full_name,
    )
    db.add(new_user)
    await db.flush()

    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="user",
        action="user.create",
        actor_id=user.id,
        resource_type="user",
        resource_id=str(new_user.id),
    )
    await db.commit()
    return UserResponse(
        id=str(new_user.id),
        tenant_id=str(new_user.tenant_id),
        email=new_user.email,
        full_name=new_user.full_name,
        actor_type=new_user.actor_type,
        is_active=new_user.is_active,
        roles=[],
    )


@router.patch("/{user_id}/roles", response_model=UserResponse)
async def assign_roles(
    user_id: uuid.UUID,
    request: UserRoleAssign,
    user: Annotated[User, Depends(require_permission("users.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(User)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .where(User.id == user_id, User.tenant_id == user.tenant_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    # Remove existing roles
    for ur in target_user.user_roles:
        await db.delete(ur)

    # Add new roles
    for role_name in request.role_names:
        role_result = await db.execute(select(Role).where(Role.name == role_name))
        role = role_result.scalar_one_or_none()
        if not role:
            raise HTTPException(status_code=400, detail=f"Role '{role_name}' not found")
        db.add(UserRole(tenant_id=user.tenant_id, user_id=target_user.id, role_id=role.id))

    await db.commit()

    # Reload
    result = await db.execute(
        select(User).options(selectinload(User.user_roles).selectinload(UserRole.role)).where(User.id == user_id)
    )
    target_user = result.scalar_one()
    return UserResponse(
        id=str(target_user.id),
        tenant_id=str(target_user.tenant_id),
        email=target_user.email,
        full_name=target_user.full_name,
        actor_type=target_user.actor_type,
        is_active=target_user.is_active,
        roles=[ur.role.name for ur in target_user.user_roles],
    )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_user(
    user_id: uuid.UUID,
    user: Annotated[User, Depends(require_permission("users.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.id == user_id, User.tenant_id == user.tenant_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    target_user.is_active = False
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="user",
        action="user.deactivate",
        actor_id=user.id,
        resource_type="user",
        resource_id=str(target_user.id),
    )
    await db.commit()
