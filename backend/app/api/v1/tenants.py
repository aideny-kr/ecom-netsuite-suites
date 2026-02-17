import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_permission
from app.core.encryption import encrypt_credentials, get_current_key_version
from app.models.tenant import Tenant, TenantConfig
from app.models.user import User
from app.schemas.tenant import (
    AiKeyTestRequest,
    AiKeyTestResponse,
    PlanInfoResponse,
    PlanLimits,
    PlanUsage,
    TenantConfigResponse,
    TenantConfigUpdate,
    TenantResponse,
    TenantUpdate,
)
from app.services.chat.llm_adapter import DEFAULT_MODELS, VALID_MODELS, get_adapter
from app.services.entitlement_service import get_plan_limits, get_usage_summary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("/me", response_model=TenantResponse)
async def get_tenant(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantResponse(
        id=str(tenant.id),
        name=tenant.name,
        slug=tenant.slug,
        plan=tenant.plan,
        plan_expires_at=tenant.plan_expires_at,
        is_active=tenant.is_active,
    )


@router.get("/me/plan", response_model=PlanInfoResponse)
async def get_tenant_plan(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    limits = await get_plan_limits(db, tenant.id)
    usage = await get_usage_summary(db, tenant.id)

    return PlanInfoResponse(
        plan=tenant.plan,
        limits=PlanLimits(**limits),
        usage=PlanUsage(**usage),
        plan_expires_at=tenant.plan_expires_at,
    )


@router.patch("/me", response_model=TenantResponse)
async def update_tenant(
    update: TenantUpdate,
    user: Annotated[User, Depends(require_permission("tenant.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if update.name is not None:
        tenant.name = update.name

    await db.commit()
    await db.refresh(tenant)
    return TenantResponse(
        id=str(tenant.id),
        name=tenant.name,
        slug=tenant.slug,
        plan=tenant.plan,
        plan_expires_at=tenant.plan_expires_at,
        is_active=tenant.is_active,
    )


@router.get("/me/config", response_model=TenantConfigResponse)
async def get_tenant_config(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == user.tenant_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Tenant config not found")
    return _build_config_response(config)


def _build_config_response(config: TenantConfig) -> TenantConfigResponse:
    return TenantConfigResponse(
        id=str(config.id),
        tenant_id=str(config.tenant_id),
        subsidiaries=config.subsidiaries,
        account_mappings=config.account_mappings,
        posting_mode=config.posting_mode,
        posting_batch_size=config.posting_batch_size,
        posting_attach_evidence=config.posting_attach_evidence,
        netsuite_account_id=config.netsuite_account_id,
        ai_provider=config.ai_provider,
        ai_model=config.ai_model,
        ai_api_key_set=bool(config.ai_api_key_encrypted),
    )


@router.patch("/me/config", response_model=TenantConfigResponse)
async def update_tenant_config(
    update: TenantConfigUpdate,
    user: Annotated[User, Depends(require_permission("tenant.manage"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == user.tenant_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Tenant config not found")

    update_data = update.model_dump(exclude_unset=True)

    # Validate provider/model combination
    ai_provider = update_data.get("ai_provider", config.ai_provider)
    ai_model = update_data.get("ai_model")
    if ai_model and ai_provider:
        provider_models = VALID_MODELS.get(ai_provider, [])
        if ai_model not in provider_models:
            raise HTTPException(
                status_code=422,
                detail=f"Model '{ai_model}' is not valid for provider '{ai_provider}'",
            )

    # Handle AI API key — encrypt before storage
    raw_ai_key = update_data.pop("ai_api_key", None)
    if raw_ai_key is not None:
        config.ai_api_key_encrypted = encrypt_credentials({"api_key": raw_ai_key})
        config.ai_key_version = get_current_key_version()

    # If clearing provider, also clear model and key
    if "ai_provider" in update_data and update_data["ai_provider"] is None:
        config.ai_provider = None
        config.ai_model = None
        config.ai_api_key_encrypted = None
        update_data.pop("ai_provider")
        update_data.pop("ai_model", None)

    for key, value in update_data.items():
        setattr(config, key, value)

    await db.commit()
    await db.refresh(config)
    return _build_config_response(config)


@router.post("/me/config/test-ai-key", response_model=AiKeyTestResponse)
async def test_ai_key(
    body: AiKeyTestRequest,
    user: Annotated[User, Depends(require_permission("tenant.manage"))],
):
    """Test an AI provider API key by sending a trivial message."""
    model = body.model or DEFAULT_MODELS.get(body.provider, "")
    try:
        adapter = get_adapter(body.provider, body.api_key)
        await adapter.create_message(
            model=model,
            max_tokens=5,
            system="You are a test assistant.",
            messages=[{"role": "user", "content": "Say hi"}],
        )
        return AiKeyTestResponse(valid=True)
    except Exception as exc:
        logger.info("AI key test failed for provider=%s: %s", body.provider, exc)
        return AiKeyTestResponse(valid=False, error=str(exc))
