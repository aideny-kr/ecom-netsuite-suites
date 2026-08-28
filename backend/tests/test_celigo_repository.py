# backend/tests/test_celigo_repository.py
"""Task 5: `app/services/celigo/repository.py` -- idempotent upsert functions
over the seven `celigo_*` tables (migration 094) plus the two behaviors the
brief calls out as headline tests:

1. Upsert is idempotent: syncing the same (sanitized) payload twice yields
   ONE row, never a duplicate-key error.
2. `(source_id, content_hash)` dedup collapses ~20 clones of one script to a
   single logical script with N attachments (`list_logical_scripts`).

Also covers the non-negotiable behaviors called out for this task:
  * `celigo_flow_steps`' unique key omits `role` -- a genuine collision
    (same export id, same flow, same branch, DIFFERENT role) must surface as
    a loud, uncaught exception, never a silent overwrite.
  * `json_path` is identity on `celigo_script_attachments`, not decoration --
    two different paths never collide, the exact same path always upserts.
  * `celigo_flow_errors` rows are never deleted -- only resolved_at/purged_at
    transitions exist.

Fixtures use SYNTHETIC values only (no real Celigo payloads) -- shapes are
taken from `.superpowers/sdd/2026-08-25-celigo-plan-b-flow-map/
observed-shapes.md`, values are made up.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy.exc
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.celigo import CeligoFlowError, CeligoScriptAttachment
from app.services.celigo.graph import walk_script_refs
from app.services.celigo.repository import (
    FlowStepRoleCollisionError,
    extract_flow_steps,
    list_logical_scripts,
    mark_flow_errors_purged,
    mark_flow_errors_resolved,
    sync_flow_steps,
    upsert_error_signature,
    upsert_flow,
    upsert_flow_error,
    upsert_integration,
    upsert_script,
    upsert_script_attachment,
    upsert_script_attachment_from_ref,
)
from app.services.celigo.sanitizer import sanitize
from tests.conftest import create_test_tenant


async def _make_connection(db: AsyncSession, tenant_id) -> uuid.UUID:
    """Raw SQL, not the `Connection` ORM model -- `celigo_write_guard.py`
    refuses any ORM flush of a provider='celigo' row outside the paired
    connect/disconnect endpoints; raw SQL below the ORM is its documented,
    accepted escape hatch for tests. Mirrors `test_celigo_flow_map_rls.py`'s
    identical helper."""
    conn_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO connections (id, tenant_id, provider, label, status, encrypted_credentials, encryption_key_version) "
            "VALUES (:id, :tenant_id, 'celigo', 'Celigo', 'active', 'unit-test-not-a-real-token', 1)"
        ).bindparams(id=conn_id, tenant_id=tenant_id)
    )
    await db.flush()
    return conn_id


async def _seed_integration_and_flow(db: AsyncSession, tenant_id, connection_id, *, flow_suffix: str):
    """Seed one integration + one flow via the repository's own upsert
    functions (not raw SQL) -- these two are prerequisites for everything
    else in this file, and exercising them here doubles as their own
    idempotency coverage."""
    integration_id = await upsert_integration(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        sanitized=sanitize("integration", {"_id": f"int_{flow_suffix}", "name": "Test Integration"}),
    )
    flow_id = await upsert_flow(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        integration_id=integration_id,
        sanitized=sanitize(
            "flow",
            {"_id": f"flow_{flow_suffix}", "name": "Test Flow", "_integrationId": f"int_{flow_suffix}"},
        ),
    )
    await db.flush()
    return integration_id, flow_id


class TestUpsertIsIdempotent:
    """Headline test #1: syncing the same sanitized payload twice yields one
    row, no duplicate-key error -- across every table that owns a Celigo
    identity."""

    async def test_upsert_integration_twice_yields_one_row(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        payload = sanitize("integration", {"_id": "int_1", "name": "Framework NetSuite", "sandbox": False})

        id1 = await upsert_integration(db, tenant_id=tenant.id, connection_id=conn_id, sanitized=payload)
        id2 = await upsert_integration(db, tenant_id=tenant.id, connection_id=conn_id, sanitized=payload)
        await db.flush()

        assert id1 == id2
        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_integrations WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert count == 1

    async def test_upsert_flow_twice_yields_one_row_and_updates_fields(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        integration_id, _ = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="fixed")

        payload_v1 = sanitize("flow", {"_id": "flow_x", "name": "V1", "_integrationId": "int_x", "disabled": False})
        payload_v2 = sanitize("flow", {"_id": "flow_x", "name": "V2", "_integrationId": "int_x", "disabled": True})

        id1 = await upsert_flow(
            db, tenant_id=tenant.id, connection_id=conn_id, integration_id=integration_id, sanitized=payload_v1
        )
        id2 = await upsert_flow(
            db, tenant_id=tenant.id, connection_id=conn_id, integration_id=integration_id, sanitized=payload_v2
        )
        await db.flush()

        assert id1 == id2
        row = (
            await db.execute(text("SELECT name, disabled FROM celigo_flows WHERE id = :id").bindparams(id=id1))
        ).first()
        assert row.name == "V2"
        assert row.disabled is True

    async def test_upsert_script_twice_yields_one_row(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        payload = sanitize("script", {"_id": "scr_1", "name": "BigQuery Warehouse Script", "content": "function() {}"})

        id1 = await upsert_script(db, tenant_id=tenant.id, connection_id=conn_id, sanitized=payload)
        id2 = await upsert_script(db, tenant_id=tenant.id, connection_id=conn_id, sanitized=payload)
        await db.flush()

        assert id1 == id2
        count = (
            await db.execute(text("SELECT COUNT(*) FROM celigo_scripts WHERE tenant_id = :t").bindparams(t=tenant.id))
        ).scalar_one()
        assert count == 1

    async def test_upsert_script_attachment_twice_yields_one_row(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="att")

        kwargs = dict(
            tenant_id=tenant.id,
            connection_id=conn_id,
            flow_id=flow_id,
            flow_step_id=None,
            script_id=None,
            script_celigo_id="scr_att_1",
            function_name="preSavePage",
            json_path="pageProcessors[0].hooks.preSavePage",
            reference_object_celigo_id=None,
            site_type="hook",
        )
        id1 = await upsert_script_attachment(db, **kwargs)
        id2 = await upsert_script_attachment(db, **kwargs)
        await db.flush()

        assert id1 == id2
        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_script_attachments WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert count == 1

    async def test_upsert_error_signature_twice_yields_one_row(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        id1 = await upsert_error_signature(db, tenant_id=tenant.id, connection_id=conn_id, fingerprint="fp_1")
        id2 = await upsert_error_signature(db, tenant_id=tenant.id, connection_id=conn_id, fingerprint="fp_1")
        await db.flush()

        assert id1 == id2
        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_error_signatures WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert count == 1

    async def test_upsert_flow_error_twice_yields_one_row(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        id1 = await upsert_flow_error(db, tenant_id=tenant.id, connection_id=conn_id, celigo_id="err_1", code="E1")
        id2 = await upsert_flow_error(db, tenant_id=tenant.id, connection_id=conn_id, celigo_id="err_1", code="E1")
        await db.flush()

        assert id1 == id2
        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM celigo_flow_errors WHERE tenant_id = :t").bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert count == 1


class TestFlowStepExtractionWalksRouterBranches:
    """The trap: steps live in top-level pageGenerators/pageProcessors AND in
    routers[].branches[].pageProcessors. A flat two-level reader would
    silently miss the router-branch ones -- exactly the flows the recon
    chain depends on per observed-shapes.md."""

    def test_top_level_generators_and_processors_are_extracted(self):
        flow = sanitize(
            "flow",
            {
                "_id": "flow_1",
                "pageGenerators": [{"_exportId": "exp_1", "skipRetries": True}],
                "pageProcessors": [{"type": "import", "_importId": "imp_1"}],
            },
        )
        steps = extract_flow_steps(flow)
        assert len(steps) == 2
        gen = next(s for s in steps if s.role == "generator")
        assert gen.celigo_id == "exp_1"
        assert gen.router_id is None
        assert gen.branch_id is None
        proc = next(s for s in steps if s.role == "processor")
        assert proc.celigo_id == "imp_1"

    def test_router_branch_processors_are_extracted(self):
        """Live-observed shape (observed-shapes.md "routers" section) --
        synthetic ids, real structure."""
        flow = sanitize(
            "flow",
            {
                "_id": "flow_2",
                "pageGenerators": [{"_exportId": "exp_root"}],
                "routers": [
                    {
                        "id": "router_1",
                        "routeRecordsTo": "first_matching_branch",
                        "branches": [
                            {
                                "name": "Subsidiary A",
                                "branchId": "branch_a",
                                "pageProcessors": [{"type": "import", "_importId": "imp_a"}],
                            },
                            {
                                "name": "Subsidiary B",
                                "branchId": "branch_b",
                                "pageProcessors": [{"type": "import", "_importId": "imp_b"}],
                            },
                        ],
                    }
                ],
            },
        )
        steps = extract_flow_steps(flow)
        celigo_ids = {s.celigo_id for s in steps}
        assert celigo_ids == {"exp_root", "imp_a", "imp_b"}

        branch_a_step = next(s for s in steps if s.celigo_id == "imp_a")
        assert branch_a_step.router_id == "router_1"
        assert branch_a_step.branch_id == "branch_a"
        assert branch_a_step.role == "processor"

    def test_a_flat_reader_would_have_missed_the_router_branch_steps(self):
        """Guards the regression this whole module exists to avoid: asserts
        the top-level arrays ALONE do not contain the router-branch step,
        proving extract_flow_steps must (and does) walk routers."""
        flow = sanitize(
            "flow",
            {
                "_id": "flow_3",
                "pageGenerators": [],
                "pageProcessors": [],
                "routers": [
                    {
                        "id": "router_1",
                        "branches": [{"branchId": "branch_only", "pageProcessors": [{"_importId": "imp_only"}]}],
                    }
                ],
            },
        )
        assert flow.get("pageProcessors") == []  # a flat reader would see nothing here
        steps = extract_flow_steps(flow)
        assert [s.celigo_id for s in steps] == ["imp_only"]


class TestFlowStepUpsertIdempotentAndLoudOnRoleCollision:
    async def test_same_step_synced_twice_yields_one_row(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="step1")

        flow = sanitize("flow", {"_id": "f", "pageGenerators": [{"_exportId": "exp_1"}]})
        steps = extract_flow_steps(flow)

        ids1 = await sync_flow_steps(db, tenant_id=tenant.id, connection_id=conn_id, flow_id=flow_id, steps=steps)
        ids2 = await sync_flow_steps(db, tenant_id=tenant.id, connection_id=conn_id, flow_id=flow_id, steps=steps)
        await db.flush()

        assert ids1 == ids2
        count = (
            await db.execute(text("SELECT COUNT(*) FROM celigo_flow_steps WHERE flow_id = :f").bindparams(f=flow_id))
        ).scalar_one()
        assert count == 1

    async def test_top_level_and_router_branch_steps_both_persist(self, db: AsyncSession):
        """Proves the extraction -> storage path preserves router-branch
        steps end to end, not just in the pure walker."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="step2")

        flow = sanitize(
            "flow",
            {
                "_id": "f",
                "pageGenerators": [{"_exportId": "exp_root"}],
                "routers": [
                    {
                        "id": "r1",
                        "branches": [{"branchId": "b1", "pageProcessors": [{"_importId": "imp_b1"}]}],
                    }
                ],
            },
        )
        steps = extract_flow_steps(flow)
        await sync_flow_steps(db, tenant_id=tenant.id, connection_id=conn_id, flow_id=flow_id, steps=steps)
        await db.flush()

        rows = (
            await db.execute(
                text("SELECT celigo_id, branch_key FROM celigo_flow_steps WHERE flow_id = :f").bindparams(f=flow_id)
            )
        ).all()
        by_celigo_id = {r.celigo_id: r.branch_key for r in rows}
        assert by_celigo_id == {"exp_root": "$root", "imp_b1": "b1"}

    async def test_same_export_id_different_role_same_branch_raises_loudly(self, db: AsyncSession):
        """THE non-negotiable: the unique key omits `role`. If the same
        export id is claimed as BOTH a generator and a processor in one
        flow/branch, that must surface as a loud, uncaught exception -- never
        a silent overwrite of one role's row by the other's."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="collide")

        flow = sanitize(
            "flow",
            {
                "_id": "f",
                "pageGenerators": [{"_exportId": "exp_shared"}],
                "pageProcessors": [{"_exportId": "exp_shared", "type": "export"}],
            },
        )
        steps = extract_flow_steps(flow)
        assert {s.role for s in steps} == {"generator", "processor"}
        assert len({s.celigo_id for s in steps}) == 1  # both claim exp_shared

        with pytest.raises(FlowStepRoleCollisionError):
            await sync_flow_steps(db, tenant_id=tenant.id, connection_id=conn_id, flow_id=flow_id, steps=steps)


class TestScriptAttachmentJsonPathIsIdentity:
    async def test_different_json_paths_do_not_collide(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="paths")

        await upsert_script_attachment(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            flow_id=flow_id,
            flow_step_id=None,
            script_id=None,
            script_celigo_id="scr_1",
            function_name="preSavePage",
            json_path="pageProcessors[0].hooks.preSavePage",
            reference_object_celigo_id=None,
            site_type="hook",
        )
        await upsert_script_attachment(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            flow_id=flow_id,
            flow_step_id=None,
            script_id=None,
            script_celigo_id="scr_1",  # SAME script, DIFFERENT attach site
            function_name="preSavePage",
            json_path="pageProcessors[1].hooks.preSavePage",
            reference_object_celigo_id=None,
            site_type="hook",
        )
        await db.flush()

        count = (
            (await db.execute(select(CeligoScriptAttachment).where(CeligoScriptAttachment.flow_id == flow_id)))
            .scalars()
            .all()
        )
        assert len(count) == 2

    async def test_same_path_on_two_reference_objects_does_not_collide(self, db: AsyncSession):
        """FINAL REVIEW finding 1, at the repository layer. `walk_script_refs`
        emits a path relative to the object it walked, so two exports in one
        flow that each carry `transform.script` hand this function the SAME
        `json_path`. Qualifying with the owning object's celigo id is what
        keeps them two rows instead of one silent overwrite -- see
        `qualify_json_path`. (`test_celigo_sync.py`'s
        TestTwoReferenceObjectsInOneFlowKeepSeparateAttachments proves the
        same thing through the real orchestrator.)"""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="collide")

        for reference_object, script in (("imp_1", "scr_aaa"), ("imp_2", "scr_bbb")):
            await upsert_script_attachment(
                db,
                tenant_id=tenant.id,
                connection_id=conn_id,
                flow_id=flow_id,
                flow_step_id=None,
                script_id=None,
                script_celigo_id=script,
                function_name="onTransform",
                json_path="transform.script",  # IDENTICAL, as the walker emits it
                reference_object_celigo_id=reference_object,
                site_type="transform",
            )
        await db.flush()

        rows = (
            (await db.execute(select(CeligoScriptAttachment).where(CeligoScriptAttachment.flow_id == flow_id)))
            .scalars()
            .all()
        )
        assert {r.script_celigo_id for r in rows} == {"scr_aaa", "scr_bbb"}
        assert {r.json_path for r in rows} == {"imp_1.transform.script", "imp_2.transform.script"}

    async def test_qualification_is_stable_so_reupserting_updates_in_place(self, db: AsyncSession):
        """Unique is not enough -- the qualified path must also be the SAME
        string next run, or every nightly sync inserts a duplicate."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="stable")

        kwargs = dict(
            tenant_id=tenant.id,
            connection_id=conn_id,
            flow_id=flow_id,
            flow_step_id=None,
            script_id=None,
            script_celigo_id="scr_stable",
            function_name="onTransform",
            json_path="transform.script",
            reference_object_celigo_id="imp_1",
            site_type="transform",
        )
        first = await upsert_script_attachment(db, **kwargs)
        second = await upsert_script_attachment(db, **kwargs)
        await db.flush()

        assert first == second
        rows = (
            (await db.execute(select(CeligoScriptAttachment).where(CeligoScriptAttachment.flow_id == flow_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].json_path == "imp_1.transform.script"

    async def test_flow_relative_paths_are_stored_unqualified(self, db: AsyncSession):
        """A ref found on the flow object itself has no owning export/import;
        qualifying it would make identity depend on where the walk began."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="unqualified")

        await upsert_script_attachment(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            flow_id=flow_id,
            flow_step_id=None,
            script_id=None,
            script_celigo_id="scr_router",
            function_name="branching",
            json_path="routers[0].script",
            reference_object_celigo_id=None,
            site_type="router",
        )
        await db.flush()

        row = (
            await db.execute(select(CeligoScriptAttachment).where(CeligoScriptAttachment.flow_id == flow_id))
        ).scalar_one()
        assert row.json_path == "routers[0].script"

    async def test_walk_script_refs_output_stores_one_attachment_per_ref(self, db: AsyncSession):
        """Ties Task 2's walker directly to Task 5's storage, per the
        'WHERE THIS FITS' integration point."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="refs")

        flow = sanitize(
            "flow",
            {
                "_id": "f",
                "pageProcessors": [
                    {
                        "type": "export",
                        "_exportId": "exp_1",
                        "transform": {"type": "script", "script": {"_scriptId": "scr_transform", "function": "run"}},
                    }
                ],
            },
        )
        refs = walk_script_refs(flow)
        assert len(refs) == 1

        for ref in refs:
            await upsert_script_attachment_from_ref(
                db,
                tenant_id=tenant.id,
                connection_id=conn_id,
                flow_id=flow_id,
                flow_step_id=None,
                ref=ref,
                reference_object_celigo_id=None,
            )
        await db.flush()

        row = (
            await db.execute(
                text("SELECT script_celigo_id, json_path FROM celigo_script_attachments WHERE flow_id = :f").bindparams(
                    f=flow_id
                )
            )
        ).first()
        assert row.script_celigo_id == "scr_transform"
        assert row.json_path == refs[0].json_path


class TestLogicalScriptDedup:
    """Headline test #2: (source_id, content_hash) dedup collapses ~20 clones
    of one script to a single logical script with N attachments. Shape from
    observed-shapes.md's live-confirmed clone pairs (synthetic ids/content)."""

    async def test_clones_collapse_to_one_logical_script_with_summed_attachments(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="dedup")

        original_id = "scr_original"
        content = "function run() { return 1; }"

        await upsert_script(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            sanitized=sanitize("script", {"_id": original_id, "name": "Compal 856", "content": content}),
        )

        clone_count = 20
        for i in range(clone_count):
            clone_id = f"scr_clone_{i}"
            await upsert_script(
                db,
                tenant_id=tenant.id,
                connection_id=conn_id,
                sanitized=sanitize(
                    "script",
                    {"_id": clone_id, "name": "Compal 856", "content": content, "_sourceId": original_id},
                ),
            )
            await upsert_script_attachment(
                db,
                tenant_id=tenant.id,
                connection_id=conn_id,
                flow_id=flow_id,
                flow_step_id=None,
                script_id=None,
                script_celigo_id=clone_id,
                function_name=None,
                json_path=f"pageProcessors[{i}].transform.script",
                reference_object_celigo_id=None,
                site_type="transform",
            )
        # The original also has one attachment site of its own.
        await upsert_script_attachment(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            flow_id=flow_id,
            flow_step_id=None,
            script_id=None,
            script_celigo_id=original_id,
            function_name=None,
            json_path="pageProcessors[99].transform.script",
            reference_object_celigo_id=None,
            site_type="transform",
        )
        await db.flush()

        logical_scripts = await list_logical_scripts(db, tenant_id=tenant.id, connection_id=conn_id)

        assert len(logical_scripts) == 1
        logical = logical_scripts[0]
        assert len(logical.script_ids) == clone_count + 1
        assert logical.attachment_count == clone_count + 1
        assert logical.content_diverged is False
        assert logical.dedup_key == original_id

    async def test_unrelated_scripts_stay_in_separate_groups(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)

        await upsert_script(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            sanitized=sanitize("script", {"_id": "scr_a", "name": "Script A", "content": "a"}),
        )
        await upsert_script(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            sanitized=sanitize("script", {"_id": "scr_b", "name": "Script B", "content": "b"}),
        )
        await db.flush()

        logical_scripts = await list_logical_scripts(db, tenant_id=tenant.id, connection_id=conn_id)
        assert {ls.dedup_key for ls in logical_scripts} == {"scr_a", "scr_b"}
        assert all(len(ls.script_ids) == 1 for ls in logical_scripts)


class TestFlowErrorsAreNeverDeleted:
    async def test_mark_resolved_does_not_delete_the_row(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        error_id = await upsert_flow_error(db, tenant_id=tenant.id, connection_id=conn_id, celigo_id="err_resolve")
        await db.flush()

        resolved_count = await mark_flow_errors_resolved(
            db, tenant_id=tenant.id, connection_id=conn_id, celigo_ids=["err_resolve"]
        )
        await db.flush()

        assert resolved_count == 1
        row = (await db.execute(select(CeligoFlowError).where(CeligoFlowError.id == error_id))).scalar_one()
        assert row.resolved_at is not None
        assert row.purged_at is None

    async def test_mark_purged_does_not_delete_the_row(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        error_id = await upsert_flow_error(db, tenant_id=tenant.id, connection_id=conn_id, celigo_id="err_purge")
        await db.flush()

        purged_count = await mark_flow_errors_purged(
            db, tenant_id=tenant.id, connection_id=conn_id, celigo_ids=["err_purge"]
        )
        await db.flush()

        assert purged_count == 1
        row = (await db.execute(select(CeligoFlowError).where(CeligoFlowError.id == error_id))).scalar_one()
        assert row.purged_at is not None

    async def test_connection_delete_preserves_the_error_row(self, db: AsyncSession):
        """Same guarantee migration 094's own RLS test proves at the raw-SQL
        level (`test_celigo_connection_delete_preserves_flow_errors`), proven
        here through the repository's own write path instead."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        error_id = await upsert_flow_error(db, tenant_id=tenant.id, connection_id=conn_id, celigo_id="err_survives")
        await db.flush()

        await db.execute(text("DELETE FROM connections WHERE id = :id").bindparams(id=conn_id))
        await db.flush()

        row = (await db.execute(select(CeligoFlowError).where(CeligoFlowError.id == error_id))).scalar_one()
        assert row.celigo_connection_id is None
        assert row.resolved_at is None
        assert row.purged_at is None

    def test_repository_module_exposes_no_delete_function_for_flow_errors(self):
        """Cheap, durable guard against the failure class this whole task
        warns about: a future edit adding a `delete_flow_error`-shaped
        function would be a defect per the task's non-negotiable #1."""
        import app.services.celigo.repository as repo_module

        delete_like = [name for name in dir(repo_module) if "delete" in name.lower() and "flow_error" in name.lower()]
        assert delete_like == []


class TestNullConnectionIdCannotSilentlyDuplicateAuditRows:
    """`celigo_connection_id` is nullable/SET NULL on `celigo_error_signatures`
    and `celigo_flow_errors` (the audit tables) -- Postgres treats NULL as
    DISTINCT for UNIQUE-constraint purposes, so `ON CONFLICT ON CONSTRAINT
    ...` never fires when `celigo_connection_id` is NULL. An upsert called
    with `connection_id=None` would silently INSERT a new row every time
    instead of updating. Both upsert functions require a real `connection_id`
    (module docstring point 4) to remove that call shape entirely, with a
    runtime `ValueError` guard since Python type hints aren't enforced."""

    async def test_upsert_error_signature_rejects_none_connection_id(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        with pytest.raises(ValueError, match="connection_id"):
            await upsert_error_signature(db, tenant_id=tenant.id, connection_id=None, fingerprint="fp_none")

    async def test_upsert_flow_error_rejects_none_connection_id(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        with pytest.raises(ValueError, match="connection_id"):
            await upsert_flow_error(db, tenant_id=tenant.id, connection_id=None, celigo_id="err_none")

    async def test_orphaned_error_and_resync_under_a_new_connection_do_not_collide(self, db: AsyncSession):
        """The real regression this guard exists to prevent: a row orphaned
        by a connection deletion (celigo_connection_id -> NULL, via the DB's
        own SET NULL, never via this module) must not corrupt or block a
        LATER resync of the same celigo_id under a brand-new connection.
        Because upsert always requires a real connection_id, the new sync
        creates its own distinct row rather than silently duplicating
        forever -- and the orphaned original is left untouched, exactly as
        the audit trail requires."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        old_conn_id = await _make_connection(db, tenant.id)
        original_id = await upsert_flow_error(
            db, tenant_id=tenant.id, connection_id=old_conn_id, celigo_id="err_reused", code="E1"
        )
        await db.flush()

        await db.execute(text("DELETE FROM connections WHERE id = :id").bindparams(id=old_conn_id))
        await db.flush()

        new_conn_id = await _make_connection(db, tenant.id)
        new_id = await upsert_flow_error(
            db, tenant_id=tenant.id, connection_id=new_conn_id, celigo_id="err_reused", code="E1"
        )
        await db.flush()

        assert new_id != original_id  # a fresh row, not an update of the orphaned one

        orphaned_row = (await db.execute(select(CeligoFlowError).where(CeligoFlowError.id == original_id))).scalar_one()
        assert orphaned_row.celigo_connection_id is None  # untouched by the later resync

        new_row = (await db.execute(select(CeligoFlowError).where(CeligoFlowError.id == new_id))).scalar_one()
        assert new_row.celigo_connection_id == new_conn_id

        count = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM celigo_flow_errors WHERE tenant_id = :t AND celigo_id = 'err_reused'"
                ).bindparams(t=tenant.id)
            )
        ).scalar_one()
        assert count == 2  # both rows coexist -- no silent overwrite, no unbounded duplication either


class TestFlowStepInsertNeverSwallowsUnrelatedIntegrityErrors:
    async def test_missing_flow_fk_raises_not_silently_ignored(self, db: AsyncSession):
        """Not the role-collision case -- a plain FK violation (bad flow_id)
        must also propagate, proving the repository doesn't wrap step writes
        in a blanket try/except."""
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        bogus_flow_id = uuid.uuid4()

        flow = sanitize("flow", {"_id": "f", "pageGenerators": [{"_exportId": "exp_1"}]})
        steps = extract_flow_steps(flow)

        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await sync_flow_steps(db, tenant_id=tenant.id, connection_id=conn_id, flow_id=bogus_flow_id, steps=steps)
