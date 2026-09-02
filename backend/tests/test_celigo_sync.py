# backend/tests/test_celigo_sync.py
"""Task 7: `app/services/celigo/sync_service.py` (orchestrator) + `app/workers/
tasks/celigo_flow_map_sync.py` (Celery task + fan-out). See sync_service.py's
module docstring for the design (sequencing, drift detection, purge marking,
freshness-cursor discipline). This file proves it.

Fixtures use SYNTHETIC values only -- same standing rule as
test_celigo_error_signatures.py. The one REAL observed drift shape named in
the brief ("Balance Users to NetSuite" disabled: true -> false) is reproduced
here with an invented flow name/id, never the real one.

TESTING STRATEGY: `list_resource`/`get_resource`/`list_flow_errors_for_step`
are monkeypatched at the point sync_service.py imports them -- client.py has
its own thorough HTTP-layer test suite (test_celigo_client.py: pagination,
retry, sanitization); this file verifies ORCHESTRATION (sequencing, drift,
cursor, purge), not the HTTP layer, mirroring how test_stripe_sync_task.py
fakes `sync_stripe` wholesale rather than mocking httpx.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.encryption import encrypt_credentials
from app.models.celigo import (
    CeligoConfigChange,
    CeligoFlowError,
    CeligoFlowStep,
    CeligoScript,
    CeligoScriptAttachment,
)
from app.services.celigo.client import CeligoIncompleteListingError
from app.services.celigo.repository import (
    extract_flow_steps,
    sync_flow_steps,
    upsert_flow,
    upsert_flow_error,
    upsert_integration,
    upsert_script,
)
from app.services.celigo.sanitizer import sanitize
from app.services.celigo.sync_service import SyncSummary, sync_flow_map_for_connection
from tests.conftest import create_test_tenant

# ---------------------------------------------------------------------------
# Shared fixture helpers -- same pattern as test_celigo_error_signatures.py.
# ---------------------------------------------------------------------------


async def _make_connection(db: AsyncSession, tenant_id, *, status: str = "active") -> uuid.UUID:
    """Raw SQL, not the `Connection` ORM model -- celigo_write_guard.py refuses
    any ORM flush of a provider='celigo' row outside the paired connect/
    disconnect endpoints. Mirrors test_celigo_error_signatures.py's identical
    helper, parameterized on status for the dispatch tests.

    Stores a REAL Fernet-encrypted credentials blob (not a placeholder
    string) -- unlike test_celigo_error_signatures.py's identical-looking
    helper, this file's tests exercise `celigo_flow_map_sync._execute`, which
    calls `decrypt_credentials` for real."""
    conn_id = uuid.uuid4()
    encrypted = encrypt_credentials({"token": "unit-test-not-a-real-token"})
    await db.execute(
        text(
            "INSERT INTO connections (id, tenant_id, provider, label, status, encrypted_credentials, "
            "encryption_key_version) VALUES (:id, :tenant_id, 'celigo', 'Celigo', :status, :creds, 1)"
        ).bindparams(id=conn_id, tenant_id=tenant_id, status=status, creds=encrypted)
    )
    await db.flush()
    return conn_id


def _raw_integration(celigo_id: str, name: str = "Test Integration", *, sandbox: bool | None = None) -> dict:
    """`sandbox` is emitted only when given -- a live Celigo integration always
    carries the flag, but an absent one must be exercised too (see
    TestProductionOnly: absent means production, never hidden)."""
    raw: dict = {"_id": celigo_id, "name": name}
    if sandbox is not None:
        raw["sandbox"] = sandbox
    return raw


def _raw_flow(
    celigo_id: str,
    *,
    integration_id: str,
    name: str = "Test Flow",
    disabled: bool = False,
    schedule: dict | None = None,
    export_id: str = "exp_default",
    filter_json: dict | None = None,
) -> dict:
    processor: dict = {"_exportId": export_id}
    if filter_json is not None:
        processor["filter"] = filter_json
    return {
        "_id": celigo_id,
        "name": name,
        "_integrationId": integration_id,
        "disabled": disabled,
        "schedule": schedule,
        "pageProcessors": [processor],
    }


def _raw_script(celigo_id: str, *, name: str = "Test Script", content: str | None = "console.log(1);") -> dict:
    return {"_id": celigo_id, "name": name, "content": content}


def _raw_export(
    celigo_id: str,
    *,
    name: str = "Test Export",
    adaptor_type: str | None = "NetSuiteExport",
    connection_celigo_id: str | None = "conn_ns_synth",
    transform: dict | None = None,
    record_type: str | None = None,
    search_id: str | None = None,
) -> dict:
    """Shaped like sanitizer.py's `_EXPORT` allowlist (observed-shapes.md:
    `transform` carries either `type: "expression"` -- no script -- or
    `type: "script"` with a nested `script: {_scriptId, function}`).
    `record_type`/`search_id` populate `netsuite.restlet.{recordType,
    searchId}` -- Task 11's export-side provenance input (fix round 2)."""
    obj: dict = {"_id": celigo_id, "name": name, "adaptorType": adaptor_type, "_connectionId": connection_celigo_id}
    if transform is not None:
        obj["transform"] = transform
    if record_type is not None or search_id is not None:
        obj["netsuite"] = {"restlet": {"recordType": record_type, "searchId": search_id}}
    return obj


def _raw_import(
    celigo_id: str,
    *,
    name: str = "Test Import",
    adaptor_type: str | None = "NetSuiteDistributedImport",
    connection_celigo_id: str | None = "conn_ns_synth",
    filter_json: dict | None = None,
    transform: dict | None = None,
    record_type: str | None = None,
    operation: str | None = None,
) -> dict:
    """Shaped like sanitizer.py's `_IMPORT` allowlist. `record_type`/
    `operation` populate `netsuite_da.{recordType, operation}` -- Task 11's
    import-side provenance input."""
    obj: dict = {"_id": celigo_id, "name": name, "adaptorType": adaptor_type, "_connectionId": connection_celigo_id}
    if filter_json is not None:
        obj["filter"] = filter_json
    if transform is not None:
        obj["transform"] = transform
    if record_type is not None or operation is not None:
        obj["netsuite_da"] = {"recordType": record_type, "operation": operation}
    return obj


def _script_ref_transform(script_celigo_id: str, function_name: str = "onTransform") -> dict:
    """A `transform` in SCRIPT form (observed-shapes.md, fix round 2): "the
    most-used script attachment site in the live account"."""
    return {"type": "script", "script": {"_scriptId": script_celigo_id, "function": function_name}}


def _expression_transform() -> dict:
    """A `transform`/`filter` in EXPRESSION form -- live-confirmed negative
    case, carries NO `_scriptId` (observed-shapes.md)."""
    return {"type": "expression", "expression": {"rules": ["a"], "version": "1"}, "rules": ["a"], "version": "1"}


def _raw_error(
    *,
    celigo_id: str,
    source: str | None = "import",
    code: str | None = "TIMEOUT",
    message: str = "Request timed out",
    occurred_at: str | None = "2026-08-20T10:00:00Z",
    purge_at: str = "2026-09-19T10:00:00Z",
) -> dict:
    """Shaped like sanitizer.py's `_ERROR` allowlist -- same convention as
    test_celigo_error_signatures.py's `_raw_error`."""
    return {
        "errorId": celigo_id,
        "traceKey": f"trace_{celigo_id}",
        "retryDataKey": f"retry_{celigo_id}",
        "source": source,
        "code": code,
        "message": message,
        "occurredAt": occurred_at,
        "purgeAt": purge_at,
        "_flowJobId": "job_1",
        "retriable": True,
    }


def _fake_list_resource(data: dict[str, list[dict]]):
    """Async-generator fake for client.list_resource, keyed by `kind`."""

    async def _fake(kind, *, token, region="us", include=None, exclude=None, params=None, client=None):
        for item in data.get(kind, []):
            yield item

    return _fake


def _fake_list_flow_errors_for_step(
    data: dict[tuple[str, str], list[dict]] | None = None,
    calls: list | None = None,
    truncated: dict[tuple[str, str], list[dict]] | None = None,
):
    """*truncated* maps a step to the PARTIAL listing the real fetcher would
    have collected before hitting `_MAX_ERROR_PAGES` -- it raises
    `CeligoIncompleteListingError` carrying exactly that, the way client.py
    does, instead of returning."""
    data = data or {}
    truncated = truncated or {}

    async def _fake(flow_id, step_id, *, token, region="us", client=None):
        if calls is not None:
            calls.append((flow_id, step_id))
        if (flow_id, step_id) in truncated:
            raise CeligoIncompleteListingError(
                f"error listing for flow {flow_id} step {step_id} did not terminate",
                partial_errors=list(truncated[(flow_id, step_id)]),
            )
        return list(data.get((flow_id, step_id), []))

    return _fake


