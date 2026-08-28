# backend/tests/test_celigo_provenance.py
"""Task 11: `app/services/celigo/provenance.py` -- derive which flows write
which NetSuite record types, from already-synced `celigo_flow_steps`
provenance columns (`record_type`/`operation`/`search_id`, Task 7 fix round
2, migration 096).

SCOPE: WRITES ONLY, config-derived only. `operation` is populated
exclusively from `netsuite_da.operation` (imports) -- see
`app/services/celigo/sync_service.py`'s `_extract_provenance` -- so it is
the write signal this module keys on. Exports carry `record_type`/
`search_id` (the READ side: a saved search Celigo runs against NetSuite to
pull data out) and must NEVER show up as a "write" -- `TestExportStepIsRead`
below pins that distinction as a live guard, not just a comment.

THE NO-PROVENANCE CASE IS THE COMMON CASE, per observed-shapes.md (probed
live 2026-08-27): this account is heavy with AS2Export/FTPExport/
NetSuiteExport adaptors that carry NULL in all three provenance columns.
`TestNonNetSuiteStepYieldsNothing` pins that as normal, not a guess.

No test here asserts anything about the CURRENT error distribution of any
one flow (e.g. "deposit flows are clean") -- that class of claim was
falsified live during this session and does not belong in a test that must
stay true regardless of which flows happen to be erroring today. Every
assertion below is about the derivation's PROPERTIES: which rows count as a
write, which don't, and that tenants never see each other's flows.

Fixtures use SYNTHETIC values only (no real Celigo payloads) -- shapes are
taken from `.superpowers/sdd/2026-08-25-celigo-plan-b-flow-map/
observed-shapes.md`, values are made up. Mirrors `test_celigo_repository.py`'s
`_make_connection`/`_seed_integration_and_flow` helpers (this codebase's
established idiom is to duplicate these small per-file fixtures rather than
share them across test modules -- see that file's own helper docstring).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.celigo.provenance import (
    FlowRecordWrite,
    derive_flow_record_writes,
    group_by_flow,
    group_by_record_type,
)
from app.services.celigo.repository import (
    FlowStepInput,
    backfill_flow_step_reference_info,
    upsert_flow,
    upsert_flow_step,
    upsert_integration,
)
from app.services.celigo.sanitizer import sanitize
from tests.conftest import create_test_tenant


async def _make_connection(db: AsyncSession, tenant_id) -> uuid.UUID:
    """Raw SQL, not the `Connection` ORM model -- `celigo_write_guard.py`
    refuses any ORM flush of a provider='celigo' row outside the paired
    connect/disconnect endpoints; raw SQL below the ORM is its documented,
    accepted escape hatch for tests. Mirrors `test_celigo_repository.py`'s
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
    functions (not raw SQL)."""
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
            {"_id": f"flow_{flow_suffix}", "name": f"Test Flow {flow_suffix}", "_integrationId": f"int_{flow_suffix}"},
        ),
    )
    await db.flush()
    return integration_id, flow_id


async def _add_step_with_provenance(
    db: AsyncSession,
    *,
    tenant_id,
    connection_id,
    flow_id,
    celigo_id: str,
    record_type: str | None,
    operation: str | None = None,
    search_id: str | None = None,
    sequence: int = 0,
):
    """Upsert one processor step, then backfill provenance onto it exactly
    the way `sync_service.py`'s Phase D does (via a LATER export/import
    fetch, never at initial step-insert time) -- so this fixture exercises
    the real two-phase write path, not a shortcut that writes provenance
    columns directly."""
    step_id = await upsert_flow_step(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        flow_id=flow_id,
        step=FlowStepInput(
            celigo_id=celigo_id,
            role="processor",
            router_id=None,
            branch_id=None,
            sequence=sequence,
            filter_json=None,
            mapping_json=None,
            proceed_on_failure=None,
            skip_retries=None,
        ),
    )
    await backfill_flow_step_reference_info(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        celigo_id=celigo_id,
        adaptor_type=None,
        connection_celigo_id=None,
        record_type=record_type,
        operation=operation,
        search_id=search_id,
    )
    await db.flush()
    return step_id


class TestImportStepYieldsFlowRecordType:
    async def test_single_import_step_yields_one_write(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="ra")

        step_id = await _add_step_with_provenance(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            flow_id=flow_id,
            celigo_id="imp_ra_1",
            record_type="returnauthorization",
            operation="update",
        )

        writes = await derive_flow_record_writes(db, tenant_id=tenant.id, connection_id=conn_id)

        assert writes == [
            FlowRecordWrite(
                flow_id=flow_id,
                flow_celigo_id="flow_ra",
                flow_name="Test Flow ra",
                flow_step_id=step_id,
                record_type="returnauthorization",
                operation="update",
            )
        ]


class TestNonNetSuiteStepYieldsNothing:
    """observed-shapes.md: this is the COMMON case (AS2/FTP/NetSuiteExport
    adaptors), not an edge case -- all three provenance columns stay NULL
    and this module must say NOTHING about the flow, never guess."""

    async def test_step_with_no_provenance_columns_yields_no_write(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="edi")

        # No backfill call at all -- record_type/operation/search_id stay
        # NULL, exactly as an AS2Export/FTPExport step would after a real
        # sync (sync_service.py's Phase D only ever writes non-None values).
        await upsert_flow_step(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            flow_id=flow_id,
            step=FlowStepInput(
                celigo_id="exp_as2_1",
                role="generator",
                router_id=None,
                branch_id=None,
                sequence=0,
                filter_json=None,
                mapping_json=None,
                proceed_on_failure=None,
                skip_retries=None,
            ),
        )
        await db.flush()

        writes = await derive_flow_record_writes(db, tenant_id=tenant.id, connection_id=conn_id)

        assert writes == []


class TestExportStepIsRead:
    """Exports carry `record_type`/`search_id` but never `operation`
    (`netsuite.restlet` has no operation field at all -- observed-shapes.md).
    That is the READ side and must be excluded from a "writes" derivation,
    never blurred into the same list."""

    async def test_export_backed_step_is_not_counted_as_a_write(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="so-export")

        await _add_step_with_provenance(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            flow_id=flow_id,
            celigo_id="exp_so_1",
            record_type="salesorder",
            operation=None,
            search_id="customsearch_so_export",
        )

        writes = await derive_flow_record_writes(db, tenant_id=tenant.id, connection_id=conn_id)

        assert writes == []


class TestFlowWithSeveralImportStepsYieldsSeveralRecordTypes:
    async def test_multiple_import_steps_yield_multiple_writes(self, db: AsyncSession):
        tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        _, flow_id = await _seed_integration_and_flow(db, tenant.id, conn_id, flow_suffix="multi")

        await _add_step_with_provenance(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            flow_id=flow_id,
            celigo_id="imp_multi_1",
            record_type="salesorder",
            operation="add",
            sequence=0,
        )
        await _add_step_with_provenance(
            db,
            tenant_id=tenant.id,
            connection_id=conn_id,
            flow_id=flow_id,
            celigo_id="imp_multi_2",
            record_type="customerdeposit",
            operation="add",
            sequence=1,
        )

        writes = await derive_flow_record_writes(db, tenant_id=tenant.id, connection_id=conn_id)

        assert {w.record_type for w in writes} == {"salesorder", "customerdeposit"}
        assert all(w.flow_id == flow_id for w in writes)

        by_flow = group_by_flow(writes)
        assert len(by_flow) == 1
        assert {w.record_type for w in by_flow[flow_id]} == {"salesorder", "customerdeposit"}


class TestTenantIsolation:
    async def test_derivation_never_crosses_tenants(self, db: AsyncSession):
        tenant_a = await create_test_tenant(db, name=f"Tenant A {uuid.uuid4().hex[:6]}")
        tenant_b = await create_test_tenant(db, name=f"Tenant B {uuid.uuid4().hex[:6]}")
        conn_a = await _make_connection(db, tenant_a.id)
        conn_b = await _make_connection(db, tenant_b.id)
        _, flow_a = await _seed_integration_and_flow(db, tenant_a.id, conn_a, flow_suffix="a")
        _, flow_b = await _seed_integration_and_flow(db, tenant_b.id, conn_b, flow_suffix="b")

        await _add_step_with_provenance(
            db,
            tenant_id=tenant_a.id,
            connection_id=conn_a,
            flow_id=flow_a,
            celigo_id="imp_a_1",
            record_type="customer",
            operation="update",
        )
        await _add_step_with_provenance(
            db,
            tenant_id=tenant_b.id,
            connection_id=conn_b,
            flow_id=flow_b,
            celigo_id="imp_b_1",
            record_type="vendor",
            operation="update",
        )

        writes_a = await derive_flow_record_writes(db, tenant_id=tenant_a.id, connection_id=conn_a)
        writes_b = await derive_flow_record_writes(db, tenant_id=tenant_b.id, connection_id=conn_b)

        assert [w.record_type for w in writes_a] == ["customer"]
        assert [w.record_type for w in writes_b] == ["vendor"]


class TestGroupingHelpersArePure:
    """`group_by_record_type`/`group_by_flow` are plain list->dict regroups,
    no DB -- covered directly, no fixtures needed."""

    def test_group_by_record_type(self):
        write_a = FlowRecordWrite(
            flow_id=uuid.uuid4(),
            flow_celigo_id="flow_1",
            flow_name="Flow 1",
            flow_step_id=uuid.uuid4(),
            record_type="salesorder",
            operation="add",
        )
        write_b = FlowRecordWrite(
            flow_id=uuid.uuid4(),
            flow_celigo_id="flow_2",
            flow_name="Flow 2",
            flow_step_id=uuid.uuid4(),
            record_type="salesorder",
            operation="update",
        )
        write_c = FlowRecordWrite(
            flow_id=uuid.uuid4(),
            flow_celigo_id="flow_3",
            flow_name="Flow 3",
            flow_step_id=uuid.uuid4(),
            record_type="customer",
            operation="add",
        )

        grouped = group_by_record_type([write_a, write_b, write_c])

        assert set(grouped.keys()) == {"salesorder", "customer"}
        assert grouped["salesorder"] == [write_a, write_b]
        assert grouped["customer"] == [write_c]

    def test_group_by_flow(self):
        flow_id = uuid.uuid4()
        write_a = FlowRecordWrite(
            flow_id=flow_id,
            flow_celigo_id="flow_1",
            flow_name="Flow 1",
            flow_step_id=uuid.uuid4(),
            record_type="salesorder",
            operation="add",
        )
        write_b = FlowRecordWrite(
            flow_id=flow_id,
            flow_celigo_id="flow_1",
            flow_name="Flow 1",
            flow_step_id=uuid.uuid4(),
            record_type="customerdeposit",
            operation="add",
        )

        grouped = group_by_flow([write_a, write_b])

        assert set(grouped.keys()) == {flow_id}
        assert grouped[flow_id] == [write_a, write_b]
