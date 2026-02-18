import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_entitlement, require_permission
from app.models.user import User
from app.schemas.prompt_template import PromptTemplatePreview, PromptTemplateResponse
from app.schemas.tenant_profile import TenantProfileCreate, TenantProfileResponse
from app.services import onboarding_service, prompt_template_service

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


def _serialize_profile(p) -> dict:
    return {
        "id": str(p.id),
        "tenant_id": str(p.tenant_id),
        "version": p.version,
        "status": p.status,
        "industry": p.industry,
        "business_description": p.business_description,
        "netsuite_account_id": p.netsuite_account_id,
        "chart_of_accounts": p.chart_of_accounts,
        "subsidiaries": p.subsidiaries,
        "item_types": p.item_types,
        "custom_segments": p.custom_segments,
        "fiscal_calendar": p.fiscal_calendar,
        "suiteql_naming": p.suiteql_naming,
        "confirmed_by": str(p.confirmed_by) if p.confirmed_by else None,
        "confirmed_at": p.confirmed_at,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _serialize_template(t) -> dict:
    return {
        "id": str(t.id),
        "tenant_id": str(t.tenant_id),
        "version": t.version,
        "profile_id": str(t.profile_id),
        "policy_id": str(t.policy_id) if t.policy_id else None,
        "template_text": t.template_text,
        "sections": t.sections,
        "is_active": t.is_active,
        "generated_at": t.generated_at,
        "created_at": t.created_at,
    }


@router.post("/profiles", status_code=status.HTTP_201_CREATED, response_model=TenantProfileResponse)
async def create_profile(
    body: TenantProfileCreate,
    user: User = Depends(require_permission("onboarding.manage")),
    _ent: User = Depends(require_entitlement("onboarding")),
    db: AsyncSession = Depends(get_db),
):
    profile = await onboarding_service.create_profile(
        db=db,
        tenant_id=user.tenant_id,
        data=body.model_dump(exclude_none=True),
        user_id=user.id,
    )
    await db.commit()
    return _serialize_profile(profile)


@router.get("/profiles", response_model=list[TenantProfileResponse])
async def list_profiles(
    user: User = Depends(require_permission("onboarding.view")),
    db: AsyncSession = Depends(get_db),
):
    profiles = await onboarding_service.list_profiles(db, user.tenant_id)
    return [_serialize_profile(p) for p in profiles]


@router.get("/profiles/active", response_model=TenantProfileResponse)
async def get_active_profile(
    user: User = Depends(require_permission("onboarding.view")),
    db: AsyncSession = Depends(get_db),
):
    profile = await onboarding_service.get_active_profile(db, user.tenant_id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active profile found")
    return _serialize_profile(profile)


@router.get("/profiles/{profile_id}", response_model=TenantProfileResponse)
async def get_profile(
    profile_id: uuid.UUID,
    user: User = Depends(require_permission("onboarding.view")),
    db: AsyncSession = Depends(get_db),
):
    profile = await onboarding_service.get_profile(db, user.tenant_id, profile_id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return _serialize_profile(profile)


@router.post("/profiles/{profile_id}/confirm", response_model=TenantProfileResponse)
async def confirm_profile(
    profile_id: uuid.UUID,
    user: User = Depends(require_permission("onboarding.manage")),
    db: AsyncSession = Depends(get_db),
):
    try:
        profile = await onboarding_service.confirm_profile(
            db=db,
            tenant_id=user.tenant_id,
            profile_id=profile_id,
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    await db.commit()
    return _serialize_profile(profile)


@router.post("/discover")
async def discover_netsuite_metadata(
    user: User = Depends(require_permission("onboarding.manage")),
    _ent: User = Depends(require_entitlement("onboarding")),
    db: AsyncSession = Depends(get_db),
):
    metadata = await onboarding_service.discover_netsuite_metadata(db, user.tenant_id)
    return metadata


@router.get("/prompt-template", response_model=PromptTemplateResponse)
async def get_prompt_template(
    user: User = Depends(require_permission("onboarding.view")),
    db: AsyncSession = Depends(get_db),
):
    template = await prompt_template_service.get_active_template_obj(db, user.tenant_id)
    if not template:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active prompt template")
    return _serialize_template(template)


@router.get("/prompt-template/preview", response_model=PromptTemplatePreview)
async def preview_prompt_template(
    user: User = Depends(require_permission("onboarding.view")),
    db: AsyncSession = Depends(get_db),
):
    """Preview prompt template from the latest draft profile."""
    from sqlalchemy import select

    from app.models.tenant_profile import TenantProfile

    result = await db.execute(
        select(TenantProfile)
        .where(
            TenantProfile.tenant_id == user.tenant_id,
            TenantProfile.status == "draft",
        )
        .order_by(TenantProfile.version.desc())
        .limit(1)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No draft profile to preview")

    from app.services.policy_service import get_active_policy

    policy = await get_active_policy(db, user.tenant_id)
    template_text, sections = prompt_template_service.generate_template(profile, policy)
    return {"template_text": template_text, "sections": sections}