async def _run_sync(
    monkeypatch,
    db: AsyncSession,
    *,
    tenant_id,
    connection_id,
    integrations: list[dict] | None = None,
    flows: list[dict] | None = None,
    scripts: list[dict] | None = None,
    exports: list[dict] | None = None,
    imports: list[dict] | None = None,
    errors_by_step: dict[tuple[str, str], list[dict]] | None = None,
    error_calls: list | None = None,
    truncated_steps: dict[tuple[str, str], list[dict]] | None = None,
) -> SyncSummary:
    resource_data = {
        "integration": integrations or [],
        "flow": flows or [],
        "script": scripts or [],
        "export": exports or [],
        "import": imports or [],
    }
    monkeypatch.setattr(
        "app.services.celigo.sync_service.list_resource",
        _fake_list_resource(resource_data),
    )
    monkeypatch.setattr(
        "app.services.celigo.sync_service.list_flow_errors_for_step",
        _fake_list_flow_errors_for_step(errors_by_step, error_calls, truncated_steps),
    )
    return await sync_flow_map_for_connection(
        db, tenant_id=tenant_id, connection_id=connection_id, token="unit-test-token", region="us"
    )


# ---------------------------------------------------------------------------
# Sequencing: integrations -> flows -> steps -> scripts -> errors per step.
# ---------------------------------------------------------------------------


class TestSyncSequencingAndPersistence:
    async def test_full_pipeline_persists_every_stage(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        error_calls: list = []
        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[
                _raw_flow("flow_1", integration_id="int_1", export_id="exp_1"),
                _raw_flow("flow_2", integration_id="int_1", export_id="exp_2"),
            ],
            scripts=[_raw_script("script_1")],
            errors_by_step={("flow_1", "exp_1"): [_raw_error(celigo_id="err_1")]},
            error_calls=error_calls,
        )

        assert summary.integrations_synced == 1
        assert summary.flows_synced == 2
        assert summary.steps_synced == 2
        assert summary.scripts_synced == 1
        assert summary.steps_with_errors_checked == 2
        assert summary.errors_snapshotted == 1
        assert summary.flows_skipped_no_integration == 0

        integ_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_integrations WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert integ_count == 1

        flow_count = (
            await db.execute(text("SELECT COUNT(*) FROM celigo_flows WHERE tenant_id = :t").bindparams(t=tenant.id))
        ).scalar_one()
        assert flow_count == 2

        step_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_flow_steps WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert step_count == 2

        script_count = (
            await db.execute(text("SELECT COUNT(*) FROM celigo_scripts WHERE tenant_id = :t").bindparams(t=tenant.id))
        ).scalar_one()
        assert script_count == 1

        error_count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_flow_errors WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert error_count == 1

        # "errors per step" ran using REAL, already-upserted step ids -- proof
        # requirement 7 ("make it work end-to-end") actually holds: the error
        # row's flow_step_id/flow_id resolve to real celigo_flow_steps/
        # celigo_flows rows, not left NULL by a broken duck-typed `step`.
        error_row = (
            await db.execute(select(CeligoFlowError).where(CeligoFlowError.tenant_id == tenant.id))
        ).scalar_one()
        assert error_row.flow_step_id is not None
        assert error_row.flow_id is not None
        step_row = (
            await db.execute(select(CeligoFlowStep).where(CeligoFlowStep.id == error_row.flow_step_id))
        ).scalar_one()
        assert step_row.flow_id == error_row.flow_id

        # list_flow_errors_for_step was called once per REAL synced step, with
        # the step's own celigo_id (the referenced export id) as `_stepId` --
        # never with the flow id alone (observed-shapes.md: that mode returns
        # steps: [] even when errors exist).
        assert set(error_calls) == {("flow_1", "exp_1"), ("flow_2", "exp_2")}

    async def test_flow_referencing_unknown_integration_id_is_skipped_gracefully(self, db: AsyncSession, monkeypatch):
        """A flow whose `_integrationId` is missing entirely (malformed) is
        skipped, not fatal -- same posture as extract_flow_steps skipping a
        step with no export/import id."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        malformed_flow = {"_id": "flow_bad", "name": "No integration", "pageProcessors": []}
        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[malformed_flow],
        )

        assert summary.flows_synced == 0
        assert summary.flows_skipped_no_integration == 1


# ---------------------------------------------------------------------------
# Drift detection -- disabled/schedule (flow), mapping_json/filter_json
# (step), content_hash (script).
# ---------------------------------------------------------------------------


class TestProductionOnly:
    """Operator directive 2026-09-01: "don't bring sandbox celigo, just
    production". Phase A skips a `sandbox: true` integration, Phase B skips
    every flow under it, and -- the part that is easy to get wrong -- a flow
    whose `_integrationId` names a skipped sandbox integration must NOT reach
    `_resolve_integration_id`'s listing-gap fallback, which would fetch and
    upsert that very integration straight back in.

    On the live account this halves the work: 19 of 36 integrations and 118
    of 239 flows were sandbox copies, each flow costing a per-step error
    listing call."""

    async def test_sandbox_integrations_and_their_flows_are_skipped(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        fallback_fetches: list[tuple[str, str]] = []

        async def _fallback_must_not_run(kind, celigo_id, *, token, region="us", client=None):
            fallback_fetches.append((kind, celigo_id))
            raise AssertionError(f"listing-gap fallback fetched {kind} {celigo_id}")

        monkeypatch.setattr("app.services.celigo.sync_service.get_resource", _fallback_must_not_run)

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[
                _raw_integration("int_prod", name="Production", sandbox=False),
                _raw_integration("int_sb", name="Sandbox Copy", sandbox=True),
                # No flag at all: production. Hiding on an absent field would
                # let a missing key silently erase real integrations.
                _raw_integration("int_legacy", name="Legacy"),
            ],
            flows=[
                _raw_flow("flow_prod", integration_id="int_prod", export_id="exp_1"),
                _raw_flow("flow_sb", integration_id="int_sb", export_id="exp_2"),
                _raw_flow("flow_legacy", integration_id="int_legacy", export_id="exp_3"),
            ],
        )

        assert summary.integrations_synced == 2
        assert summary.integrations_skipped_sandbox == 1
        assert summary.flows_synced == 2
        assert summary.flows_skipped_sandbox == 1
        assert summary.flows_skipped_no_integration == 0, "a sandbox skip is its own count, not a listing gap"
        assert fallback_fetches == []

        names = (
            (
                await db.execute(
                    text("SELECT name FROM celigo_integrations WHERE tenant_id = :t ORDER BY name").bindparams(
                        t=tenant.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert names == ["Legacy", "Production"]
        flow_ids = (
            (
                await db.execute(
                    text("SELECT celigo_id FROM celigo_flows WHERE tenant_id = :t ORDER BY celigo_id").bindparams(
                        t=tenant.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert flow_ids == ["flow_legacy", "flow_prod"]

    async def test_a_sandbox_integration_synced_before_this_rule_is_purged(self, db: AsyncSession, monkeypatch):
        """The staging DB already holds 19 sandbox integrations (118 flows, their
        steps and attachments) from syncs that predate this rule. A clean run
        removes them so the DB matches the product promise; the FK CASCADEs
        (`app/models/celigo.py`) take the dependent rows with them."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        old_sb_id = await upsert_integration(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            sanitized={"_id": "int_old_sb", "name": "Old Sandbox", "sandbox": True},
        )
        await upsert_flow(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integration_id=old_sb_id,
            sanitized={"_id": "flow_old_sb", "name": "Old Sandbox Flow"},
        )
        await db.flush()

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_prod", name="Production", sandbox=False)],
            flows=[_raw_flow("flow_prod", integration_id="int_prod", export_id="exp_1")],
        )

        assert summary.integrations_purged_sandbox == 1
        names = (
            (
                await db.execute(
                    text("SELECT name FROM celigo_integrations WHERE tenant_id = :t").bindparams(t=tenant.id)
                )
            )
            .scalars()
            .all()
        )
        assert names == ["Production"]
        flow_ids = (
            (await db.execute(text("SELECT celigo_id FROM celigo_flows WHERE tenant_id = :t").bindparams(t=tenant.id)))
            .scalars()
            .all()
        )
        assert flow_ids == ["flow_prod"], "the sandbox flow must go with its integration (FK CASCADE)"


