"""Timeout behavior for google_auth_service.verify_google_token.

Regression test for the 2026-05-28 staging incident: the Google ID-token
verification call (asyncio.to_thread -> verify_oauth2_token, which fetches
Google certs over the network) had no timeout, so under resource pressure
``POST /auth/google`` hung ~50s instead of failing fast.
"""

import threading
import time

import pytest

import app.services.google_auth_service as gas


async def test_verify_google_token_times_out_instead_of_hanging(monkeypatch):
    """A stalled Google verification must raise quickly, not hang for its full duration."""
    monkeypatch.setattr(gas, "GOOGLE_CLIENT_ID", "test-client-id")

    # The worker thread can't be cancelled (asyncio.to_thread), so block it on an
    # Event we release after the assertion — keeps the normal run fast (~timeout)
    # instead of waiting out a long sleep.
    release = threading.Event()

    def stalled_verify(*args, **kwargs):
        release.wait(timeout=5)
        return {"iss": "accounts.google.com", "email": "x@y.z", "sub": "1", "name": "X"}

    monkeypatch.setattr(gas.google_id_token_lib, "verify_oauth2_token", stalled_verify)
    # Short timeout keeps the test fast; production default is larger.
    monkeypatch.setattr(gas, "GOOGLE_VERIFY_TIMEOUT", 0.2, raising=False)

    start = time.monotonic()
    try:
        with pytest.raises(ValueError):
            await gas.verify_google_token("any-token")
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"verify_google_token hung {elapsed:.1f}s instead of timing out"
    finally:
        release.set()  # let the lingering worker thread exit immediately
