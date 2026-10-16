"""The REST OAuth connection is not a SuiteCloud SDK authorization store."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.services.workspace.suitecloud_auth_seeder import AuthSeederError, seed_credentials_for_run


@pytest_asyncio.fixture
async def seeded_netsuite_connection(db: AsyncSession, tenant_a):
    conn = Connection(
        tenant_id=tenant_a.id,
        provider="netsuite",
        label="Synthetic REST connection",
        status="active",
        auth_type="oauth2",
        encrypted_credentials=encrypt_credentials(
            {"account_id": "1234567_SB1", "client_id": "synthetic", "access_token": "synthetic-only"}
        ),
    )
    db.add(conn)
    await db.flush()
    return conn


@pytest.mark.asyncio
async def test_rest_oauth_is_rejected_without_refresh_or_fictitious_sdk_file(
    db: AsyncSession, tmp_path: Path, seeded_netsuite_connection, tenant_a
):
    refresh = AsyncMock(return_value="synthetic-only")
    with patch("app.services.netsuite_oauth_service.get_valid_token", new=refresh):
        with pytest.raises(AuthSeederError, match="certificate-based CI authorization"):
            await seed_credentials_for_run(db=db, tenant_id=tenant_a.id, auth_root=tmp_path, project_id="ws-1")
    refresh.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_no_connection_has_specific_blocker(db: AsyncSession, tmp_path: Path, tenant_a):
    with pytest.raises(AuthSeederError, match="no active NetSuite connection"):
        await seed_credentials_for_run(db=db, tenant_id=tenant_a.id, auth_root=tmp_path, project_id="ws-1")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_peer_and_inactive_connections_do_not_supply_authority(
    db: AsyncSession, tmp_path: Path, seeded_netsuite_connection, tenant_a, tenant_b
):
    with pytest.raises(AuthSeederError, match="no active NetSuite connection"):
        await seed_credentials_for_run(db=db, tenant_id=tenant_b.id, auth_root=tmp_path, project_id="ws-1")
    seeded_netsuite_connection.status = "disconnected"
    await db.flush()
    with pytest.raises(AuthSeederError, match="no active NetSuite connection"):
        await seed_credentials_for_run(db=db, tenant_id=tenant_a.id, auth_root=tmp_path, project_id="ws-1")
    assert list(tmp_path.iterdir()) == []


def test_deploy_command_requires_verified_account_and_role():
    from app.services.runner_service import _build_deploy_command

    with pytest.raises(ValueError, match="verified SuiteCloud account and role"):
        _build_deploy_command(["suitecloud", "project:deploy", "--validate"], "1234567-sb1")


def test_deploy_command_rejects_different_authorized_account():
    from app.services.runner_service import _build_deploy_command

    with pytest.raises(ValueError, match="does not match"):
        _build_deploy_command(
            ["suitecloud", "project:deploy", "--validate"],
            "1234567-sb1",
            authorized_account_id="7654321_SB1",
            authorized_role_id="1234",
        )


def test_deploy_command_uses_real_cli_options_after_identity_check():
    from app.services.runner_service import ALLOWED_COMMANDS, _build_deploy_command

    cmd = _build_deploy_command(
        ALLOWED_COMMANDS["deploy_sandbox"]["cmd"],
        "1234567-sb1",
        authorized_account_id="1234567_SB1",
        authorized_role_id="1234",
    )
    assert cmd == ["suitecloud", "project:deploy", "--validate"]