class TestProductionOnlyHoldsAcrossKindsAndTime:
    """PR #216 gate, round 1. The first cut enforced "production only" with
    three separate mechanisms that each covered one kind (integrations) and
    one moment (this run). Every test here is a way that shape leaked."""

    async def test_an_integration_that_flips_to_sandbox_is_purged_despite_its_stored_flag(
        self, db: AsyncSession, monkeypatch
    ):
        """GATE FINDING (major): Phase A never upserts a sandbox integration, so a
        row stored as production by an earlier sync keeps `sandbox=false`
        forever after Celigo flips it -- and a purge keyed on the STORED flag
        alone never touches it. The purge must also be driven by what THIS run
        saw."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        stored_id = await upsert_integration(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            sanitized={"_id": "int_flip", "name": "Was Production", "sandbox": False},
        )
        await upsert_flow(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integration_id=stored_id,
            sanitized={"_id": "flow_flip", "name": "Flip Flow"},
        )
        await db.flush()

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[
                _raw_integration("int_flip", name="Was Production", sandbox=True),
                _raw_integration("int_prod", name="Production", sandbox=False),
            ],
            flows=[
                _raw_flow("flow_flip", integration_id="int_flip", export_id="exp_1"),
                _raw_flow("flow_prod", integration_id="int_prod", export_id="exp_2"),
            ],
        )

        assert summary.integrations_purged_sandbox == 1
        assert summary.flows_skipped_sandbox == 1
        names = (
            (
                await db.execute(
                    text("SELECT name FROM celigo_integrations WHERE tenant_id = :t").bindparams(t=tenant.id)
                )
            )
            .scalars()
            .all()
        )
        assert names == ["Production"]
        flow_ids = (
            (await db.execute(text("SELECT celigo_id FROM celigo_flows WHERE tenant_id = :t").bindparams(t=tenant.id)))
            .scalars()
            .all()
        )
        assert flow_ids == ["flow_prod"]

    async def test_purging_a_sandbox_integration_leaves_its_flow_errors_as_audit_rows(
        self, db: AsyncSession, monkeypatch
    ):
        """Two review rounds, opposite conclusions, and the second one is right.

        Round 1 (codex, the independent-model angle): `celigo_flow_errors.
        flow_id` is ON DELETE SET NULL, not CASCADE, so the purge's docstring
        overclaimed that errors cascade away. True. The fix drawn from it --
        delete the rows explicitly -- was wrong.

        Round 2 (blocker): `celigo_flow_errors` is THE audit trail (design
        spec G2, `CeligoFlowError`'s own docstring: "NEVER DELETE A ROW
        HERE"). SET NULL is not an accident to work around; it is the design.
        An error must outlive its flow, its integration and its connection,
        the same way it outlives Celigo's own ~30-day purge. So a purged
        sandbox flow's errors stay, with `flow_id NULL`, exactly as they
        would if the flow were deleted for any other reason -- and they are
        counted nowhere, because every open-error count joins through a flow.
        """
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        sb_id = await upsert_integration(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            sanitized={"_id": "int_sb", "name": "Sandbox", "sandbox": True},
        )
        sb_flow_id = await upsert_flow(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integration_id=sb_id,
            sanitized={"_id": "flow_sb", "name": "Sandbox Flow"},
        )
        db.add(
            CeligoFlowError(
                tenant_id=tenant.id,
                celigo_connection_id=conn_id,
                flow_id=sb_flow_id,
                celigo_id="err_sb",
                message="synthetic sandbox error",
                occurred_at=datetime.now(timezone.utc),
            )
        )
        await db.flush()

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_prod", name="Production", sandbox=False)],
            flows=[_raw_flow("flow_prod", integration_id="int_prod", export_id="exp_1")],
        )

        assert summary.integrations_purged_sandbox == 1
        surviving = (
            await db.execute(
                text("SELECT celigo_id, flow_id, purged_at FROM celigo_flow_errors WHERE tenant_id = :t").bindparams(
                    t=tenant.id
                )
            )
        ).all()
        assert [(row.celigo_id, row.flow_id) for row in surviving] == [("err_sb", None)], (
            "the audit row must survive its flow's purge, with flow_id SET NULL -- never deleted"
        )
        # GATE ROUND 3: the row survives, but it must not keep counting as an
        # OPEN error -- an error signature's occurrence recompute
        # (`errors.py`) only looks at `celigo_error_is_open()`, so an orphan
        # left open would inflate a production signature it happens to share
        # a fingerprint with, forever. `purged_at` is the state transition
        # the model allows for "gone from what we track" (an UPDATE, which
        # the never-delete pin permits); the purge stamps it.
        assert surviving[0].purged_at is not None, "orphaned audit rows are marked purged, not left open"

    async def test_sandbox_scripts_are_skipped_and_previously_synced_ones_purged(self, db: AsyncSession, monkeypatch):
        """SINGLE-AGENT REVIEW: Phase C synced every script regardless of
        environment -- 132 of the live account's 259 scripts are sandbox
        copies, and the script viewer's clone-family counts summed both
        environments. Scripts carry their own `sandbox` flag (sanitizer
        `_SCRIPT` allowlist), so the one classifier applies to them too."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        await upsert_script(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            sanitized={"_id": "scr_old_sb", "name": "Old Sandbox Script", "content": "x", "sandbox": True},
        )
        await db.flush()

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_prod", name="Production", sandbox=False)],
            flows=[_raw_flow("flow_prod", integration_id="int_prod", export_id="exp_1")],
            scripts=[_raw_script("scr_prod"), {**_raw_script("scr_sb"), "sandbox": True}],
        )

        assert summary.scripts_synced == 1
        assert summary.scripts_skipped_sandbox == 1
        assert summary.scripts_purged_sandbox == 1
        remaining = (
            (
                await db.execute(
                    text("SELECT celigo_id FROM celigo_scripts WHERE tenant_id = :t").bindparams(t=tenant.id)
                )
            )
            .scalars()
            .all()
        )
        assert remaining == ["scr_prod"]

    async def test_a_sandbox_export_is_skipped_even_when_a_flow_references_it(self, db: AsyncSession, monkeypatch):
        """Exports/imports carry `sandbox` too. The classifier sits at the
        ingestion boundary for EVERY kind, so a kind cannot be forgotten the
        way scripts were in the first cut."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_prod", name="Production", sandbox=False)],
            flows=[_raw_flow("flow_prod", integration_id="int_prod", export_id="exp_sb")],
            exports=[{**_raw_export("exp_sb"), "sandbox": True}],
        )

        assert summary.exports_imports_skipped_sandbox == 1
        assert summary.exports_imports_synced == 0


class TestProductionOnlyIsOneSeam:
    """GATE ROUND 3 (nit, but the shape that produced every round's major):
    sandbox classification lived in four near-identical `if _is_sandbox(x)`
    blocks, one per kind. Now every object of every kind enters the sync
    through `_list_production`, so a kind cannot be listed without passing
    the classifier -- and a flow object that carries the flag ITSELF (none
    observed live, but the seam is kind-agnostic) is skipped the same way."""

    async def test_a_flow_object_flagged_sandbox_is_skipped_even_under_a_production_integration(
        self, db: AsyncSession, monkeypatch
    ):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_prod", name="Production", sandbox=False)],
            flows=[
                {**_raw_flow("flow_flagged", integration_id="int_prod", export_id="exp_1"), "sandbox": True},
                _raw_flow("flow_prod", integration_id="int_prod", export_id="exp_2"),
            ],
        )

        assert summary.flows_synced == 1
        assert summary.flows_skipped_sandbox == 1
        flow_ids = (
            (await db.execute(text("SELECT celigo_id FROM celigo_flows WHERE tenant_id = :t").bindparams(t=tenant.id)))
            .scalars()
            .all()
        )
        assert flow_ids == ["flow_prod"]


class TestDriftDetection:
    async def test_first_sync_of_a_flow_records_no_drift(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_1", integration_id="int_1", disabled=True)],
        )
        assert summary.config_changes_recorded == 0
        count = (await db.execute(text("SELECT COUNT(*) FROM celigo_config_changes"))).scalar_one()
        assert count == 0

    async def test_flow_disabled_flip_is_recorded_real_observed_shape_synthetic_id(self, db: AsyncSession, monkeypatch):
        """Reproduces the brief's named real drift ('Balance Users to
        NetSuite' disabled: true -> false between two syncs) with an INVENTED
        flow name/id -- never the real one."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_synth_1", integration_id="int_1", name="Synthetic Sync Flow", disabled=True)],
        )
        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_synth_1", integration_id="int_1", name="Synthetic Sync Flow", disabled=False)],
        )

        changes = (
            (await db.execute(select(CeligoConfigChange).where(CeligoConfigChange.tenant_id == tenant.id)))
            .scalars()
            .all()
        )
        assert len(changes) == 1
        change = changes[0]
        assert change.object_kind == "flow"
        assert change.field == "disabled"
        assert change.old_value is True
        assert change.new_value is False
        assert change.celigo_id == "flow_synth_1"
        assert change.flow_id is not None

    async def test_schedule_change_is_recorded(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_1", integration_id="int_1", schedule={"cron": "0 2 * * *"})],
        )
        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_1", integration_id="int_1", schedule={"cron": "0 5 * * *"})],
        )

        change = (
            await db.execute(
                select(CeligoConfigChange).where(
                    CeligoConfigChange.tenant_id == tenant.id, CeligoConfigChange.field == "schedule"
                )
            )
        ).scalar_one()
        assert change.old_value == {"cron": "0 2 * * *"}
        assert change.new_value == {"cron": "0 5 * * *"}

    async def test_step_filter_json_change_is_recorded(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[
                _raw_flow(
                    "flow_1",
                    integration_id="int_1",
                    export_id="exp_1",
                    filter_json={"type": "expression", "rules": ["a"]},
                )
            ],
        )
        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[
                _raw_flow(
                    "flow_1",
                    integration_id="int_1",
                    export_id="exp_1",
                    filter_json={"type": "expression", "rules": ["b"]},
                )
            ],
        )

        change = (
            await db.execute(
                select(CeligoConfigChange).where(
                    CeligoConfigChange.tenant_id == tenant.id, CeligoConfigChange.field == "filter_json"
                )
            )
        ).scalar_one()
        assert change.object_kind == "flow_step"
        assert change.old_value == {"type": "expression", "rules": ["a"]}
        assert change.new_value == {"type": "expression", "rules": ["b"]}
        assert change.flow_id is not None

    async def test_script_content_hash_change_is_recorded(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        await _run_sync(
            monkeypatch, db, tenant_id=tenant.id, connection_id=conn_id, scripts=[_raw_script("script_1", content="v1")]
        )
        await _run_sync(
            monkeypatch, db, tenant_id=tenant.id, connection_id=conn_id, scripts=[_raw_script("script_1", content="v2")]
        )

        change = (
            await db.execute(
                select(CeligoConfigChange).where(
                    CeligoConfigChange.tenant_id == tenant.id, CeligoConfigChange.field == "content_hash"
                )
            )
        ).scalar_one()
        assert change.object_kind == "script"
        assert change.flow_id is None
        assert change.old_value != change.new_value
        script_row = (await db.execute(select(CeligoScript).where(CeligoScript.tenant_id == tenant.id))).scalar_one()
        assert change.new_value == script_row.content_hash

    async def test_script_with_no_content_on_either_side_records_no_false_drift(self, db: AsyncSession, monkeypatch):
        """A list-mode fetch that omits `content` on both syncs must never
        look like a content edit -- content_hash stays NULL both times."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        await _run_sync(
            monkeypatch, db, tenant_id=tenant.id, connection_id=conn_id, scripts=[_raw_script("script_1", content=None)]
        )
        await _run_sync(
            monkeypatch, db, tenant_id=tenant.id, connection_id=conn_id, scripts=[_raw_script("script_1", content=None)]
        )

        count = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM celigo_config_changes WHERE tenant_id = :t AND field = 'content_hash'"
                ).bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert count == 0

    async def test_unchanged_flow_records_no_drift_on_resync(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        for _ in range(2):
            await _run_sync(
                monkeypatch,
                db,
                tenant_id=tenant.id,
                connection_id=conn_id,
                integrations=[_raw_integration("int_1")],
                flows=[_raw_flow("flow_1", integration_id="int_1", disabled=False)],
            )

        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_config_changes WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert count == 0


# ---------------------------------------------------------------------------
# Fix round 1: export/import fetch phase -- script attachments +
# celigo_flow_steps.adaptor_type/connection_celigo_id backfill. The refs live
# on the EXPORT/IMPORT object (transform.script, filter.script), not on the
# flow -- a flow only references them by id. See sync_service.py's module
# docstring for the full "one missing stage explains three flagged gaps"
# reasoning.
# ---------------------------------------------------------------------------


class TestExportImportAttachmentsAndStepBackfill:
    async def test_script_form_transform_produces_an_attachment_row(self, db: AsyncSession, monkeypatch):
        """THE regression case (observed-shapes.md, plan's Verified Facts):
        transform.script is the most-used script attachment site live. A
        flow-only walk finds nothing here -- the ref lives on the export."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_1", integration_id="int_1", export_id="exp_1")],
            exports=[_raw_export("exp_1", transform=_script_ref_transform("script_ref_1"))],
        )

        assert summary.attachments_synced == 1

        from app.models.celigo import CeligoScriptAttachment

        attachment = (
            await db.execute(select(CeligoScriptAttachment).where(CeligoScriptAttachment.tenant_id == tenant.id))
        ).scalar_one()
        assert attachment.script_celigo_id == "script_ref_1"
        assert attachment.function_name == "onTransform"
        assert attachment.site_type == "transform"
        # Qualified with the export the ref was walked off (finding 1): the
        # walker emits `transform.script`, which is relative to exp_1, not to
        # the flow, and two exports in one flow would otherwise collide. See
        # TestTwoReferenceObjectsInOneFlowKeepSeparateAttachments.
        assert attachment.json_path == "exp_1.transform.script"
        assert attachment.flow_id is not None

    async def test_expression_form_transform_and_filter_produce_no_attachment(self, db: AsyncSession, monkeypatch):
        """Both live-confirmed negative cases: type:'expression' carries NO
        _scriptId on either an export's transform or an import's filter."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_1", integration_id="int_1", export_id="exp_1", filter_json=_expression_transform())],
            exports=[_raw_export("exp_1", transform=_expression_transform())],
            imports=[_raw_import("exp_1", filter_json=_expression_transform())],
        )

        count = (await db.execute(text("SELECT COUNT(*) FROM celigo_script_attachments"))).scalar_one()
        assert count == 0

    async def test_step_backfill_across_two_flows_sharing_one_export(self, db: AsyncSession, monkeypatch):
        """The SAME export id referenced by steps in TWO DIFFERENT flows --
        both flow-step rows get backfilled, and TWO attachment rows are
        created (one per flow), per the team lead's explicit ruling."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[
                _raw_flow("flow_a", integration_id="int_1", export_id="exp_shared"),
                _raw_flow("flow_b", integration_id="int_1", export_id="exp_shared"),
            ],
            exports=[
                _raw_export(
                    "exp_shared",
                    adaptor_type="NetSuiteExport",
                    connection_celigo_id="conn_ns_shared",
                    transform=_script_ref_transform("script_shared"),
                )
            ],
        )

        assert summary.flow_steps_backfilled == 2
        assert summary.attachments_synced == 2  # one per flow

        steps = (await db.execute(select(CeligoFlowStep).where(CeligoFlowStep.tenant_id == tenant.id))).scalars().all()
        assert len(steps) == 2
        for step in steps:
            assert step.adaptor_type == "NetSuiteExport"
            assert step.connection_celigo_id == "conn_ns_shared"

        from app.models.celigo import CeligoScriptAttachment

        attachments = (
            (await db.execute(select(CeligoScriptAttachment).where(CeligoScriptAttachment.tenant_id == tenant.id)))
            .scalars()
            .all()
        )
        assert len(attachments) == 2
        assert {a.flow_id for a in attachments} == {s.flow_id for s in steps}

    async def test_backfill_never_blanks_a_previously_known_value(self, db: AsyncSession, monkeypatch):
        """A resync where the export fetch happens to omit adaptorType must
        not wipe out a value a PREVIOUS sync already learned."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_1", integration_id="int_1", export_id="exp_1")],
            exports=[_raw_export("exp_1", adaptor_type="NetSuiteExport")],
        )
        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_1", integration_id="int_1", export_id="exp_1")],
            exports=[_raw_export("exp_1", adaptor_type=None)],
        )

        step = (await db.execute(select(CeligoFlowStep).where(CeligoFlowStep.tenant_id == tenant.id))).scalar_one()
        assert step.adaptor_type == "NetSuiteExport"

    async def test_export_import_stage_is_idempotent(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        for _ in range(2):
            await _run_sync(
                monkeypatch,
                db,
                tenant_id=tenant.id,
                connection_id=conn_id,
                integrations=[_raw_integration("int_1")],
                flows=[_raw_flow("flow_1", integration_id="int_1", export_id="exp_1")],
                exports=[_raw_export("exp_1", transform=_script_ref_transform("script_ref_1"))],
            )

        attachment_count = (await db.execute(text("SELECT COUNT(*) FROM celigo_script_attachments"))).scalar_one()
        assert attachment_count == 1  # not 2

        step = (await db.execute(select(CeligoFlowStep).where(CeligoFlowStep.tenant_id == tenant.id))).scalar_one()
        assert step.adaptor_type == "NetSuiteExport"

    async def test_export_with_no_referencing_flow_step_is_skipped_not_fatal(self, db: AsyncSession, monkeypatch):
        """An export nobody's synced step currently references (a listing
        gap, or genuinely unused) cannot be attached to any flow --
        celigo_script_attachments.flow_id is NOT NULL -- so it is skipped,
        never a crash."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            exports=[_raw_export("exp_orphan", transform=_script_ref_transform("script_x"))],
        )

        assert summary.attachments_synced == 0
        assert summary.exports_imports_skipped_no_flow == 1
        count = (await db.execute(text("SELECT COUNT(*) FROM celigo_script_attachments"))).scalar_one()
        assert count == 0

    async def test_captured_payload_on_export_does_not_survive_into_anything_stored(
        self, db: AsyncSession, monkeypatch
    ):
        """Composition property (Task 1's FIX ROUND 2/4): sanitize() must
        strip payload fields while PRESERVING the script ref. Feeds a
        payload-shaped export through the real sanitizer (mimicking exactly
        what client.py does before this module ever sees it), then confirms
        neither the sanitized object nor anything this phase stores carries
        the captured value."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        raw_export_with_payload = {
            "_id": "exp_1",
            "name": "Test Export",
            "adaptorType": "NetSuiteExport",
            "_connectionId": "conn_ns_synth",
            "transform": _script_ref_transform("script_ref_1"),
            "mockOutput": {"_headers": {"set-cookie": "SENSITIVE_CAPTURED_VALUE"}},
            "rawData": "SENSITIVE_CAPTURED_VALUE",
        }
        sanitized_export = sanitize("export", raw_export_with_payload)
        assert "mockOutput" not in sanitized_export  # sanitizer's own guarantee, reasserted here
        assert "rawData" not in sanitized_export

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_1", integration_id="int_1", export_id="exp_1")],
            exports=[sanitized_export],
        )

        from app.models.celigo import CeligoScriptAttachment

        attachment = (
            await db.execute(select(CeligoScriptAttachment).where(CeligoScriptAttachment.tenant_id == tenant.id))
        ).scalar_one()
        assert attachment.script_celigo_id == "script_ref_1"  # the ref survived sanitization

        # Nothing stored anywhere carries the captured value -- there is no
        # raw_json sink for exports/imports at all (no celigo_exports table),
        # and the attachment/step columns only ever hold ids/paths/hashes.
        step = (await db.execute(select(CeligoFlowStep).where(CeligoFlowStep.tenant_id == tenant.id))).scalar_one()
        assert "SENSITIVE_CAPTURED_VALUE" not in repr(step.raw_json)
        assert "SENSITIVE_CAPTURED_VALUE" not in repr(attachment.json_path)
        assert "SENSITIVE_CAPTURED_VALUE" not in repr(attachment.function_name)

    async def test_flow_level_router_script_ref_is_captured(self, db: AsyncSession, monkeypatch):
        """The brief also asks to walk the FLOW object itself (routers can
        carry `script`). Every router observed live has NO `_scriptId`
        (site_type 'router', function 'branching') -- this test does NOT
        claim that shape is observed reality (it demonstrably isn't); it
        proves the walker's generic coverage using a SYNTHETIC router script
        ref, on the one schema-permitted flow-level site
        (sanitizer.py's `_FLOW["routers"]` -> `_ROUTER["script"]` --
        `_FLOW` has no top-level `hooks` key at all, so that is not a real
        site to fixture against)."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        flow = _raw_flow("flow_1", integration_id="int_1", export_id="exp_1")
        flow["routers"] = [
            {
                "id": "router_1",
                "name": "",
                "routeRecordsTo": "first_matching_branch",
                "routeRecordsUsing": "script",
                "branches": [],
                "script": {"_scriptId": "script_router_1", "function": "branching"},
            }
        ]

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[flow],
        )

        assert summary.attachments_synced == 1
        from app.models.celigo import CeligoScriptAttachment

        attachment = (
            await db.execute(select(CeligoScriptAttachment).where(CeligoScriptAttachment.tenant_id == tenant.id))
        ).scalar_one()
        assert attachment.script_celigo_id == "script_router_1"
        assert attachment.flow_step_id is None  # a flow-level ref, not scoped to one step
        assert attachment.site_type == "router"


