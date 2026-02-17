"""In-memory sliding-window rate limiter for login attempts (F2)."""

import time
from collections import defaultdict

_login_attempts: dict[str, list[float]] = defaultdict(list)

WINDOW_SECONDS = 60
MAX_ATTEMPTS = 10


def check_login_rate_limit(ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = time.monotonic()
    attempts = _login_attempts[ip]
    # Prune old entries outside the window
    cutoff = now - WINDOW_SECONDS
    _login_attempts[ip] = [t for t in attempts if t > cutoff]
    attempts = _login_attempts[ip]

    if len(attempts) >= MAX_ATTEMPTS:
        return False

    attempts.append(now)
    return True


def reset_rate_limits() -> None:
    """Clear all rate limit state. Used in tests."""
    _login_attempts.clear()
