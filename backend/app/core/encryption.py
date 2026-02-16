import json

from cryptography.fernet import Fernet

from app.core.config import settings


def _get_fernet() -> Fernet:
    key = settings.ENCRYPTION_KEY
    if not key or key == "change-me-generate-a-real-fernet-key":
        raise ValueError("ENCRYPTION_KEY must be set to a valid Fernet key")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_credentials(credentials: dict) -> str:
    """Encrypt a credentials dictionary to a Fernet-encrypted string."""
    f = _get_fernet()
    plaintext = json.dumps(credentials).encode("utf-8")
    return f.encrypt(plaintext).decode("utf-8")


def decrypt_credentials(encrypted: str) -> dict:
    """Decrypt a Fernet-encrypted string back to a credentials dictionary."""
    f = _get_fernet()
    plaintext = f.decrypt(encrypted.encode("utf-8"))
    return json.loads(plaintext.decode("utf-8"))


def get_current_key_version() -> int:
    return settings.ENCRYPTION_KEY_VERSION
