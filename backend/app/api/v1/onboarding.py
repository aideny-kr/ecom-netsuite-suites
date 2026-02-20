import logging
import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user, require_entitlement, require_permission
from app.models.chat import ChatSession
from app.models.tenant import TenantConfig
from app.models.user import User
from app.schemas.onboarding_wizard import (
    AuditTrailEvent,
    AuditTrailResponse,
    ChecklistItemResponse,
    ChecklistResponse,
    FinalizeResponse,
    SetupPolicyRequest,
    StepCompleteRequest,
    StepValidationResponse,
)
from app.schemas.prompt_template import PromptTemplatePreview, PromptTemplateResponse
from app.schemas.tenant_profile import TenantProfileCreate, TenantProfileResponse
from app.services import onboarding_service, onboarding_wizard_service, policy_service, prompt_template_service
from app.services.chat.orchestrator import run_chat_turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


def _serialize_profile(p) -> dict:
    return {
        "id": str(p.id),
        "tenant_id": str(p.tenant_id),
        "version": p.version,
        "status": p.status,
        "industry": p.industry,
        "team_size": p.team_size,
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
    metadata = await onboarding_service.discover_netsuite_metadata(db, user.tenant_id, user.id)
    await db.commit()
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


# --- Chat-based onboarding endpoints ---


class OnboardingChatStartResponse(BaseModel):
    session_id: str
    message: dict


class OnboardingStatusResponse(BaseModel):
    completed: bool
    completed_at: str | None = None
    session_id: str | None = None


@router.post("/chat/start", status_code=status.HTTP_201_CREATED, response_model=OnboardingChatStartResponse)
async def start_onboarding_chat(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an onboarding chat session and return the AI greeting.

    If an onboarding session already exists for this user, returns it instead of creating a new one.
    """
    # Check for existing onboarding session
    existing = await db.execute(
        select(ChatSession).where(
            ChatSession.tenant_id == user.tenant_id,
            ChatSession.user_id == user.id,
            ChatSession.session_type == "onboarding",
        )
    )
    existing_session = existing.scalar_one_or_none()
    if existing_session:
        # Return existing session with its first assistant message
        first_msg = None
        for msg in existing_session.messages:
            if msg.role == "assistant":
                first_msg = msg
                break
        if first_msg:
            return OnboardingChatStartResponse(
                session_id=str(existing_session.id),
                message={
                    "id": str(first_msg.id),
                    "role": first_msg.role,
                    "content": first_msg.content,
                    "created_at": first_msg.created_at.isoformat(),
                },
            )

    # Create new onboarding session
    session = ChatSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        title="Onboarding",
        session_type="onboarding",
    )
    db.add(session)
    await db.flush()

    # Trigger the first AI greeting by sending a system-initiated message
    try:
        assistant_msg = await run_chat_turn(
            db=db,
            session=session,
            user_message="Hello! I just signed up and I'm ready to set up my account.",
            user_id=user.id,
            tenant_id=user.tenant_id,
        )
    except Exception:
        logger.exception("Failed to start onboarding chat")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to start onboarding chat. Please try again.",
        )

    return OnboardingChatStartResponse(
        session_id=str(session.id),
        message={
            "id": str(assistant_msg.id),
            "role": assistant_msg.role,
            "content": assistant_msg.content,
            "created_at": assistant_msg.created_at.isoformat(),
        },
    )


@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if the current tenant has completed onboarding."""
    config_result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == user.tenant_id))
    config = config_result.scalar_one_or_none()

    completed = config.onboarding_completed_at is not None if config else False
    completed_at = config.onboarding_completed_at.isoformat() if config and config.onboarding_completed_at else None

    # Find existing onboarding session
    session_result = await db.execute(
        select(ChatSession.id).where(
            ChatSession.tenant_id == user.tenant_id,
            ChatSession.user_id == user.id,
            ChatSession.session_type == "onboarding",
        )
    )
    session_row = session_result.first()
    session_id = str(session_row[0]) if session_row else None

    return OnboardingStatusResponse(
        completed=completed,
        completed_at=completed_at,
        session_id=session_id,
    )