class TestFlowLevelAttachmentScriptIdIsBackfilled:
    """WHOLE-BRANCH REVIEW FINDING 6 (2026-08-27, PROVEN across two full
    syncs -- not a first-run artifact): Phase B records flow/router-level
    attachments (`_record_attachments` for the flow object itself) BEFORE
    Phase C has synced any script, so `script_ids` -- reset to `{}` at the
    top of Phase B every run -- is always empty at that call site.
    `script_id` therefore stayed NULL forever, even after the referenced
    script itself synced successfully in the SAME run (via Phase C, which
    runs later) and on every subsequent resync (Phase B always runs before
    Phase C again). `sync_flow_map_for_connection` now backfills flow/router-
    level attachments' `script_id` once Phase C's script_ids map is
    populated, mirroring Phase D's export/import-level attachments, which
    already got a real `script_id` because Phase D runs AFTER Phase C."""

    async def test_router_level_attachment_script_id_resolves_once_the_script_syncs_same_run(
        self, db: AsyncSession, monkeypatch
    ):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        flow = _raw_flow("flow_1", integration_id="int_1", export_id="exp_1")
        flow["routers"] = [
            {
                "id": "router_1",
                "name": "",
                "routeRecordsTo": "first_matching_branch",
                "routeRecordsUsing": "script",
                "branches": [],
                "script": {"_scriptId": "script_router_1", "function": "branching"},
            }
        ]

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[flow],
            # The referenced script DOES sync, in the SAME run, via Phase C
            # -- proving this is not a "script hasn't synced yet" gap.
            scripts=[_raw_script("script_router_1")],
        )

        script_local_id = (
            await db.execute(select(CeligoScript.id).where(CeligoScript.tenant_id == tenant.id))
        ).scalar_one()
        attachment = (
            await db.execute(select(CeligoScriptAttachment).where(CeligoScriptAttachment.tenant_id == tenant.id))
        ).scalar_one()
        assert attachment.script_id == script_local_id

    async def test_router_level_attachment_script_id_resolves_after_a_second_sync(self, db: AsyncSession, monkeypatch):
        """PROVEN ACROSS TWO SYNCS, per the finding: the bug is not a
        first-run artifact -- Phase B always runs before Phase C, on every
        resync, so a naive "it'll catch up next time" fix would not actually
        converge without this backfill."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        flow = _raw_flow("flow_1", integration_id="int_1", export_id="exp_1")
        flow["routers"] = [
            {
                "id": "router_1",
                "name": "",
                "routeRecordsTo": "first_matching_branch",
                "routeRecordsUsing": "script",
                "branches": [],
                "script": {"_scriptId": "script_router_1", "function": "branching"},
            }
        ]

        for _ in range(2):
            await _run_sync(
                monkeypatch,
                db,
                tenant_id=tenant.id,
                connection_id=conn_id,
                integrations=[_raw_integration("int_1")],
                flows=[flow],
                scripts=[_raw_script("script_router_1")],
            )

        script_local_id = (
            await db.execute(select(CeligoScript.id).where(CeligoScript.tenant_id == tenant.id))
        ).scalar_one()
        attachment = (
            await db.execute(select(CeligoScriptAttachment).where(CeligoScriptAttachment.tenant_id == tenant.id))
        ).scalar_one()
        assert attachment.script_id == script_local_id


# ---------------------------------------------------------------------------
# Fix round 2: NetSuite provenance (Task 11's data dependency). Phase D
# already fetches the export/import object -- these fields were fetched and
# thrown away because nothing persisted them. `netsuite_da.{recordType,
# operation}` (imports) / `netsuite.restlet.{recordType, searchId}`
# (exports) land on dedicated `celigo_flow_steps` columns (design call: see
# task-7-report.md for the raw_json-vs-dedicated-columns reasoning).
# ---------------------------------------------------------------------------


class TestNetSuiteProvenanceBackfill:
    async def test_import_backed_step_carries_record_type_and_operation(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        # `export_id` in `_raw_flow` just names "the referenced id" on the
        # pageProcessor -- `extract_flow_steps` keys a step's `celigo_id` on
        # whichever of `_exportId`/`_importId` is present, and Phase D's
        # export_import_flow_steps map is keyed on that celigo_id alone, not
        # on which key it came from -- so reusing it to point at an id the
        # IMPORT loop will fetch is correct, not a fixture shortcut.
        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_1", integration_id="int_1", export_id="imp_1")],
            imports=[_raw_import("imp_1", record_type="customer", operation="add")],
        )

        step = (await db.execute(select(CeligoFlowStep).where(CeligoFlowStep.tenant_id == tenant.id))).scalar_one()
        assert step.record_type == "customer"
        assert step.operation == "add"
        assert step.search_id is None  # import-side never populates the export-only field

    async def test_export_backed_step_carries_record_type_and_search_id(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[_raw_flow("flow_1", integration_id="int_1", export_id="exp_1")],
            exports=[_raw_export("exp_1", record_type="salesorder", search_id="customsearch_so_export")],
        )

        step = (await db.execute(select(CeligoFlowStep).where(CeligoFlowStep.tenant_id == tenant.id))).scalar_one()
        assert step.record_type == "salesorder"
        assert step.search_id == "customsearch_so_export"
        assert step.operation is None  # export-side never populates the import-only field

    async def test_provenance_backfill_is_idempotent_and_never_blanked(self, db: AsyncSession, monkeypatch):
        """Same NULL-wipe class the adaptor_type/connection_celigo_id fix
        already closed once -- proving the new fields inherit that guard
        rather than reopening the bug for themselves."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        for _ in range(2):
            await _run_sync(
                monkeypatch,
                db,
                tenant_id=tenant.id,
                connection_id=conn_id,
                integrations=[_raw_integration("int_1")],
                flows=[_raw_flow("flow_1", integration_id="int_1", export_id="exp_1")],
                exports=[_raw_export("exp_1", record_type="salesorder", search_id="cs1")],
            )

        step_count = (await db.execute(text("SELECT COUNT(*) FROM celigo_flow_steps"))).scalar_one()
        assert step_count == 1  # not duplicated

        step = (await db.execute(select(CeligoFlowStep).where(CeligoFlowStep.tenant_id == tenant.id))).scalar_one()
        assert step.record_type == "salesorder"  # not blanked
        assert step.search_id == "cs1"


