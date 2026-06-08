"""Learned-rules management API — list/create/update/delete tenant semantic rules.

Admin-gated (tenant.manage). These rules are injected into every chat turn's
prompt, so a bad rule silently skews analytics — this surface lets admins see
and prune them (a broken rule previously rotted unseen for 3 months).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.models.tenant_learned_rule import TenantLearnedRule
from app.models.user import User
from app.schemas.learned_rule import LearnedRuleCreate, LearnedRuleResponse, LearnedRuleUpdate
from app.services import audit_service, learned_rule_service

router = APIRouter(prefix="/learned-rules", tags=["learned-rules"])

_AdminUser = Annotated[User, Depends(require_permission("tenant.manage"))]
_Db = Annotated[AsyncSession, Depends(get_db)]


def _to_response(rule: TenantLearnedRule) -> LearnedRuleResponse:
    return LearnedRuleResponse(
        id=str(rule.id),
        tenant_id=str(rule.tenant_id),
        rule_category=rule.rule_category,
        rule_description=rule.rule_description,
        is_active=rule.is_active,
        created_by=str(rule.created_by) if rule.created_by else None,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


@router.get("", response_model=list[LearnedRuleResponse])
async def list_learned_rules(user: _AdminUser, db: _Db):
    rules = await learned_rule_service.list_rules(db, user.tenant_id)
    return [_to_response(r) for r in rules]


@router.post("", response_model=LearnedRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_learned_rule(request: LearnedRuleCreate, user: _AdminUser, db: _Db):
    rule = await learned_rule_service.create_rule(
        db,
        user.tenant_id,
        rule_description=request.rule_description,
        rule_category=request.rule_category,
        created_by=user.id,
    )
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="learned_rule",
        action="learned_rule.create",
        actor_id=user.id,
        resource_type="learned_rule",
        resource_id=str(rule.id),
        payload={"category": rule.rule_category},
    )
    await db.commit()
    await db.refresh(rule)
    return _to_response(rule)


@router.patch("/{rule_id}", response_model=LearnedRuleResponse)
async def update_learned_rule(rule_id: uuid.UUID, request: LearnedRuleUpdate, user: _AdminUser, db: _Db):
    rule = await learned_rule_service.update_rule(
        db,
        user.tenant_id,
        rule_id,
        rule_description=request.rule_description,
        rule_category=request.rule_category,
        is_active=request.is_active,
    )
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learned rule not found")
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="learned_rule",
        action="learned_rule.update",
        actor_id=user.id,
        resource_type="learned_rule",
        resource_id=str(rule_id),
        payload=request.model_dump(exclude_none=True),
    )
    await db.commit()
    await db.refresh(rule)
    return _to_response(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_learned_rule(rule_id: uuid.UUID, user: _AdminUser, db: _Db):
    ok = await learned_rule_service.delete_rule(db, user.tenant_id, rule_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learned rule not found")
    await audit_service.log_event(
        db=db,
        tenant_id=user.tenant_id,
        category="learned_rule",
        action="learned_rule.delete",
        actor_id=user.id,
        resource_type="learned_rule",
        resource_id=str(rule_id),
    )
    await db.commit()
    return None
