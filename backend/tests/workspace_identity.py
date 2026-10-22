"""Synthetic certificate identities for workspace boundary tests; no SDK/network."""

import hashlib
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection


async def make_target(db, tenant_id, account="123-sb1"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    fingerprint = hashlib.sha256(
        key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ).hexdigest()
    credentials = {
        "account_id": account,
        "suitecloud_ci": {"role_id": "1234", "certificate_id": "synthetic-cert", "private_key": pem},
    }
    connection = Connection(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="netsuite",
        label="Synthetic certificate only",
        status="active",
        auth_type="oauth2",
        encrypted_credentials=encrypt_credentials(credentials),
    )
    db.add(connection)
    await db.flush()
    identity = {
        "account_id": account.lower().replace("_", "-"),
        "role_id": "1234",
        "connection_id": str(connection.id),
        "requested_certificate_id": "synthetic-cert",
        "local_public_key_sha256": fingerprint,
    }
    return connection, credentials, identity