# ---------------------------------------------------------------------------
# Purge marking -- Task 6 explicitly left this undone; Task 7 owns it.
# ---------------------------------------------------------------------------


class TestPurgeMarking:
    async def _seed_error(self, db: AsyncSession, tenant_id, conn_id, *, celigo_id: str, purge_at: datetime) -> None:
        integration_id = await upsert_integration(
            db, tenant_id=tenant_id, connection_id=conn_id, sanitized=sanitize("integration", _raw_integration("int_p"))
        )
        flow_id = await upsert_flow(
            db,
            tenant_id=tenant_id,
            connection_id=conn_id,
            integration_id=integration_id,
            sanitized=sanitize("flow", _raw_flow("flow_p", integration_id="int_p", export_id="exp_p")),
        )
        flow = sanitize("flow", _raw_flow("flow_p", integration_id="int_p", export_id="exp_p"))
        steps = extract_flow_steps(flow)
        step_ids = await sync_flow_steps(db, tenant_id=tenant_id, connection_id=conn_id, flow_id=flow_id, steps=steps)
        await upsert_flow_error(
            db,
            tenant_id=tenant_id,
            connection_id=conn_id,
            celigo_id=celigo_id,
            flow_id=flow_id,
            flow_step_id=step_ids[0],
            purge_at=purge_at,
        )
        await db.flush()

    async def test_expired_error_is_marked_purged_not_deleted(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        expired_at = datetime.now(timezone.utc) - timedelta(days=1)
        await self._seed_error(db, tenant.id, conn_id, celigo_id="err_expired", purge_at=expired_at)

        summary = await _run_sync(monkeypatch, db, tenant_id=tenant.id, connection_id=conn_id)

        assert summary.errors_purged == 1
        row = (await db.execute(select(CeligoFlowError).where(CeligoFlowError.celigo_id == "err_expired"))).scalar_one()
        assert row.purged_at is not None  # marked, never deleted
        count = (await db.execute(text("SELECT COUNT(*) FROM celigo_flow_errors"))).scalar_one()
        assert count == 1  # row still exists

    async def test_not_yet_expired_error_is_left_alone(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        future = datetime.now(timezone.utc) + timedelta(days=10)
        await self._seed_error(db, tenant.id, conn_id, celigo_id="err_future", purge_at=future)

        summary = await _run_sync(monkeypatch, db, tenant_id=tenant.id, connection_id=conn_id)

        assert summary.errors_purged == 0
        row = (await db.execute(select(CeligoFlowError).where(CeligoFlowError.celigo_id == "err_future"))).scalar_one()
        assert row.purged_at is None


class TestOneTruncatedStepIsContainedNotFatal:
    """FIX ROUND 9 (scoped re-review R1b, 2026-08-27). Fix round 8 made
    `list_flow_errors_for_step` raise instead of truncating silently -- right
    call, wrong blast radius: ONE step exceeding `_MAX_ERROR_PAGES` aborted
    the ENTIRE connection sync (phases A-E), on every run, until a human
    intervened. Meanwhile the per-step escape hatch that exists precisely for
    this (`upsert_errors(raw_errors_is_complete=False)`) had zero callers.

    Two invariants, both proven below against the real orchestrator:

      1. A truncated step can NEVER resolve an error. Its listing is
         admittedly partial, so absence from it means nothing.
      2. One truncated step cannot discard every OTHER step's results. The
         sync continues, and the run reports that it was partial rather than
         reporting nothing at all.
    """

    async def test_a_truncated_step_neither_resolves_nor_aborts_the_other_steps(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        flows = [
            _raw_flow("flow_trunc", integration_id="int_t", export_id="exp_trunc"),
            _raw_flow("flow_ok", integration_id="int_t", export_id="exp_ok"),
        ]

        # Sync 1 -- everything complete. The soon-to-be-truncated step has two
        # open errors; only one of them comes back in sync 2.
        first = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_t")],
            flows=flows,
            errors_by_step={
                ("flow_trunc", "exp_trunc"): [
                    _raw_error(celigo_id="err_trunc_seen"),
                    _raw_error(celigo_id="err_trunc_absent"),
                ],
            },
        )
        assert first.errors_snapshotted == 2
        await db.flush()

        # Sync 2 -- the FIRST step's listing truncates (it is first in flow
        # order, so an abort here would take every later step with it). Its
        # partial page carries one already-known error plus one brand-new
        # one; the second flow's step answers normally.
        error_calls: list = []
        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_t")],
            flows=flows,
            errors_by_step={("flow_ok", "exp_ok"): [_raw_error(celigo_id="err_ok_new")]},
            truncated_steps={
                ("flow_trunc", "exp_trunc"): [
                    _raw_error(celigo_id="err_trunc_seen"),
                    _raw_error(celigo_id="err_trunc_partial_new"),
                ]
            },
            error_calls=error_calls,
        )
        await db.flush()

        # Invariant 2: the sync ran to completion and every step was visited.
        assert set(error_calls) == {("flow_trunc", "exp_trunc"), ("flow_ok", "exp_ok")}
        assert summary.steps_with_errors_checked == 2
        assert summary.steps_with_incomplete_errors == 1

        # Invariant 1: absence from an admittedly-partial listing resolves
        # nothing.
        absent = (
            await db.execute(select(CeligoFlowError).where(CeligoFlowError.celigo_id == "err_trunc_absent"))
        ).scalar_one()
        assert absent.resolved_at is None

        # The partial page's own errors are still RECORDED -- "incomplete"
        # means "cannot conclude an absence", not "throw the data away".
        partial_new = (
            await db.execute(select(CeligoFlowError).where(CeligoFlowError.celigo_id == "err_trunc_partial_new"))
        ).scalar_one_or_none()
        assert partial_new is not None

        # And the step AFTER the truncated one was snapshotted normally.
        ok_new = (
            await db.execute(select(CeligoFlowError).where(CeligoFlowError.celigo_id == "err_ok_new"))
        ).scalar_one_or_none()
        assert ok_new is not None

    async def test_a_clean_run_reports_zero_incomplete_steps(self, db: AsyncSession, monkeypatch):
        """The counter is what makes a partial run VISIBLE, so it must be
        honest in the ordinary case too -- a field that is always non-zero
        surfaces nothing."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_c")],
            flows=[_raw_flow("flow_c", integration_id="int_c", export_id="exp_c")],
            errors_by_step={("flow_c", "exp_c"): [_raw_error(celigo_id="err_c")]},
        )

        assert summary.steps_with_incomplete_errors == 0
        assert summary.errors_snapshotted == 1


# ---------------------------------------------------------------------------
# Celery task layer: freshness cursor advances only on success; a
# status='error' connection is still attempted (not pre-filtered).
# ---------------------------------------------------------------------------


class TestFreshnessCursorAdvancesOnlyOnSuccess:
    @staticmethod
    @asynccontextmanager
    async def _fake_worker_session(db):
        yield db

    async def test_a_failing_sync_never_writes_a_cursor_row(self, db: AsyncSession, monkeypatch):
        from app.workers.tasks import celigo_flow_map_sync as task_module

        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        async def _raising_list_resource(
            kind, *, token, region="us", include=None, exclude=None, params=None, client=None
        ):
            raise RuntimeError("simulated Celigo outage")
            yield  # pragma: no cover -- makes this an async generator

        monkeypatch.setattr("app.services.celigo.sync_service.list_resource", _raising_list_resource)
        monkeypatch.setattr(task_module, "worker_async_session", lambda: self._fake_worker_session(db))

        with pytest.raises(RuntimeError, match="simulated Celigo outage"):
            await task_module._execute(str(tenant.id), str(conn_id))

        cursor_count = (
            await db.execute(text("SELECT COUNT(*) FROM cursor_states WHERE connection_id = :c").bindparams(c=conn_id))
        ).scalar_one()
        assert cursor_count == 0

    async def test_a_successful_sync_writes_a_cursor_row(self, db: AsyncSession, monkeypatch):
        from app.workers.tasks import celigo_flow_map_sync as task_module

        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        monkeypatch.setattr(
            "app.services.celigo.sync_service.list_resource",
            _fake_list_resource({"integration": [], "flow": [], "script": []}),
        )
        monkeypatch.setattr(task_module, "worker_async_session", lambda: self._fake_worker_session(db))

        result = await task_module._execute(str(tenant.id), str(conn_id))
        assert result["integrations_synced"] == 0

        cursor = (
            await db.execute(
                text("SELECT last_synced_at FROM cursor_states WHERE connection_id = :c").bindparams(c=conn_id)
            )
        ).scalar_one()
        assert cursor is not None

    async def test_no_matching_connection_raises_before_any_sync_attempt(self, db: AsyncSession, monkeypatch):
        from app.workers.tasks import celigo_flow_map_sync as task_module
        from app.workers.tasks.celigo_flow_map_sync import CeligoSyncFailedError

        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        monkeypatch.setattr(task_module, "worker_async_session", lambda: self._fake_worker_session(db))

        with pytest.raises(CeligoSyncFailedError):
            await task_module._execute(str(tenant.id), str(uuid.uuid4()))


class TestErrorStatusConnectionIsStillAttempted:
    """Per DISPATCHABLE_CONNECTION_STATUSES / the 2026-07-29 incident: a
    connection in status='error' must not be silently pre-filtered out of the
    single-connection task's own lookup -- only `revoked` is excluded."""

    async def test_error_status_connection_is_looked_up_not_skipped(self, db: AsyncSession, monkeypatch):
        from app.workers.tasks import celigo_flow_map_sync as task_module

        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id, status="error")

        monkeypatch.setattr(
            "app.services.celigo.sync_service.list_resource",
            _fake_list_resource({"integration": [], "flow": [], "script": []}),
        )
        monkeypatch.setattr(
            task_module,
            "worker_async_session",
            lambda: TestFreshnessCursorAdvancesOnlyOnSuccess._fake_worker_session(db),
        )

        result = await task_module._execute(str(tenant.id), str(conn_id))
        assert result["integrations_synced"] == 0  # proves the sync was ATTEMPTED, not skipped

    async def test_revoked_connection_is_not_attempted(self, db: AsyncSession, monkeypatch):
        from app.workers.tasks import celigo_flow_map_sync as task_module
        from app.workers.tasks.celigo_flow_map_sync import CeligoSyncFailedError

        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id, status="revoked")

        monkeypatch.setattr(
            task_module,
            "worker_async_session",
            lambda: TestFreshnessCursorAdvancesOnlyOnSuccess._fake_worker_session(db),
        )

        with pytest.raises(CeligoSyncFailedError):
            await task_module._execute(str(tenant.id), str(conn_id))


