"""Bind SDK receipts to an explicit current company connection and requested key.

No provisioning, network authentication or SDK activation. The certificate and
public-key fields describe requested local material; only account/role are
verified by SDK accountInfo. A REST token alone supplies none of this authority.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping

from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_credentials
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.services.workspace.credential_contract import AuthSeederError
from app.services.workspace.suitecloud_auth_seeder import require_verified_identity

_FIELDS = ("account_id", "role_id", "connection_id", "requested_certificate_id", "local_public_key_sha256")
_ERROR = "SuiteCloud requires the current permitted connection, account, numeric role, certificate and local public key"


def receipt_identity(value: Mapping) -> dict[str, str]:
    """Return only bounded public identity fields; reject legacy partial receipts."""
    try:
        fields = {name: value.get(name) for name in _FIELDS}
        if not all(isinstance(v, str) for v in fields.values()):
            raise ValueError()
        fields["account_id"], fields["role_id"] = require_verified_identity(
            account_id=fields["account_id"], role_id=fields["role_id"]
        )
        if not re.fullmatch(r"[a-z0-9]+-sb[0-9]+", fields["account_id"], re.ASCII):
            raise ValueError()
        connection_id = uuid.UUID(fields["connection_id"])
        if str(connection_id) != fields["connection_id"]:
            raise ValueError()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", fields["requested_certificate_id"], re.ASCII):
            raise ValueError()
        if not re.fullmatch(r"[a-f0-9]{64}", fields["local_public_key_sha256"], re.ASCII):
            raise ValueError()
        return fields
    except (ValueError, TypeError, AttributeError, AuthSeederError):
        raise AuthSeederError(_ERROR) from None


def seeded_identity(seeded) -> dict[str, str]:
    return receipt_identity({name: getattr(seeded, name, None) for name in _FIELDS})


async def require_current_target(db: AsyncSession, tenant_id: uuid.UUID, identity: Mapping) -> dict[str, str]:
    """Check the exact row, with a shared lock until transaction completion.

    Never select another available connection or refresh tokens. Re-read the row
    even in a long-lived ORM session so revocation/rotation cannot use cached data.
    Supported encrypted configuration is account_id plus suitecloud_ci containing
    role_id, certificate_id and PEM private_key. No public metadata can establish
    key identity. This contract does not create or authorize that configuration.
    """
    expected = receipt_identity(identity)
    connection = await db.scalar(
        select(Connection)
        .where(
            Connection.id == uuid.UUID(expected["connection_id"]),
            Connection.tenant_id == tenant_id,
            Connection.provider == "netsuite",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
        )
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    try:
        if connection is None or len(connection.encrypted_credentials) > 64 * 1024:
            raise ValueError()
        credentials = decrypt_credentials(connection.encrypted_credentials)
        ci = credentials["suitecloud_ci"]
        private_key = ci["private_key"]
        if not isinstance(private_key, str) or len(private_key) > 16 * 1024:
            raise ValueError()
        key = serialization.load_pem_private_key(private_key.encode("ascii"), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError()
        fingerprint = hashlib.sha256(
            key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        ).hexdigest()
        actual = receipt_identity(
            {
                "connection_id": str(connection.id),
                "account_id": credentials["account_id"],
                "role_id": ci["role_id"],
                "requested_certificate_id": ci["certificate_id"],
                "local_public_key_sha256": fingerprint,
            }
        )
        if actual != expected:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, InvalidToken, AuthSeederError):
        raise AuthSeederError(_ERROR) from None
    return expected