# --- Wizard Checklist Endpoints ---


@router.get("/checklist", response_model=ChecklistResponse)
async def get_checklist(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items = await onboarding_wizard_service.get_checklist(db, user.tenant_id)
    config_result = await db.execute(select(TenantConfig).where(TenantConfig.tenant_id == user.tenant_id))
    config = config_result.scalar_one_or_none()
    finalized_at = config.onboarding_completed_at.isoformat() if config and config.onboarding_completed_at else None

    return ChecklistResponse(
        items=[
            ChecklistItemResponse(
                step_key=i.step_key,
                status=i.status,
                completed_at=i.completed_at.isoformat() if i.completed_at else None,
                completed_by=str(i.completed_by) if i.completed_by else None,
                metadata=i.metadata_,
            )
            for i in items
        ],
        all_completed=all(i.status in ("completed", "skipped") for i in items),
        finalized_at=finalized_at,
    )


@router.post("/checklist/{step_key}/complete", response_model=ChecklistItemResponse)
async def complete_step(
    step_key: str,
    body: StepCompleteRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.onboarding_checklist import STEP_KEYS

    if step_key not in STEP_KEYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid step_key: {step_key}")

    # Validate step before marking complete
    validation = await onboarding_wizard_service.validate_step(db, user.tenant_id, step_key)
    if not validation["valid"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=validation.get("reason", "Step requirements not met"),
        )

    metadata = body.metadata if body else None
    if step_key == "connection":
        if not isinstance(metadata, dict) or metadata.get("discovery_status") != "completed":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Connection step requires a completed discovery run",
            )

    item = await onboarding_wizard_service.complete_step(
        db,
        user.tenant_id,
        step_key,
        user.id,
        metadata=metadata,
    )
    await db.commit()
    return ChecklistItemResponse(
        step_key=item.step_key,
        status=item.status,
        completed_at=item.completed_at.isoformat() if item.completed_at else None,
        completed_by=str(item.completed_by) if item.completed_by else None,
        metadata=item.metadata_,
    )


@router.post("/checklist/{step_key}/skip", response_model=ChecklistItemResponse)
async def skip_step(
    step_key: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.onboarding_checklist import STEP_KEYS

    if step_key not in STEP_KEYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid step_key: {step_key}")

    item = await onboarding_wizard_service.skip_step(db, user.tenant_id, step_key, user.id)
    await db.commit()
    return ChecklistItemResponse(
        step_key=item.step_key,
        status=item.status,
        completed_at=item.completed_at.isoformat() if item.completed_at else None,
        completed_by=str(item.completed_by) if item.completed_by else None,
        metadata=item.metadata_,
    )


@router.get("/checklist/{step_key}/validate", response_model=StepValidationResponse)
async def validate_step(
    step_key: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.onboarding_checklist import STEP_KEYS

    if step_key not in STEP_KEYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid step_key: {step_key}")

    result = await onboarding_wizard_service.validate_step(db, user.tenant_id, step_key)
    return StepValidationResponse(**result)


@router.post("/setup-policy", response_model=ChecklistItemResponse)
async def setup_policy(
    body: SetupPolicyRequest,
    user: User = Depends(require_permission("onboarding.manage")),
    db: AsyncSession = Depends(get_db),
):
    new_policy = await policy_service.create_policy(
        db=db,
        tenant_id=user.tenant_id,
        data={
            "name": "Onboarding Default Policy",
            "is_active": True,
            "read_only_mode": body.read_only_mode,
            "sensitivity_default": body.sensitivity_default,
            "allowed_record_types": body.allowed_record_types,
            "blocked_fields": body.blocked_fields,
            "tool_allowlist": body.tool_allowlist,
            "max_rows_per_query": body.max_rows_per_query,
            "require_row_limit": body.require_row_limit,
        },
        user_id=user.id,
    )
    item = await onboarding_wizard_service.complete_step(
        db, user.tenant_id, "policy", user.id, metadata={"policy_id": str(new_policy.id)}
    )
    await db.commit()
    return ChecklistItemResponse(
        step_key=item.step_key,
        status=item.status,
        completed_at=item.completed_at.isoformat() if item.completed_at else None,
        completed_by=str(item.completed_by) if item.completed_by else None,
        metadata=item.metadata_,
    )


@router.post("/finalize", response_model=FinalizeResponse)
async def finalize_onboarding(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        completed_at = await onboarding_wizard_service.finalize_onboarding(db, user.tenant_id, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    await db.commit()
    return FinalizeResponse(success=True, completed_at=completed_at.isoformat())


@router.get("/audit-trail", response_model=AuditTrailResponse)
async def get_audit_trail(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    events = await onboarding_wizard_service.get_audit_trail(db, user.tenant_id)
    return AuditTrailResponse(
        events=[
            AuditTrailEvent(
                id=str(e.id),
                action=e.action,
                created_at=e.timestamp.isoformat(),
                correlation_id=e.correlation_id,
                actor_id=str(e.actor_id) if e.actor_id else None,
                resource_type=e.resource_type,
                resource_id=e.resource_id,
                payload=e.payload,
            )
            for e in events
        ]
    )


# --- Onboarding-specific OAuth authorize endpoints ---
# These bypass the normal entitlement checks (connections, mcp_tools)
# and instead use the "onboarding" entitlement which is available on all plans.


@router.get("/netsuite-mcp/authorize")
async def onboarding_netsuite_mcp_authorize(
    account_id: str,
    client_id: str,
    label: str = "",
    user: User = Depends(get_current_user),
    _ent: User = Depends(require_entitlement("onboarding")),
):
    """Start the OAuth 2.0 PKCE flow for a NetSuite MCP connector during onboarding.

    Wraps the same logic as /mcp-connectors/netsuite/authorize but uses
    the 'onboarding' entitlement instead of 'mcp_tools'.
    """
    if not client_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="client_id is required")
    if not account_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="account_id is required")

    from app.services.netsuite_oauth_service import build_mcp_authorize_url, generate_pkce_pair

    code_verifier, code_challenge = generate_pkce_pair()
    state = uuid.uuid4().hex
    redirect_uri = settings.NETSUITE_OAUTH_REDIRECT_URI

    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    await r.setex(
        f"netsuite_mcp_oauth:{state}",
        600,
        f"{code_verifier}:{account_id}:{client_id}:{user.tenant_id}:{user.id}:{label}",
    )
    await r.aclose()

    url = build_mcp_authorize_url(account_id, client_id, redirect_uri, state, code_challenge)
    return {"authorize_url": url, "state": state}


@router.get("/netsuite-oauth/authorize")
async def onboarding_netsuite_oauth_authorize(
    account_id: str,
    user: User = Depends(get_current_user),
    _ent: User = Depends(require_entitlement("onboarding")),
):
    """Start the OAuth 2.0 PKCE flow for a NetSuite Connection during onboarding.

    Wraps the same logic as /connections/netsuite/authorize but uses
    the 'onboarding' entitlement instead of requiring 'connections' entitlement.
    Reuses the existing callback at /connections/netsuite/callback.
    """
    if not account_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="account_id is required")

    if not settings.NETSUITE_OAUTH_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="NETSUITE_OAUTH_CLIENT_ID is not configured",
        )

    from app.services.netsuite_oauth_service import build_authorize_url, generate_pkce_pair

    code_verifier, code_challenge = generate_pkce_pair()
    state = uuid.uuid4().hex

    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    await r.setex(
        f"netsuite_oauth:{state}",
        600,
        f"{code_verifier}:{account_id}:{user.tenant_id}:{user.id}",
    )
    await r.aclose()

    url = build_authorize_url(account_id, state, code_challenge)
    return {"authorize_url": url, "state": state}
