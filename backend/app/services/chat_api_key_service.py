"""Chat API key management — create, authenticate, revoke, list."""

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_api_key import ChatApiKey
from app.services.audit_service import log_event

logger = structlog.get_logger()

_KEY_PREFIX = "ck_"
_KEY_RANDOM_BYTES = 32  # 256-bit random key


def _generate_raw_key() -> str:
    """Generate a random API key with the ck_ prefix."""
    return f"{_KEY_PREFIX}{secrets.token_hex(_KEY_RANDOM_BYTES)}"


def _hash_key(raw_key: str) -> str:
    """SHA-256 hash of the raw key, stored as hex."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def create_key(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    scopes: list[str],
    user_id: uuid.UUID,
    rate_limit_per_minute: int = 60,
    expires_at: datetime | None = None,
) -> tuple[ChatApiKey, str]:
    """Create a new API key. Returns (key_record, raw_key).

    The raw_key is shown once at creation and never stored.
    """
    raw_key = _generate_raw_key()
    key_hash = _hash_key(raw_key)
    prefix = raw_key[:7]  # "ck_" + first 4 hex chars

    api_key = ChatApiKey(
        tenant_id=tenant_id,
        name=name,
        key_prefix=prefix,
        key_hash=key_hash,
        scopes=scopes,
        rate_limit_per_minute=rate_limit_per_minute,
        is_active=True,
        expires_at=expires_at,
        created_by=user_id,
    )
    db.add(api_key)
    await db.flush()

    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="chat_api",
        action="chat_api.key_created",
        actor_id=user_id,
        resource_type="chat_api_key",
        resource_id=str(api_key.id),
        payload={"name": name, "key_prefix": prefix},
    )

    logger.info("chat_api.key_created", tenant_id=str(tenant_id), key_prefix=prefix)
    return api_key, raw_key


async def authenticate_key(
    db: AsyncSession,
    key_string: str,
) -> tuple[uuid.UUID, list[str]]:
    """Authenticate an API key string. Returns (tenant_id, scopes).

    Raises ValueError if key is invalid, inactive, or expired.
    """
    if not key_string.startswith(_KEY_PREFIX):
        raise ValueError("Invalid API key format")

    key_hash = _hash_key(key_string)
    result = await db.execute(select(ChatApiKey).where(ChatApiKey.key_hash == key_hash))
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise ValueError("Invalid API key")
    if not api_key.is_active:
        raise ValueError("API key has been revoked")
    if api_key.expires_at and api_key.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise ValueError("API key has expired")

    # Update last_used_at
    await db.execute(
        update(ChatApiKey).where(ChatApiKey.id == api_key.id).values(last_used_at=datetime.now(timezone.utc))
    )

    scopes = api_key.scopes if isinstance(api_key.scopes, list) else []
    return api_key.tenant_id, scopes


async def revoke_key(
    db: AsyncSession,
    key_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Soft-revoke an API key."""
    result = await db.execute(
        select(ChatApiKey).where(
            ChatApiKey.id == key_id,
            ChatApiKey.tenant_id == tenant_id,
        )
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise ValueError("API key not found")

    api_key.is_active = False
    await db.flush()

    await log_event(
        db=db,
        tenant_id=tenant_id,
        category="chat_api",
        action="chat_api.key_revoked",
        actor_id=user_id,
        resource_type="chat_api_key",
        resource_id=str(api_key.id),
        payload={"name": api_key.name, "key_prefix": api_key.key_prefix},
    )

    logger.info("chat_api.key_revoked", tenant_id=str(tenant_id), key_id=str(key_id))


async def list_keys(
    db: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[ChatApiKey]:
    """List all API keys for a tenant (never returns hashes)."""
    result = await db.execute(
        select(ChatApiKey).where(ChatApiKey.tenant_id == tenant_id).order_by(ChatApiKey.created_at.desc())
    )
    return list(result.scalars().all())
