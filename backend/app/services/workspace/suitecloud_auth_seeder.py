"""Fail closed until a supported, isolated SuiteCloud SDK adapter is verified.

Oracle CLI 3.2 uses ``account:setup:ci`` and an encrypted SDK credential store,
then selects the project's ``defaultAuthId``. Suite Studio's REST OAuth tokens
are not that store. Writing a guessed JSON file neither authorizes the CLI nor
binds its account/role. Never refresh REST credentials or use host credentials
as a substitute.

A future certificate adapter must verify the actual account/role and bind that
identity across check receipts, preview, confirmation and worker execution.
No certificate provisioning or SDK-store import is implemented here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection


class AuthSeederError(Exception):
    """The run has no verified, isolated SuiteCloud account/role authorization."""


@dataclass(frozen=True)
class SeededCredentials:
    """Adapter result contract; currently used only by synthetic runner tests.

    Production seeding fails closed. A stored REST account ID alone is never
    sufficient authorization, even when its name looks like a sandbox.
    """

    path: Path
    account_id: str
    role_id: str | None = None


async def seed_credentials_for_run(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    auth_root: Path,
    project_id: str,
) -> SeededCredentials:
    """Reject the unsupported REST-token bridge without reading secret values.

    Keep the runner interface while the certificate-based adapter is pending.
    Neither a missing connection nor an OAuth-only connection can fall through
    to ambient CLI credentials. No token refresh or filesystem mutation occurs.
    """
    connection_id = await db.scalar(
        select(Connection.id)
        .where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        )
        .limit(1)
    )
    if connection_id is None:
        raise AuthSeederError("no active NetSuite connection for this workspace company")
    raise AuthSeederError(
        "SuiteCloud requires verified certificate-based CI authorization for an authorized sandbox account and role. "
        "The configured REST OAuth connection cannot authorize SuiteCloud CLI. "
        "The supported SDK credential adapter is not yet available; validation and deployment remain blocked."
    )
