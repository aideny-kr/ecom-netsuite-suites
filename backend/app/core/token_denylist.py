"""In-memory JWT denylist keyed by JTI (F4).

Tokens are stored with their expiry time so they can be cleaned up
after they would have expired naturally.
"""

import time

_denied: dict[str, float] = {}  # jti -> expiry timestamp


def revoke_token(jti: str, exp: float) -> None:
    """Add a JTI to the denylist. `exp` is the token's expiry as a Unix timestamp."""
    _denied[jti] = exp
    _cleanup()


def is_revoked(jti: str) -> bool:
    """Check if a JTI has been revoked."""
    return jti in _denied


def _cleanup() -> None:
    """Remove expired entries to prevent unbounded growth."""
    now = time.time()
    expired = [jti for jti, exp in _denied.items() if exp < now]
    for jti in expired:
        del _denied[jti]


def reset_denylist() -> None:
    """Clear all denylist state. Used in tests."""
    _denied.clear()
