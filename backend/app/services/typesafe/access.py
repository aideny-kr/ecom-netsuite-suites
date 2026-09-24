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
from app.models.connection import Connection

logger = structlog.get_logger()

PROVIDER = "typesafe"
MODES = ("live", "shadow", "off")
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
    tid = tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))
    result = await db.execute(select(Connection).where(Connection.tenant_id == tid, Connection.provider == PROVIDER))
    return result.scalars().first()


async def _load(db: AsyncSession, tenant_id) -> tuple[JevSetting, str | None]:
    connection = await tenant_connection(db, tenant_id)
    tenant_mode: Mode = _mode((connection.metadata_json or {}).get("mode") if connection else None, "live")
    cap = deployment_cap()
    key: str | None = None
    source: Literal["tenant", "platform", "none"] = "none"
    hint: str | None = None
    problem: str | None = None

    if connection is not None:
        try:
            key = decrypt_credentials(connection.encrypted_credentials).get("api_key") or None
        except Exception:
            logger.warning("typesafe.tenant_key_unreadable", tenant_id=str(tenant_id))
            problem = "unreadable_key"
            source = "tenant"
        if key:
            source, hint = "tenant", key[-4:]
    if key is None and problem is None and settings.TYPESAFE_API_KEY:
        key, source = settings.TYPESAFE_API_KEY, "platform"

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