class TestFanoutDispatchableStatuses:
    """Mirrors tests/workers/test_sync_all_fanout.py's pattern EXACTLY, but
    with raw-SQL-seeded celigo connections (celigo_write_guard refuses the
    ORM `Connection(...)` construction that file uses for stripe/netsuite)."""

    @pytest.fixture
    def sync_db(self):
        from app.workers.base_task import sync_engine

        with sync_engine.connect() as conn:
            trans = conn.begin()
            session = Session(bind=conn)
            try:
                yield session
            finally:
                session.close()
                trans.rollback()

    @staticmethod
    def _make_tenant_sync(session):
        from app.models.tenant import Tenant

        tenant = Tenant(
            name="Celigo Fanout Test Corp",
            slug=f"test-{uuid.uuid4().hex[:8]}",
            plan="free",
            plan_expires_at=datetime.now(timezone.utc) + timedelta(days=14),
            is_active=True,
        )
        session.add(tenant)
        session.flush()
        return tenant

    @staticmethod
    def _make_celigo_connection_sync(session, tenant_id, *, status: str):
        conn_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO connections (id, tenant_id, provider, label, status, encrypted_credentials, "
                "encryption_key_version) VALUES (:id, :tenant_id, 'celigo', 'Celigo', :status, "
                "'unit-test-not-a-real-token', 1)"
            ).bindparams(id=conn_id, tenant_id=tenant_id, status=status)
        )
        session.flush()
        return conn_id

    def test_dispatches_error_status_skips_revoked(self, sync_db):
        from app.workers.tasks.celigo_flow_map_sync import _find_dispatchable_celigo_connections

        active_tenant = self._make_tenant_sync(sync_db)
        error_tenant = self._make_tenant_sync(sync_db)
        revoked_tenant = self._make_tenant_sync(sync_db)
        self._make_celigo_connection_sync(sync_db, active_tenant.id, status="active")
        self._make_celigo_connection_sync(sync_db, error_tenant.id, status="error")
        self._make_celigo_connection_sync(sync_db, revoked_tenant.id, status="revoked")

        result = _find_dispatchable_celigo_connections(sync_db)
        tenant_ids = {row["tenant_id"] for row in result}

        assert str(active_tenant.id) in tenant_ids
        assert str(error_tenant.id) in tenant_ids
        assert str(revoked_tenant.id) not in tenant_ids

    def test_only_celigo_provider_rows_are_returned(self, sync_db):
        """A dispatchable non-celigo connection (e.g. stripe) must never leak
        into the celigo fan-out -- provider scoping, not just status."""
        from app.workers.tasks.celigo_flow_map_sync import _find_dispatchable_celigo_connections

        tenant = self._make_tenant_sync(sync_db)
        self._make_celigo_connection_sync(sync_db, tenant.id, status="active")
        sync_db.execute(
            text(
                "INSERT INTO connections (id, tenant_id, provider, label, status, encrypted_credentials, "
                "encryption_key_version) VALUES (:id, :tenant_id, 'stripe', 'Stripe', 'active', 'blob', 1)"
            ).bindparams(id=uuid.uuid4(), tenant_id=tenant.id)
        )
        sync_db.flush()

        result = _find_dispatchable_celigo_connections(sync_db)
        assert len(result) == 1


