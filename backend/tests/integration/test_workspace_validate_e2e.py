"""End-to-end: apply_patch -> auto-validate -> narration -> record fix intent.

Wires the orchestrator to a real runner_service.create_run closure (the
production code does this in main.py's lifespan) so apply_patch's enqueue
actually produces a WorkspaceRun row. Then dispatches that row through
execute_run with a mocked subprocess + seeder, parses the warnings fixture,
runs the narration helper, and asserts orchestrator state mutations.

NOTE: _maybe_auto_propose_fix is currently narrate-only (Task 10b will add
real patch generators). The test asserts the orchestrator records intent,
not that workspace_propose_patch is called.
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
from app.mcp.tools import workspace_tools
from app.models.connection import Connection
from app.models.workspace import (
    ValidationHit,
    Workspace,
    WorkspaceChangeSet,
    WorkspaceFile,
    WorkspaceRun,
)
from app.services import runner_service
from app.services.chat.agents.workspace_agent import (
    _batch_hits_by_family,
    _maybe_auto_propose_fix,
)
from app.services.workspace.auto_validate_orchestrator import get_orchestrator
from app.services.workspace.suitecloud_auth_seeder import SeededCredentials

FIXTURES_DIR = Path(__file__).parent.parent / "services" / "workspace" / "fixtures"


@pytest_asyncio.fixture
async def seeded_workspace_with_changeset(db: AsyncSession, tenant_a, admin_user):
    """Approved (empty) changeset on a workspace with one in-tree file.

    apply_changeset's patch loop is a no-op with zero patches; the changeset
    is simply marked applied. The integration test cares about the wire-up,
    not the patch application semantics (that's covered elsewhere).
    """
    user, _ = admin_user
    workspace = Workspace(
        tenant_id=tenant_a.id,
        name="ws-e2e",
        status="active",
        created_by=user.id,
    )
    db.add(workspace)
    await db.flush()

    wf = WorkspaceFile(
        tenant_id=tenant_a.id,
        workspace_id=workspace.id,
        path="src/UserEvents/auditLog.js",
        file_name="auditLog.js",
        content="// existing\n",
        size_bytes=len("// existing\n".encode("utf-8")),
        is_directory=False,
    )
    db.add(wf)
    await db.flush()

    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id,
        workspace_id=workspace.id,
        title="E2E CS",
        status="approved",
        proposed_by=user.id,
    )
    db.add(cs)
    await db.flush()

    return workspace, cs, user


@pytest_asyncio.fixture
async def seeded_netsuite_connection(db: AsyncSession, tenant_a):
    creds = {
        "auth_type": "oauth2",
        "access_token": "tok",
        "refresh_token": "ref",
        "expires_at": time.time() + 3600,
        "account_id": "1234567",
        "client_id": "test_client",
    }
    conn = Connection(
        tenant_id=tenant_a.id,
        provider="netsuite",
        label="NS",
        status="active",
        auth_type="oauth2",
        encrypted_credentials=encrypt_credentials(creds),
    )
    db.add(conn)
    await db.flush()
    return conn


@pytest.mark.asyncio
async def test_apply_patch_to_validate_to_record_fix_intent_e2e(
    db: AsyncSession,
    seeded_workspace_with_changeset,
    seeded_netsuite_connection,
) -> None:
    """Walk the entire pipeline.

    1. execute_apply_patch enqueues a suitecloud_validate run via the orchestrator.
    2. The orchestrator's _create_run closure produces a real WorkspaceRun row.
    3. We dispatch that row through execute_run (mocked subprocess + seeder).
    4. Parser extracts 2 warnings from the fixture; ValidationHit rows persist.
    5. _batch_hits_by_family groups the hits by code.
    6. _maybe_auto_propose_fix records orchestrator intent for each fixable hit.
    """
    workspace, cs, admin = seeded_workspace_with_changeset
    fixture = (FIXTURES_DIR / "suitecloud_validate_warnings.txt").read_text()

    # Reset orchestrator state for test isolation. Production wires _create_run
    # in main.py's lifespan; we wire it here in-test against the live db session.
    # get_orchestrator() returns a process-singleton, so any prior test that
    # touched it would otherwise leak state into this run.
    orch = get_orchestrator()
    orch._latest_run_per_workspace.clear()
    orch._cancelled.clear()
    orch._auto_fix_count.clear()
    orch._proposed_fingerprints.clear()

    async def _create_run_inline(**kwargs):
        run = await runner_service.create_run(db=db, **kwargs)
        await db.flush()
        return run.id

    orch._create_run = _create_run_inline

    permission_mock = AsyncMock(return_value=True)
    seeded_creds = SeededCredentials(path=Path("/tmp/fake.json"), account_id="1234567")

    with (
        patch("app.core.dependencies.has_permission", new=permission_mock),
        patch(
            "app.services.runner_service._run_subprocess",
            new=AsyncMock(return_value=(0, fixture, "")),
        ),
        patch(
            "app.services.workspace.suitecloud_auth_seeder.seed_credentials_for_run",
            new=AsyncMock(return_value=seeded_creds),
        ),
    ):
        # -- Step 1+2: apply_patch enqueues + creates a real WorkspaceRun row --
        result = await workspace_tools.execute_apply_patch(
            params={"changeset_id": str(cs.id)},
            context={
                "tenant_id": str(workspace.tenant_id),
                "actor_id": str(admin.id),
                "db": db,
            },
        )
        assert "error" not in result, f"apply_patch failed: {result}"

        # The orchestrator's enqueue created a queued WorkspaceRun.
        runs_q = await db.execute(
            select(WorkspaceRun).where(
                WorkspaceRun.workspace_id == workspace.id,
                WorkspaceRun.run_type == "suitecloud_validate",
            )
        )
        runs = runs_q.scalars().all()
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "queued"

        # -- Step 3: execute the run; parser picks up 2 warnings from the fixture --
        await runner_service.execute_run(db=db, run_id=run.id, tenant_id=run.tenant_id)

        await db.refresh(run)
        assert run.status == "passed"  # warnings only -> passed
        assert run.has_warnings is True
        assert run.has_errors is False
        assert run.gate_status == "pass"
        assert run.validator_engine == "suitecloud_server"
        assert run.parser_version == "1.0.0"

        # -- Step 4: ValidationHit rows persisted --
        hits_q = await db.execute(select(ValidationHit).where(ValidationHit.run_id == run.id))
        hits = hits_q.scalars().all()
        assert len(hits) == 2
        assert {h.severity for h in hits} == {"warning"}

        # -- Step 5: narration helper groups by family --
        families = _batch_hits_by_family(hits)
        assert "SUITESCRIPT-DEPRECATED-2X" in families
        assert "GOVERNANCE-CHECK" in families
        assert len(families["SUITESCRIPT-DEPRECATED-2X"]) == 1
        assert len(families["GOVERNANCE-CHECK"]) == 1

        # -- Step 6: _maybe_auto_propose_fix records intent for FIXABLE hits only --
        # The classifier allowlist contains SUITESCRIPT-DEPRECATED-2X but NOT
        # GOVERNANCE-CHECK. So orchestrator state mutates once for the deprecated
        # hit and zero times for the governance hit.
        for hit in hits:
            await _maybe_auto_propose_fix(
                hit=hit,
                changeset_id=cs.id,
                tenant_id=workspace.tenant_id,
                user_id=admin.id,
            )

        # Auto-fix count: 1 (only the deprecated-2X hit fired record_auto_fix)
        assert orch._auto_fix_count[cs.id] == 1
        # Proposed fingerprints: 1 entry (the deprecated-2X fingerprint)
        assert len(orch._proposed_fingerprints[cs.id]) == 1
        deprecated_hit = next(h for h in hits if h.code == "SUITESCRIPT-DEPRECATED-2X")
        assert deprecated_hit.fingerprint in orch._proposed_fingerprints[cs.id]

    # Test isolation note: orchestrator state is reset at test start, not
    # cleaned up at end. The singleton survives between tests, but the leading
    # reset means we don't depend on any particular prior state.
