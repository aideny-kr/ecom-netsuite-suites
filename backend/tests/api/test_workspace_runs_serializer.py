"""API serializer contract — runs endpoint MUST emit validate-UX fields.

The frontend's WorkspaceRun type declares 7 new optional fields the backend
populates on suitecloud_validate runs. Without these in the API response,
the ValidationHitsTable always renders empty and the retry button never shows
(Codex P2 / Claude P0 finding).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.workspaces import _serialize_hit, _serialize_run
from app.models.workspace import (
    ValidationHit,
    Workspace,
    WorkspaceChangeSet,
    WorkspaceRun,
)


@pytest.mark.asyncio
async def test_serialize_run_emits_all_validate_fields(db: AsyncSession, tenant_a, admin_user) -> None:
    """suitecloud_validate run with hits → serializer emits the full contract."""
    user, _headers = admin_user  # admin_user fixture returns (user, auth_headers)
    workspace = Workspace(
        tenant_id=tenant_a.id,
        name="ws-serializer",
        description=None,
        status="draft",
        created_by=user.id,
    )
    db.add(workspace)
    await db.flush()

    cs = WorkspaceChangeSet(
        tenant_id=tenant_a.id,
        workspace_id=workspace.id,
        title="Test",
        status="approved",
        proposed_by=user.id,
    )
    db.add(cs)
    await db.flush()

    run = WorkspaceRun(
        tenant_id=tenant_a.id,
        workspace_id=workspace.id,
        changeset_id=cs.id,
        run_type="suitecloud_validate",
        status="failed",
        triggered_by=user.id,
        validator_engine="suitecloud_server",
        parser_version="1.0.0",
        has_errors=True,
        has_warnings=False,
        gate_status="block",
        snapshot_hash="a" * 64,
    )
    db.add(run)
    await db.flush()

    hit = ValidationHit(
        tenant_id=tenant_a.id,
        run_id=run.id,
        file_path="src/Suitelets/foo.js",
        line=42,
        severity="error",
        code="OWASP-A03",
        rule_id="netsuite-owasp-secure-coding/injection",
        message="Unsanitized user input",
        fingerprint="f" * 64,
    )
    db.add(hit)
    await db.flush()
    # Refresh the run so the relationship populates.
    await db.refresh(run, attribute_names=["validation_hits"])

    payload = _serialize_run(run)

    # Original 11 fields still present.
    assert payload["id"] == str(run.id)
    assert payload["run_type"] == "suitecloud_validate"
    assert payload["status"] == "failed"

    # New validate-UX fields populated.
    assert payload["validator_engine"] == "suitecloud_server"
    assert payload["parser_version"] == "1.0.0"
    assert payload["has_errors"] is True
    assert payload["has_warnings"] is False
    assert payload["gate_status"] == "block"
    assert payload["snapshot_hash"] == "a" * 64

    # Findings serialized correctly.
    assert len(payload["findings"]) == 1
    finding = payload["findings"][0]
    assert finding["id"] == str(hit.id)
    assert finding["file_path"] == "src/Suitelets/foo.js"
    assert finding["line"] == 42
    assert finding["severity"] == "error"
    assert finding["code"] == "OWASP-A03"
    assert finding["rule_id"] == "netsuite-owasp-secure-coding/injection"
    assert finding["message"] == "Unsanitized user input"
    assert finding["fingerprint"] == "f" * 64


@pytest.mark.asyncio
async def test_serialize_run_non_validate_run_has_empty_findings(db: AsyncSession, tenant_a, admin_user) -> None:
    """Non-validate run types serialize with findings=[] and None validate fields."""
    user, _headers = admin_user  # admin_user fixture returns (user, auth_headers)
    workspace = Workspace(
        tenant_id=tenant_a.id,
        name="ws-non-validate",
        description=None,
        status="draft",
        created_by=user.id,
    )
    db.add(workspace)
    await db.flush()

    run = WorkspaceRun(
        tenant_id=tenant_a.id,
        workspace_id=workspace.id,
        run_type="jest_unit_test",
        status="passed",
        triggered_by=user.id,
    )
    db.add(run)
    await db.flush()
    await db.refresh(run, attribute_names=["validation_hits"])

    payload = _serialize_run(run)
    assert payload["findings"] == []
    assert payload["validator_engine"] is None
    assert payload["gate_status"] is None
    assert payload["has_errors"] is False  # column default
    assert payload["snapshot_hash"] is None


def test_serialize_run_tolerates_unloaded_relation() -> None:
    """A freshly-created Run with no relationship loaded → findings=[].

    list_runs/get_run use selectinload so the relation is always populated,
    but callers passing the bare row (post-create_run, no flush+refresh) must
    not trigger a lazy load and crash. getattr() pattern handles this.
    """
    from unittest.mock import MagicMock

    fake_run = MagicMock(spec=[])  # No attributes — getattr returns default.
    fake_run.id = uuid.uuid4()
    fake_run.workspace_id = uuid.uuid4()
    fake_run.changeset_id = None
    fake_run.run_type = "suitecloud_validate"
    fake_run.status = "queued"
    fake_run.command = "suitecloud project:validate --server"
    fake_run.exit_code = None
    fake_run.started_at = None
    fake_run.completed_at = None
    fake_run.duration_ms = None
    fake_run.created_at = datetime.now(timezone.utc)
    fake_run.updated_at = datetime.now(timezone.utc)
    fake_run.validator_engine = None
    fake_run.parser_version = None
    fake_run.has_errors = False
    fake_run.has_warnings = False
    fake_run.gate_status = None
    fake_run.snapshot_hash = None
    # Intentionally do NOT set validation_hits — exercises the getattr default.
    fake_run.validation_hits = None

    payload = _serialize_run(fake_run)
    assert payload["findings"] == []


def test_serialize_hit_shape() -> None:
    """ValidationHit serializer matches the frontend's ValidationHit interface."""
    from unittest.mock import MagicMock

    hit = MagicMock()
    hit.id = uuid.uuid4()
    hit.run_id = uuid.uuid4()
    hit.file_path = "src/foo.js"
    hit.line = 10
    hit.severity = "warning"
    hit.code = "SUITESCRIPT-DEPRECATED-2X"
    hit.rule_id = None
    hit.message = "nlapi deprecated"
    hit.fingerprint = "abc" * 22  # 66 chars to confirm 64+ tolerated

    payload = _serialize_hit(hit)
    assert payload["id"] == str(hit.id)
    assert payload["run_id"] == str(hit.run_id)
    assert payload["file_path"] == "src/foo.js"
    assert payload["line"] == 10
    assert payload["severity"] == "warning"
    assert payload["code"] == "SUITESCRIPT-DEPRECATED-2X"
    assert payload["rule_id"] is None
    assert payload["message"] == "nlapi deprecated"
    assert payload["fingerprint"] == "abc" * 22
