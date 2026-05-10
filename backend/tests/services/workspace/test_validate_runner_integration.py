"""Integration tests for the suitecloud_validate run path in runner_service.

Covers Task 4 of the workspace-validate-ux plan: allowlist swap, the new
``_execute_validate_run`` branch, ValidationHit persistence, and the new
run-record columns (validator_engine, parser_version, has_errors,
has_warnings, gate_status, snapshot_hash).
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_credentials
from app.models.connection import Connection
from app.models.workspace import (
    ValidationHit,
    Workspace,
    WorkspaceChangeSet,
    WorkspaceFile,
    WorkspaceRun,
)
from app.services import runner_service
from app.services.workspace.suitecloud_auth_seeder import SeededCredentials

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Default fake seeded-credentials result. ``account_id`` matches the
# ``seeded_netsuite_connection`` fixture so snapshot_hash inputs stay
# consistent across tests.
_FAKE_SEEDED = SeededCredentials(path=Path("/tmp/fake.json"), account_id="1234567")


# --- Fixtures ---


@pytest_asyncio.fixture
async def seeded_workspace_with_changeset(db: AsyncSession, tenant_a, admin_user):
    """Workspace + a single file + an approved changeset (no patches)."""
    user, _ = admin_user
    workspace = Workspace(
        tenant_id=tenant_a.id,
        name="ws-validate-test",
        status="active",
        created_by=user.id,
    )
    db.add(workspace)
    await db.flush()

    f = WorkspaceFile(
        tenant_id=tenant_a.id,
        workspace_id=workspace.id,
        path="src/Suitelets/processOrder.js",
        file_name="processOrder.js",
        content="// stub\n",
        size_bytes=len("// stub\n".encode("utf-8")),
        is_directory=False,
    )
    db.add(f)
    await db.flush()

    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id,
        workspace_id=workspace.id,
        title="Test changeset",
        status="approved",
        proposed_by=user.id,
    )
    db.add(cs)
    await db.flush()

    return workspace, cs, user


@pytest_asyncio.fixture
async def seeded_netsuite_connection(db: AsyncSession, tenant_a):
    """Active OAuth2 NetSuite connection with a fresh access token."""
    creds = {
        "auth_type": "oauth2",
        "access_token": "live_access_tok",
        "refresh_token": "live_refresh_tok",
        "expires_at": time.time() + 3600,
        "account_id": "1234567",
        "client_id": "test_client_id_abc",
    }
    conn = Connection(
        tenant_id=tenant_a.id,
        provider="netsuite",
        label="Test NS",
        status="active",
        auth_type="oauth2",
        encrypted_credentials=encrypt_credentials(creds),
    )
    db.add(conn)
    await db.flush()
    return conn


# --- Allowlist tests ---


def test_suitecloud_validate_in_allowlist() -> None:
    cfg = runner_service.validate_run_type("suitecloud_validate")
    assert cfg["cmd"] == ["suitecloud", "project:validate", "--server"]
    assert cfg["timeout"] == 180


def test_sdf_validate_removed_from_allowlist() -> None:
    with pytest.raises(runner_service.CommandNotAllowedError):
        runner_service.validate_run_type("sdf_validate")


# --- Execution tests ---


@pytest.mark.asyncio
async def test_execute_run_persists_validation_hits_on_errors(
    db: AsyncSession,
    seeded_workspace_with_changeset,
    seeded_netsuite_connection,
) -> None:
    """Errors fixture → run failed, gate_status=block, hits persisted with codes."""
    workspace, cs, user = seeded_workspace_with_changeset
    run = await runner_service.create_run(
        db=db,
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        run_type="suitecloud_validate",
        triggered_by=user.id,
        changeset_id=cs.id,
    )
    await db.flush()

    fixture_stdout = (FIXTURES_DIR / "suitecloud_validate_errors.txt").read_text()
    with (
        patch.object(
            runner_service,
            "_run_subprocess",
            new=AsyncMock(return_value=(1, fixture_stdout, "")),
        ),
        patch(
            "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
            new=AsyncMock(return_value=_FAKE_SEEDED),
        ),
    ):
        await runner_service.execute_run(db=db, run_id=run.id, tenant_id=run.tenant_id)

    refreshed = (await db.execute(select(WorkspaceRun).where(WorkspaceRun.id == run.id))).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.has_errors is True
    assert refreshed.has_warnings is False
    assert refreshed.gate_status == "block"
    assert refreshed.validator_engine == "suitecloud_server"
    assert refreshed.parser_version == "1.0.0"
    assert refreshed.snapshot_hash is not None and len(refreshed.snapshot_hash) == 64
    # exit_code is recorded raw — gating is independent of it
    assert refreshed.exit_code == 1

    hits = (await db.execute(select(ValidationHit).where(ValidationHit.run_id == run.id))).scalars().all()
    assert len(hits) == 2
    codes = {h.code for h in hits}
    assert codes == {"OWASP-A03", "SDF-SCHEMA-001"}
    # rule_id is populated later by Task 10 narration helper
    for hit in hits:
        assert hit.rule_id is None
        assert hit.severity == "error"
        assert hit.fingerprint and len(hit.fingerprint) == 64


@pytest.mark.asyncio
async def test_execute_run_passes_on_warnings_only(
    db: AsyncSession,
    seeded_workspace_with_changeset,
    seeded_netsuite_connection,
) -> None:
    """Warnings only → run passed, gate_status=pass (warnings don't block)."""
    workspace, cs, user = seeded_workspace_with_changeset
    run = await runner_service.create_run(
        db=db,
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        run_type="suitecloud_validate",
        triggered_by=user.id,
        changeset_id=cs.id,
    )
    await db.flush()

    fixture_stdout = (FIXTURES_DIR / "suitecloud_validate_warnings.txt").read_text()
    with (
        patch.object(
            runner_service,
            "_run_subprocess",
            new=AsyncMock(return_value=(0, fixture_stdout, "")),
        ),
        patch(
            "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
            new=AsyncMock(return_value=_FAKE_SEEDED),
        ),
    ):
        await runner_service.execute_run(db=db, run_id=run.id, tenant_id=run.tenant_id)

    refreshed = (await db.execute(select(WorkspaceRun).where(WorkspaceRun.id == run.id))).scalar_one()
    assert refreshed.status == "passed"
    assert refreshed.has_errors is False
    assert refreshed.has_warnings is True
    assert refreshed.gate_status == "pass"
    assert refreshed.validator_engine == "suitecloud_server"
    assert refreshed.parser_version == "1.0.0"

    hits = (await db.execute(select(ValidationHit).where(ValidationHit.run_id == run.id))).scalars().all()
    assert len(hits) == 2
    for hit in hits:
        assert hit.severity == "warning"


@pytest.mark.asyncio
async def test_execute_run_fails_on_auth_error(
    db: AsyncSession,
    seeded_workspace_with_changeset,
) -> None:
    """Seeder failure → run failed, gate_status=block, no subprocess call."""
    workspace, cs, user = seeded_workspace_with_changeset
    run = await runner_service.create_run(
        db=db,
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        run_type="suitecloud_validate",
        triggered_by=user.id,
        changeset_id=cs.id,
    )
    await db.flush()

    from app.services.workspace.suitecloud_auth_seeder import AuthSeederError

    subprocess_mock = AsyncMock(return_value=(0, "", ""))
    with (
        patch.object(runner_service, "_run_subprocess", new=subprocess_mock),
        patch(
            "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
            new=AsyncMock(side_effect=AuthSeederError("no active NetSuite connection")),
        ),
    ):
        await runner_service.execute_run(db=db, run_id=run.id, tenant_id=run.tenant_id)

    refreshed = (await db.execute(select(WorkspaceRun).where(WorkspaceRun.id == run.id))).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.gate_status == "block"
    assert refreshed.validator_engine == "suitecloud_server"
    # No subprocess should have been launched if auth failed
    subprocess_mock.assert_not_awaited()

    # No validation hits were persisted (we never ran the CLI)
    hits = (await db.execute(select(ValidationHit).where(ValidationHit.run_id == run.id))).scalars().all()
    assert hits == []


@pytest.mark.asyncio
async def test_execute_run_clean_validation_passes(
    db: AsyncSession,
    seeded_workspace_with_changeset,
    seeded_netsuite_connection,
) -> None:
    """Clean run (SUCCESS terminal line, no diagnostics) → passed, no hits."""
    workspace, cs, user = seeded_workspace_with_changeset
    run = await runner_service.create_run(
        db=db,
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        run_type="suitecloud_validate",
        triggered_by=user.id,
        changeset_id=cs.id,
    )
    await db.flush()

    clean_stdout = (
        "INFO: Validating project against account 6738075...\nSUCCESS: Project validation completed successfully.\n"
    )
    with (
        patch.object(
            runner_service,
            "_run_subprocess",
            new=AsyncMock(return_value=(0, clean_stdout, "")),
        ),
        patch(
            "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
            new=AsyncMock(return_value=_FAKE_SEEDED),
        ),
    ):
        await runner_service.execute_run(db=db, run_id=run.id, tenant_id=run.tenant_id)

    refreshed = (await db.execute(select(WorkspaceRun).where(WorkspaceRun.id == run.id))).scalar_one()
    assert refreshed.status == "passed"
    assert refreshed.has_errors is False
    assert refreshed.has_warnings is False
    assert refreshed.gate_status == "pass"

    hits = (await db.execute(select(ValidationHit).where(ValidationHit.run_id == run.id))).scalars().all()
    assert hits == []


@pytest.mark.asyncio
async def test_gate_status_independent_of_exit_code(
    db: AsyncSession,
    seeded_workspace_with_changeset,
    seeded_netsuite_connection,
) -> None:
    """gate_status MUST come from parsed.has_errors, never exit_code (codex #11).

    Pin: exit_code=0 + parsed has errors → gate=block. Locks in the
    independence so a future refactor that adds
    ``if exit_code != 0: gate_status = "block"`` (or vice versa) regresses
    loudly instead of silently.
    """
    workspace, cs, user = seeded_workspace_with_changeset
    run = await runner_service.create_run(
        db=db,
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        run_type="suitecloud_validate",
        triggered_by=user.id,
        changeset_id=cs.id,
    )
    await db.flush()

    fixture_stdout = (FIXTURES_DIR / "suitecloud_validate_errors.txt").read_text()
    with (
        patch.object(
            runner_service,
            "_run_subprocess",
            # exit_code=0 (subprocess claims success), but stdout contains errors
            new=AsyncMock(return_value=(0, fixture_stdout, "")),
        ),
        patch(
            "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
            new=AsyncMock(return_value=_FAKE_SEEDED),
        ),
    ):
        await runner_service.execute_run(db=db, run_id=run.id, tenant_id=run.tenant_id)

    refreshed = (await db.execute(select(WorkspaceRun).where(WorkspaceRun.id == run.id))).scalar_one()
    # Subprocess reported success...
    assert refreshed.exit_code == 0
    # ...but the gate must still block on parsed errors.
    assert refreshed.gate_status == "block"
    assert refreshed.has_errors is True
    assert refreshed.status == "failed"


@pytest.mark.asyncio
async def test_execute_run_handles_mixed_severity(
    db: AsyncSession,
    seeded_workspace_with_changeset,
    seeded_netsuite_connection,
) -> None:
    """Mixed errors + warnings → has_errors AND has_warnings both True; gate=block.

    Exercises the realistic production case where a single run surfaces both
    severities. Pure-warning and pure-error paths are covered above; this
    pins the combined flag handling.
    """
    workspace, cs, user = seeded_workspace_with_changeset
    run = await runner_service.create_run(
        db=db,
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        run_type="suitecloud_validate",
        triggered_by=user.id,
        changeset_id=cs.id,
    )
    await db.flush()

    fixture_stdout = (FIXTURES_DIR / "suitecloud_validate_mixed.txt").read_text()
    with (
        patch.object(
            runner_service,
            "_run_subprocess",
            new=AsyncMock(return_value=(1, fixture_stdout, "")),
        ),
        patch(
            "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
            new=AsyncMock(return_value=_FAKE_SEEDED),
        ),
    ):
        await runner_service.execute_run(db=db, run_id=run.id, tenant_id=run.tenant_id)

    refreshed = (await db.execute(select(WorkspaceRun).where(WorkspaceRun.id == run.id))).scalar_one()
    assert refreshed.has_errors is True
    assert refreshed.has_warnings is True
    assert refreshed.gate_status == "block"
    assert refreshed.status == "failed"

    hits = (await db.execute(select(ValidationHit).where(ValidationHit.run_id == run.id))).scalars().all()
    severities = {h.severity for h in hits}
    assert severities == {"error", "warning"}
