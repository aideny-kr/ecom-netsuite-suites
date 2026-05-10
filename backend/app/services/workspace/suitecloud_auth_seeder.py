"""Bridge from our `connections` table to the suitecloud CLI's credential format.

The Oracle suitecloud CLI expects credentials at:
    ``$HOME/.suitecloud-sdk/credentials/<project_id>.json``

Our `connections` table holds encrypted OAuth2 tokens per tenant. This module
decrypts the active NetSuite connection, refreshes the access token if expired
(via ``get_valid_token``), and writes the CLI's expected JSON shape. We seed
per-run (not pod-startup) so token refresh races with long-running jobs are
avoided and so multiple tenants never share a credential cache on disk.

CLI version contract: documented for ``@oracle/suitecloud-cli`` >=2.0. If the
CLI upgrades, re-verify the credential file shape.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials
from app.models.connection import Connection
from app.services.netsuite_oauth_service import get_valid_token

logger = structlog.get_logger()


class AuthSeederError(Exception):
    """Raised when credentials cannot be seeded for a runner subprocess."""


async def seed_credentials_for_run(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    auth_root: Path,
    project_id: str,
) -> Path:
    """Write a suitecloud-CLI credential file for ``tenant_id``.

    Args:
        db: Async session — used to load the ``Connection`` row and (via
            ``get_valid_token``) to commit a refreshed token if needed.
        tenant_id: The tenant whose NetSuite connection should be seeded.
        auth_root: Directory the runner uses as ``$HOME``. The credential file
            is written to ``{auth_root}/.suitecloud-sdk/credentials/{project_id}.json``.
            Callers should pass a per-run tmp dir so multiple tenants never
            share a credential cache.
        project_id: Suitecloud project id (file basename, no ``.json``).

    Returns:
        Absolute path of the written credential file.

    Raises:
        AuthSeederError: No active NetSuite connection for the tenant; or
            ``get_valid_token`` could not produce a valid access token; or the
            stored credentials are missing ``account_id`` / ``client_id``.
    """
    result = await db.execute(
        select(Connection)
        .where(
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status == "active",
        )
        .limit(1)
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise AuthSeederError(f"no active NetSuite connection for tenant {tenant_id}")

    # Refresh-aware token retrieval. ``get_valid_token`` mutates
    # ``connection.encrypted_credentials`` in place and commits if a refresh
    # occurs, so we must re-decrypt afterwards to read the rotated
    # refresh_token / expires_at.
    access_token = await get_valid_token(db, connection)
    if access_token is None:
        raise AuthSeederError(f"could not obtain a valid NetSuite access token for tenant {tenant_id}")

    creds = decrypt_credentials(connection.encrypted_credentials)
    account_id = creds.get("account_id")
    client_id = creds.get("client_id")
    if not account_id or not client_id:
        raise AuthSeederError(f"NetSuite connection for tenant {tenant_id} missing account_id or client_id")

    cred_dir = auth_root / ".suitecloud-sdk" / "credentials"
    cred_dir.mkdir(parents=True, exist_ok=True)
    cred_path = cred_dir / f"{project_id}.json"

    payload: dict[str, Any] = {
        "accountId": account_id,
        "authType": "oauth2",
        "oauth2": {
            "clientId": client_id,
            "accessToken": access_token,
            "refreshToken": creds.get("refresh_token"),
            "tokenExpiry": creds.get("expires_at"),
        },
    }
    cred_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    cred_path.chmod(0o600)
    logger.info(
        "suitecloud_auth.seeded",
        tenant_id=str(tenant_id),
        project_id=project_id,
    )
    return cred_path
