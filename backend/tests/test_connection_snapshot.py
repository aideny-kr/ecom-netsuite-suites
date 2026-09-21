"""A status-page read must not manufacture provider verification evidence."""

import time
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.connection import Connection
from app.models.mcp_connector import McpConnector


def row(**kw):
    return Connection(
        id=uuid.uuid4(),
        provider="netsuite",
        label="ERP",
        status="active",
        auth_type="oauth2",
        encrypted_credentials="synthetic",
        **kw,
    )


def snapshot(connection, creds=None):
    from app.services.connection_snapshot import connection_snapshot

    with patch("app.services.connection_snapshot.decrypt_credentials", return_value=creds or {}):
        return connection_snapshot(connection)


def test_page_read_does_not_create_verification_time():
    assert snapshot(row())["last_health_check"] is None
    checked = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert snapshot(row(last_health_check_at=checked))["last_health_check"] == checked.isoformat()


def test_expired_access_token_with_refresh_is_not_reported_healthy_or_revoked():
    result = snapshot(row(), {"expires_at": time.time() - 1, "refresh_token": "secret"})
    assert result["status"] == "refresh_required"
    assert "secret" not in str(result)
    assert snapshot(row(), {"expires_at": time.time() - 1})["status"] == "needs_reauth"


def test_decryption_failure_is_not_masked_by_active_status():
    from app.services.connection_snapshot import connection_snapshot

    with patch("app.services.connection_snapshot.decrypt_credentials", side_effect=ValueError("secret ciphertext")):
        result = connection_snapshot(row())
    assert result["status"] == "error"
    assert "ciphertext" not in str(result)


def test_only_allowlisted_identity_and_scope_are_returned():
    result = snapshot(
        row(),
        {
            "account_id": "sandbox",
            "role_id": "reader",
            "scope": "rest_webservices",
            "access_token": "secret",
            "client_secret": "secret",
        },
    )
    assert result["account_identity"] == "sandbox"
    assert result["access_scope"] == "rest_webservices"
    assert result["role"] == "reader"
    assert "secret" not in str(result)


def test_disabled_and_partial_mcp_remain_distinct():
    mcp = McpConnector(
        id=uuid.uuid4(),
        provider="custom",
        label="Warehouse",
        status="active",
        auth_type="none",
        server_url="https://example.test/mcp",
        is_enabled=False,
        last_health_check_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        metadata_json={"verification_status": "partial", "verification_at": "2026-09-01T00:00:00+00:00"},
    )
    result = snapshot(mcp)
    assert result["status"] == "disabled"
    assert result["verification_status"] == "partial"


@pytest.mark.parametrize("expiry", ["nonsense", {}, float("inf")])
def test_malformed_expiry_fails_closed_without_failing_the_entire_list(expiry):
    assert snapshot(row(), {"expires_at": expiry})["status"] == "error"


def test_verification_result_does_not_borrow_a_newer_refresh_timestamp():
    checked = datetime(2026, 9, 1, tzinfo=timezone.utc)
    refreshed = datetime(2026, 9, 2, tzinfo=timezone.utc)
    result = snapshot(
        row(
            last_health_check_at=refreshed,
            metadata_json={"verification_status": "ok", "verification_at": checked.isoformat()},
        )
    )
    assert result["verification_status"] is None
    assert result["last_health_check"] == refreshed.isoformat()
