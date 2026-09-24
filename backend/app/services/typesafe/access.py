"""Whether a tenant's work may reach Jev, with which key, and in which mode.

Decided 2026-09-24: Jev is on by default in every deployment.

* Key: the tenant's own TypeSafe key (a ``typesafe`` connection, encrypted like every
  connection) wins; otherwise the deployment's ``TYPESAFE_API_KEY``. With no key anywhere
  Jev is off and callers run their existing path unchanged, so the service stays optional.
* Mode: the tenant chooses ``live`` (the default), ``shadow`` or ``off`` on the Jev card.
  ``JEV_RECON_RESOLUTION_MODE`` caps every tenant (``live`` = no cap, ``off`` = kill switch).
* A tenant key that cannot be decrypted means OFF, not the platform key: a tenant that
  brought its own key must not have its data sent under ours without knowing.

Every caller resolves access here, so there is one answer to "may this tenant call Jev".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.encryption import decrypt_credentials
from app.models.connection import INCLUDE_JEV_CONNECTION, JEV_PROVIDER, Connection

logger = structlog.get_logger()

PROVIDER = JEV_PROVIDER
MODES = ("live", "shadow", "off")
# A hint is the last four characters, shown only when they are a small part of the key:
# for a very short key they would BE the key.
_HINT_MIN_KEY_LENGTH = 12
_RANK = {"off": 0, "shadow": 1, "live": 2}

Mode = Literal["live", "shadow", "off"]


@dataclass(frozen=True)
class JevAccess:
    """Permission to call Jev for one tenant. ``repr`` never shows the key."""

    api_key: str = field(repr=False)
    mode: Literal["live", "shadow"]
    key_source: Literal["tenant", "platform"]


@dataclass(frozen=True)
class JevSetting:
    """What the Jev card shows. Holds a hint of the tenant's key, never a key."""

    tenant_mode: Mode
    deployment_cap: Mode
    effective_mode: Mode
    key_source: Literal["tenant", "platform", "none"]
    key_hint: str | None = None
    problem: str | None = None  # "unreadable_key" when a stored tenant key cannot be decrypted


def _mode(value: object, default: Mode) -> Mode:
    return value if value in MODES else default  # type: ignore[return-value]


def deployment_cap() -> Mode:
    return _mode(settings.JEV_RECON_RESOLUTION_MODE, "off")


async def tenant_connection(db: AsyncSession, tenant_id: uuid.UUID | str) -> Connection | None:
    """The tenant's Jev connection; the newest wins if two were ever created concurrently."""
    tid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    result = await db.execute(
        select(Connection)
        .where(Connection.tenant_id == tid, Connection.provider == PROVIDER)
        .order_by(Connection.created_at.desc(), Connection.id.desc())
        # Every other query is blind to this row (app/models/connection.py). Re-read it even
        # when the session already holds it: a card change must reach a running worker.
        .execution_options(**{INCLUDE_JEV_CONNECTION: True}, populate_existing=True)
    )
    return result.scalars().first()


def _key_of(connection: Connection | None, tenant_id) -> tuple[str | None, Literal["tenant", "platform", "none"], bool]:
    """(key, source, unreadable) for Jev calls, ignoring the mode."""
    if connection is not None:
        try:
            key = decrypt_credentials(connection.encrypted_credentials).get("api_key") or None
        except Exception:
            logger.warning("typesafe.tenant_key_unreadable", tenant_id=str(tenant_id))
            return None, "tenant", True
        if key:
            return key, "tenant", False
    if settings.TYPESAFE_API_KEY:
        return settings.TYPESAFE_API_KEY, "platform", False
    return None, "none", False


async def key_in_use(db: AsyncSession, tenant_id) -> tuple[str | None, Literal["tenant", "platform", "none"]]:
    """The key this tenant's Jev calls use whatever the mode: what the card's Test checks.
    ``(None, "tenant")`` means the tenant's stored key cannot be read."""
    key, source, _ = _key_of(await tenant_connection(db, tenant_id), tenant_id)
    return key, source


async def _load(db: AsyncSession, tenant_id) -> tuple[JevSetting, str | None]:
    connection = await tenant_connection(db, tenant_id)
    tenant_mode: Mode = _mode((connection.metadata_json or {}).get("mode") if connection else None, "live")
    cap = deployment_cap()
    key, source, unreadable = _key_of(connection, tenant_id)
    hint = key[-4:] if key and source == "tenant" and len(key) >= _HINT_MIN_KEY_LENGTH else None
    problem = "unreadable_key" if unreadable else None

    effective: Mode = min(tenant_mode, cap, key=_RANK.__getitem__) if key and problem is None else "off"
    setting = JevSetting(
        tenant_mode=tenant_mode,
        deployment_cap=cap,
        effective_mode=effective,
        key_source=source,
        key_hint=hint,
        problem=problem,
    )
    return setting, (key if effective != "off" else None)


async def load_setting(db: AsyncSession, tenant_id) -> JevSetting:
    setting, _ = await _load(db, tenant_id)
    return setting


async def resolve_access(db: AsyncSession, tenant_id) -> JevAccess | None:
    """The key and mode to use for this tenant's Jev calls, or None when Jev is off."""
    setting, key = await _load(db, tenant_id)
    if key is None or setting.effective_mode == "off" or setting.key_source == "none":
        return None
    return JevAccess(api_key=key, mode=setting.effective_mode, key_source=setting.key_source)  # type: ignore[arg-type]
