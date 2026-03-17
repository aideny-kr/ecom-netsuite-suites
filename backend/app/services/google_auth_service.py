"""Google OAuth ID token verification."""

import asyncio
import os

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token_lib

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")


async def verify_google_token(token: str) -> dict:
    """Verify a Google ID token and return user info.

    Returns: {"email": str, "name": str, "picture": str | None, "sub": str}
    Raises: ValueError if token is invalid, expired, or client ID mismatch.
    """
    if not GOOGLE_CLIENT_ID:
        raise ValueError("Google Sign-In is not configured (GOOGLE_CLIENT_ID not set).")

    try:
        id_info = await asyncio.to_thread(
            google_id_token_lib.verify_oauth2_token,
            token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
        )
    except Exception as e:
        raise ValueError(f"Invalid Google token: {e}")

    if id_info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError("Invalid token issuer.")

    return {
        "email": id_info["email"],
        "name": id_info.get("name", ""),
        "picture": id_info.get("picture"),
        "sub": id_info["sub"],
    }