class TestTaskRegistration:
    """Step 1 instruction: 'read an existing InstrumentedTask sync end-to-end
    first -- task registration...'. Pins that both tasks are actually
    registered with Celery (importable + decorated), and that NEITHER is on
    the Beat schedule (human ruling)."""

    def test_both_tasks_are_registered_with_celery(self):
        from app.workers.celery_app import celery_app
        from app.workers.tasks import celigo_flow_map_sync  # noqa: F401

        assert "tasks.celigo_flow_map_sync" in celery_app.tasks
        assert "tasks.celigo_flow_map_sync_all" in celery_app.tasks

    def test_neither_task_is_on_the_beat_schedule(self):
        from app.workers.celery_app import celery_app
        from app.workers.tasks import celigo_flow_map_sync  # noqa: F401

        scheduled_task_names = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
        assert "tasks.celigo_flow_map_sync" not in scheduled_task_names
        assert "tasks.celigo_flow_map_sync_all" not in scheduled_task_names


class TestTwoReferenceObjectsInOneFlowKeepSeparateAttachments:
    """FINAL REVIEW finding 1 -- silent data loss in the core deliverable.

    `celigo_script_attachments`' unique key is `(tenant_id, flow_id,
    json_path)`. Phase D walks each export/import object SEPARATELY, so
    `ScriptRef.json_path` is relative to THAT object (`transform.script`),
    not to the flow. Two imports in one flow that each carry a script at
    `transform.script` therefore collided on `(flow_id, json_path)`, and the
    second `ON CONFLICT DO UPDATE` overwrote the first -- including
    `script_celigo_id`, the answer to "which script is attached here". No
    error, no warning; the sync reported success.

    `_process_reference_object`'s docstring reasoned carefully about
    one-export-many-flows and got it right. Nobody considered
    many-exports-one-flow -- and a NetSuite flow with two or more
    import/export steps is the ordinary case, while `transform.script` is
    "the most-used script attachment site in the live account" per the plan's
    own Verified Facts.

    THE FIX, and why it needed no migration: `json_path` is qualified with
    the celigo id of the object it was walked from, so it is unique within
    the flow BY CONSTRUCTION and the existing constraint stands unchanged.
    Adding `flow_step_id` to the unique key instead would have reintroduced
    NULL-is-distinct (that column is nullable -- router-level refs have no
    step), which is exactly the trap `celigo_flow_steps` already solved with
    a STORED GENERATED `branch_key`.
    """

    @staticmethod
    def _flow_with_two_imports() -> dict:
        return {
            "_id": "flow_multi",
            "name": "Multi-step NetSuite flow",
            "_integrationId": "int_1",
            "pageProcessors": [
                {"type": "import", "_importId": "imp_1"},
                {"type": "import", "_importId": "imp_2"},
            ],
        }

    @staticmethod
    def _two_imports() -> list[dict]:
        return [
            _raw_import("imp_1", transform=_script_ref_transform("SCRIPT_AAA", "onFirst")),
            _raw_import("imp_2", transform=_script_ref_transform("SCRIPT_BBB", "onSecond")),
        ]

    async def _attachments(self, db: AsyncSession, tenant_id):
        from app.models.celigo import CeligoScriptAttachment

        return (
            (await db.execute(select(CeligoScriptAttachment).where(CeligoScriptAttachment.tenant_id == tenant_id)))
            .scalars()
            .all()
        )

    async def test_two_imports_in_one_flow_keep_both_script_attachments(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        summary = await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[self._flow_with_two_imports()],
            imports=self._two_imports(),
        )

        assert summary.attachments_synced == 2
        attachments = await self._attachments(db, tenant.id)
        assert len(attachments) == 2
        # NEITHER script may be lost: before the fix SCRIPT_AAA was gone and
        # SCRIPT_BBB sat in its row.
        assert {a.script_celigo_id for a in attachments} == {"SCRIPT_AAA", "SCRIPT_BBB"}
        assert {a.function_name for a in attachments} == {"onFirst", "onSecond"}
        # Both rows belong to the one flow -- this is not a case of them
        # being separated by landing under different flows.
        assert len({a.flow_id for a in attachments}) == 1

    async def test_qualified_json_path_names_the_object_the_ref_was_found_on(self, db: AsyncSession, monkeypatch):
        """`json_path` is IDENTITY (it is in the unique key), so what makes
        the two rows distinct has to be visible in the stored value -- and
        that value is what the script viewer renders in its "Where" column."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[self._flow_with_two_imports()],
            imports=self._two_imports(),
        )

        attachments = await self._attachments(db, tenant.id)
        by_script = {a.script_celigo_id: a for a in attachments}
        assert by_script["SCRIPT_AAA"].json_path == "imp_1.transform.script"
        assert by_script["SCRIPT_BBB"].json_path == "imp_2.transform.script"
        # site_type still comes from the walker's own unqualified path --
        # qualifying must not change how a ref is classified.
        assert {a.site_type for a in attachments} == {"transform"}

    async def test_two_full_syncs_are_idempotent(self, db: AsyncSession, monkeypatch):
        """The qualified path must be STABLE, not merely unique: a second
        full sync has to update the same two rows, never insert two more."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        for _ in range(2):
            await _run_sync(
                monkeypatch,
                db,
                tenant_id=tenant.id,
                connection_id=conn_id,
                integrations=[_raw_integration("int_1")],
                flows=[self._flow_with_two_imports()],
                imports=self._two_imports(),
            )

        attachments = await self._attachments(db, tenant.id)
        assert len(attachments) == 2
        assert {a.script_celigo_id for a in attachments} == {"SCRIPT_AAA", "SCRIPT_BBB"}
        assert {a.json_path for a in attachments} == {"imp_1.transform.script", "imp_2.transform.script"}

    async def test_flow_level_refs_keep_unqualified_flow_relative_paths(self, db: AsyncSession, monkeypatch):
        """A ref found on the FLOW object itself is already flow-relative and
        must NOT be qualified -- there is no owning export/import, and
        qualifying it would make the path depend on which object the walk
        happened to start from."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        flow = {
            "_id": "flow_router",
            "name": "Flow with a router script",
            "_integrationId": "int_1",
            "routers": [
                {
                    "id": "rtr_1",
                    "script": {"_scriptId": "SCRIPT_ROUTER", "function": "branching"},
                    "branches": [{"name": "b", "branchId": "b1", "pageProcessors": []}],
                }
            ],
        }

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_1")],
            flows=[flow],
        )

        attachment = (await self._attachments(db, tenant.id))[0]
        assert attachment.script_celigo_id == "SCRIPT_ROUTER"
        assert attachment.json_path == "routers[0].script"
        assert attachment.flow_step_id is None


class TestAttachmentPathsAreIndexBasedNotStable:
    """SCOPED RE-REVIEW R5 (2026-08-27). `repository.qualify_json_path` claimed
    its output is "deterministic and stable across syncs -- both halves come
    from Celigo's own ids and the walker's own path, neither of which changes
    run to run". The second half is FALSE: a flow-relative path is
    INDEX-BEARING (`routers[0].script`), so removing a router shifts every
    later router's path down one.

    This test EXECUTES the two syncs and pins what actually happens, because
    the claim was disproven that way: asserting the docstring's version --
    one attachment, `routers[0].script -> script_b` -- fails with
    `Left contains one more item: ('routers[1].script', 'script_b')`.

    THE ROOT CAUSE IS PRE-EXISTING AND DELIBERATELY NOT FIXED HERE (team lead
    ruling, 2026-08-27): index-bearing walker paths plus the absence of any
    prune -- there is no DELETE of `celigo_script_attachments` anywhere in the
    package -- so a shrinking flow leaves the vacated path behind, now
    over-written to point at the script that shifted into it. The flow map
    therefore shows one script attached at two router sites. This test exists
    so the gap is VISIBLE and executable rather than described, and so
    whoever adds a prune has a red test the moment they do."""

    async def test_removing_a_router_leaves_a_phantom_attachment_pre_existing_gap(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        def _flow_with(routers: list[dict]) -> dict:
            flow = _raw_flow("flow_paths", integration_id="int_paths", export_id="exp_paths")
            flow["routers"] = routers
            return flow

        def _router(router_id: str, script_celigo_id: str) -> dict:
            return {
                "id": router_id,
                "name": "",
                "routeRecordsTo": "first_matching_branch",
                "routeRecordsUsing": "script",
                "branches": [],
                "script": {"_scriptId": script_celigo_id, "function": "branching"},
            }

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_paths")],
            flows=[_flow_with([_router("rtr_1", "script_a"), _router("rtr_2", "script_b")])],
        )
        await db.flush()

        await _run_sync(
            monkeypatch,
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            integrations=[_raw_integration("int_paths")],
            flows=[_flow_with([_router("rtr_2", "script_b")])],
        )
        await db.flush()

        rows = (
            await db.execute(
                select(CeligoScriptAttachment.json_path, CeligoScriptAttachment.script_celigo_id).where(
                    CeligoScriptAttachment.tenant_id == tenant.id
                )
            )
        ).all()
        # WHAT ACTUALLY HAPPENS, not what the old docstring promised: the
        # surviving router shifted from index 1 to index 0, so its attachment
        # was written at the path the REMOVED router used to own, and the
        # vacated `routers[1].script` row was never pruned -- it now claims
        # the same script is attached at a second, non-existent site.
        assert sorted((r.json_path, r.script_celigo_id) for r in rows) == [
            ("routers[0].script", "script_b"),
            ("routers[1].script", "script_b"),
        ]
