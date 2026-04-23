"""Regression test: expanding _SCOPES to include drive.readonly must not
break existing Sheets flows (create / read / write / share / validate)."""

from unittest.mock import MagicMock, patch

import pytest

from app.services import sheets_service


def test_scopes_include_drive_readonly():
    """Explicit invariant: drive.readonly is in _SCOPES."""
    assert "https://www.googleapis.com/auth/drive.readonly" in sheets_service._SCOPES


def test_scopes_include_existing_sheets_and_drive_file():
    """Existing scopes must remain (backward compat)."""
    assert "https://www.googleapis.com/auth/spreadsheets" in sheets_service._SCOPES
    assert "https://www.googleapis.com/auth/drive.file" in sheets_service._SCOPES


@pytest.mark.asyncio
async def test_build_sheets_service_uses_expanded_scopes():
    """google-auth receives the full scope list when building the Sheets client."""
    creds = {
        "type": "service_account",
        "project_id": "test",
        "private_key_id": "x",
        "private_key": "-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----\n",
        "client_email": "test@test.iam.gserviceaccount.com",
        "client_id": "1",
        "auth_uri": "x",
        "token_uri": "x",
    }
    with (
        patch("app.services.sheets_service.service_account.Credentials.from_service_account_info") as mock_from_info,
        patch("app.services.sheets_service.build") as mock_build,
    ):
        mock_from_info.return_value = MagicMock()
        mock_build.return_value = MagicMock()
        sheets_service._build_sheets_service(creds)

    args, kwargs = mock_from_info.call_args
    passed_scopes = kwargs.get("scopes") or (args[1] if len(args) > 1 else None)
    assert "https://www.googleapis.com/auth/drive.readonly" in passed_scopes
    assert "https://www.googleapis.com/auth/spreadsheets" in passed_scopes


@pytest.mark.asyncio
async def test_build_drive_service_uses_expanded_scopes():
    creds = {
        "type": "service_account",
        "project_id": "test",
        "private_key_id": "x",
        "private_key": "-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----\n",
        "client_email": "test@test.iam.gserviceaccount.com",
        "client_id": "1",
        "auth_uri": "x",
        "token_uri": "x",
    }
    with (
        patch("app.services.sheets_service.service_account.Credentials.from_service_account_info") as mock_from_info,
        patch("app.services.sheets_service.build") as mock_build,
    ):
        mock_from_info.return_value = MagicMock()
        mock_build.return_value = MagicMock()
        sheets_service._build_drive_service(creds)

    args, kwargs = mock_from_info.call_args
    passed_scopes = kwargs.get("scopes") or (args[1] if len(args) > 1 else None)
    assert "https://www.googleapis.com/auth/drive.readonly" in passed_scopes
