"""Task 1 -- `app/services/celigo/script_families.py`, the clone-family facts
service behind the Scripts view (spec `docs/superpowers/specs/2026-09-06-
celigo-scripts-view-design.md` §2.2-2.3).

Fixtures use SYNTHETIC script text/names only (`"function preMap(o){return
o.data}"`-style bodies, made-up integration/flow/script names) -- no real
Celigo payload, per this module's own N2 (human-only) boundary.

Seeding goes through the existing repository upsert functions (same pattern
as `test_celigo_repository.py`/`test_celigo_topology.py`) rather than raw
ORM inserts, because `dedup_key`/`branch_key` are DB-computed generated
columns -- the repository's own upsert path is the one place that already
gets that right.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.celigo.repository import (
    FlowStepInput,
    mark_flow_errors_checked,
    mark_flow_errors_purged,
    mark_flow_errors_resolved,
    upsert_flow,
    upsert_flow_error,
    upsert_flow_step,
    upsert_integration,
    upsert_script,
    upsert_script_attachment,
)
from app.services.celigo.sanitizer import sanitize
from tests.conftest import create_test_tenant

# ---------------------------------------------------------------------------
# Seeding helpers (mirrors test_celigo_repository.py's `_make_connection`)
# ---------------------------------------------------------------------------


async def _make_connection(db: AsyncSession, tenant_id) -> uuid.UUID:
    conn_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO connections (id, tenant_id, provider, label, status, encrypted_credentials, encryption_key_version) "
            "VALUES (:id, :tenant_id, 'celigo', 'Celigo', 'active', 'unit-test-not-a-real-token', 1)"
        ).bindparams(id=conn_id, tenant_id=tenant_id)
    )
    await db.flush()
    return conn_id


async def _seed_integration(db, tenant_id, connection_id, *, celigo_id, name="Test Integration", sandbox=None):
    return await upsert_integration(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        sanitized=sanitize("integration", {"_id": celigo_id, "name": name, "sandbox": sandbox}),
    )


async def _seed_flow(db, tenant_id, connection_id, integration_id, *, celigo_id, name="Test Flow", disabled=None):
    return await upsert_flow(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        integration_id=integration_id,
        sanitized=sanitize("flow", {"_id": celigo_id, "name": name, "_integrationId": "unused", "disabled": disabled}),
    )


async def _seed_script(
    db,
    tenant_id,
    connection_id,
    *,
    celigo_id,
    name,
    content=None,
    source_id=None,
    sandbox=None,
    last_modified: str | None = None,
):
    payload = {"_id": celigo_id, "name": name, "content": content, "sandbox": sandbox}
    if source_id is not None:
        payload["_sourceId"] = source_id
    if last_modified is not None:
        payload["lastModified"] = last_modified
    return await upsert_script(
        db, tenant_id=tenant_id, connection_id=connection_id, sanitized=sanitize("script", payload)
    )


async def _seed_flow_step(db, tenant_id, connection_id, flow_id, *, celigo_id, role="processor", adaptor_type=None):
    step = FlowStepInput(
        celigo_id=celigo_id,
        role=role,
        router_id=None,
        branch_id=None,
        sequence=0,
        filter_json=None,
        mapping_json=None,
        proceed_on_failure=None,
        skip_retries=None,
    )
    return await upsert_flow_step(
        db, tenant_id=tenant_id, connection_id=connection_id, flow_id=flow_id, step=step, adaptor_type=adaptor_type
    )


async def _seed_attachment(
    db,
    tenant_id,
    connection_id,
    flow_id,
    *,
    script_celigo_id,
    script_id=None,
    flow_step_id=None,
    function_name=None,
    json_path="transform.script",
    site_type="transform",
):
    return await upsert_script_attachment(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        flow_id=flow_id,
        flow_step_id=flow_step_id,
        script_id=script_id,
        script_celigo_id=script_celigo_id,
        function_name=function_name,
        json_path=json_path,
        reference_object_celigo_id=None,
        site_type=site_type,
    )


async def _seed_error(db, tenant_id, connection_id, *, celigo_id, flow_id, flow_step_id):
    return await upsert_flow_error(
        db,
        tenant_id=tenant_id,
        connection_id=connection_id,
        celigo_id=celigo_id,
        flow_id=flow_id,
        flow_step_id=flow_step_id,
    )


def _ts(day: int) -> str:
    return datetime(2026, 1, day, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


async def _basic_tenant_conn(db):
    tenant = await create_test_tenant(db, name=f"Tenant {uuid.uuid4().hex[:6]}")
    conn_id = await _make_connection(db, tenant.id)
    return tenant.id, conn_id


# ---------------------------------------------------------------------------
# Import guard -- fails loudly (ImportError) until the module exists.
# ---------------------------------------------------------------------------


def _import_module():
    from app.services.celigo import script_families

    return script_families


class TestModuleImports:
    def test_module_and_names_exist(self):
        sf = _import_module()
        for name in (
            "ScriptFamilySite",
            "ScriptFamilyMember",
            "ScriptFamilyVersion",
            "ScriptFamilySummary",
            "ScriptFamilyTotals",
            "ScriptFamiliesList",
            "ScriptFamilyDetail",
            "list_script_families",
            "get_script_family",
        ):
            assert hasattr(sf, name), f"script_families.{name} missing"


class TestProductionOnly:
    async def test_sandbox_scripts_excluded_from_every_family(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(
            db, tenant_id, conn_id, celigo_id="s_sandbox", name="sandbox_script", content="x", sandbox=True
        )
        await _seed_script(db, tenant_id, conn_id, celigo_id="s_prod", name="prod_script", content="y", sandbox=False)
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert [f.dedup_key for f in result.families] == ["s_prod"]
        assert result.totals.scripts == 1

    async def test_family_that_is_entirely_sandbox_is_unknown_to_get_script_family(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(
            db, tenant_id, conn_id, celigo_id="s_sandbox_only", name="sandbox_only", content="x", sandbox=True
        )
        await db.flush()

        detail = await sf.get_script_family(db, tenant_id=tenant_id, connection_id=conn_id, dedup_key="s_sandbox_only")

        assert detail is None


class TestUnknownFamily:
    async def test_unknown_dedup_key_returns_none(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="only_one", content="x")
        await db.flush()

        assert (
            await sf.get_script_family(db, tenant_id=tenant_id, connection_id=conn_id, dedup_key="does_not_exist")
            is None
        )


class TestFamilyKeyAndVersionLetters:
    async def test_clones_share_dedup_key_of_the_original(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(
            db, tenant_id, conn_id, celigo_id="orig", name="ns_sales_order_premap", content="a", last_modified=_ts(1)
        )
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="clone1",
            name="ns_sales_order_premap copy",
            content="a",
            source_id="orig",
            last_modified=_ts(2),
        )
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="clone2",
            name="ns_sales_order_premap copy 2",
            content="a",
            source_id="orig",
            last_modified=_ts(3),
        )
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert len(result.families) == 1
        family = result.families[0]
        assert family.dedup_key == "orig"
        assert family.copies_count == 3

    async def test_version_letters_ordered_by_first_appearance_ties_broken_by_hash(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        # h1 first seen day 1, h2 first seen day 2 (two members), h3 first seen day 4.
        await _seed_script(
            db, tenant_id, conn_id, celigo_id="orig", name="fam", content="aaaaaaaaaa", last_modified=_ts(1)
        )
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="c1",
            name="fam copy",
            content="bbbbbbbbbbbbbbbbbbbb",
            source_id="orig",
            last_modified=_ts(2),
        )
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="c2",
            name="fam copy 2",
            content="bbbbbbbbbbbbbbbbbbbb",
            source_id="orig",
            last_modified=_ts(3),
        )
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="c3",
            name="fam copy 3",
            content="cccccccccccccccccccccccccccc",
            source_id="orig",
            last_modified=_ts(4),
        )
        await db.flush()

        detail = await sf.get_script_family(db, tenant_id=tenant_id, connection_id=conn_id, dedup_key="orig")

        by_celigo_id = {m.celigo_id: m for m in detail.members}
        assert by_celigo_id["orig"].version_letter == "A"
        assert by_celigo_id["c1"].version_letter == "B"
        assert by_celigo_id["c2"].version_letter == "B"
        assert by_celigo_id["c3"].version_letter == "C"
        assert [v.letter for v in detail.versions] == ["A", "B", "C"]
        assert detail.summary.versions_count == 3
        assert detail.summary.content_diverged is True

    async def test_member_with_null_content_hash_gets_no_letter_and_no_version(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        # content=None -> content_hash None (repository never invents a hash for an absent body).
        await _seed_script(db, tenant_id, conn_id, celigo_id="orig", name="fam", content=None, last_modified=_ts(1))
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="c1",
            name="fam copy",
            content="real body",
            source_id="orig",
            last_modified=_ts(2),
        )
        await db.flush()

        detail = await sf.get_script_family(db, tenant_id=tenant_id, connection_id=conn_id, dedup_key="orig")

        by_celigo_id = {m.celigo_id: m for m in detail.members}
        assert by_celigo_id["orig"].version_letter is None
        assert by_celigo_id["orig"].content_hash is None
        assert by_celigo_id["c1"].version_letter == "A"
        assert len(detail.versions) == 1
        assert detail.summary.versions_count == 1

    async def test_single_copy_family_still_gets_a_version_when_it_has_content(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(
            db, tenant_id, conn_id, celigo_id="solo", name="solo_script", content="body", last_modified=_ts(1)
        )
        await db.flush()

        detail = await sf.get_script_family(db, tenant_id=tenant_id, connection_id=conn_id, dedup_key="solo")

        assert detail.summary.copies_count == 1
        assert detail.summary.versions_count == 1
        assert detail.members[0].version_letter == "A"
        assert detail.versions[0].holds_original is True


class TestVersionLettersComputedOnce:
    async def test_get_script_family_computes_letters_exactly_once(self, db: AsyncSession, monkeypatch):
        """Review finding (brief item 4): `get_script_family` computed
        `letters` once at the top AND `_summarize_family` recomputed them
        again internally from the SAME `members` list -- wasted work with no
        difference in output. Counts calls to `assign_version_letters` (as
        seen through `script_families`'s own module-level name, the one
        every internal call site actually resolves through) to pin "exactly
        once per request", not just "same answer either way"."""
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="fam", content="a")
        await db.flush()

        calls: list[object] = []
        original = sf.assign_version_letters

        def _counting(members):
            calls.append(members)
            return original(members)

        monkeypatch.setattr(sf, "assign_version_letters", _counting)

        await sf.get_script_family(db, tenant_id=tenant_id, connection_id=conn_id, dedup_key="s1")

        assert len(calls) == 1


class TestNameRule:
    async def test_name_is_the_originals_when_original_is_in_production(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(
            db, tenant_id, conn_id, celigo_id="orig", name="Original Name", content="a", last_modified=_ts(2)
        )
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="c1",
            name="Clone Name",
            content="a",
            source_id="orig",
            last_modified=_ts(1),
        )
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.families[0].name == "Original Name"
        assert result.families[0].original_present is True

    async def test_name_falls_back_to_earliest_modified_member_when_original_is_sandbox(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        # The original is a sandbox row -- excluded from production, so it never joins the family.
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="orig",
            name="Sandbox Original",
            content="a",
            sandbox=True,
            last_modified=_ts(1),
        )
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="c1",
            name="Earliest Clone",
            content="a",
            source_id="orig",
            last_modified=_ts(2),
        )
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="c2",
            name="Later Clone",
            content="a",
            source_id="orig",
            last_modified=_ts(3),
        )
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.families[0].name == "Earliest Clone"
        assert result.families[0].original_present is False


class TestKind:
    async def _seed_site(
        self,
        db,
        tenant_id,
        conn_id,
        integration_id,
        flow_id,
        script_id,
        script_celigo_id,
        *,
        site_type,
        flow_step_id=None,
    ):
        await _seed_attachment(
            db,
            tenant_id,
            conn_id,
            flow_id,
            script_celigo_id=script_celigo_id,
            script_id=script_id,
            flow_step_id=flow_step_id,
            site_type=site_type,
        )

    async def test_single_site_type_across_all_sites_is_that_kind(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        integration_id = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1")
        flow_id = await _seed_flow(db, tenant_id, conn_id, integration_id, celigo_id="flow_1")
        script_id = await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="only_kind", content="a")
        await self._seed_site(db, tenant_id, conn_id, integration_id, flow_id, script_id, "s1", site_type="hook")
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.families[0].kind == "hook"

    async def test_several_distinct_site_types_is_mixed(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        integration_id = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1")
        flow_id = await _seed_flow(db, tenant_id, conn_id, integration_id, celigo_id="flow_1")
        script_id = await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="mixed_kind", content="a")
        await self._seed_site(db, tenant_id, conn_id, integration_id, flow_id, script_id, "s1", site_type="hook")
        await upsert_script_attachment(
            db,
            tenant_id=tenant_id,
            connection_id=conn_id,
            flow_id=flow_id,
            flow_step_id=None,
            script_id=script_id,
            script_celigo_id="s1",
            function_name=None,
            json_path="routers[0].script",
            reference_object_celigo_id=None,
            site_type="router",
        )
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.families[0].kind == "mixed"

    async def test_no_sites_is_unattached(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="lonely", content="a")
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.families[0].kind == "unattached"
        assert result.families[0].function_name is None
        assert result.totals.unattached_families == 1
        assert result.totals.attached_families == 0

    async def test_unrecognized_site_type_stays_inside_the_closed_kind_enum(self, db: AsyncSession):
        """Review finding (Task 1 round 1): `graph.py::_classify_site_type` is
        a documented, live-observed source of the literal string `"unknown"`
        (any `_scriptId` found outside a hooks/filter/transform/routers path
        segment), and `site_type IS NULL` maps to the same string via the
        `or "unknown"` fallback. `kind` is a closed enum (spec §2.2:
        hook|transform|filter|router|mixed|unattached) -- neither "unknown"
        nor any other unrecognized value may leak through as-is."""
        sf = _import_module()
        valid_kinds = {"hook", "transform", "filter", "router", "mixed", "unattached"}
        tenant_id, conn_id = await _basic_tenant_conn(db)
        integration_id = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1")
        flow_id = await _seed_flow(db, tenant_id, conn_id, integration_id, celigo_id="flow_1")
        script_id = await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="unknown_kind", content="a")
        await self._seed_site(db, tenant_id, conn_id, integration_id, flow_id, script_id, "s1", site_type="unknown")
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.families[0].kind in valid_kinds
        assert result.families[0].kind == "mixed"

    async def test_null_site_type_stays_inside_the_closed_kind_enum(self, db: AsyncSession):
        """Same rule, NULL `site_type` column value -- also folds to
        `"unknown"` via `_kind_from_site_types`'s own `or "unknown"`, so it
        must land in the same enum-safe bucket, not leak through."""
        sf = _import_module()
        valid_kinds = {"hook", "transform", "filter", "router", "mixed", "unattached"}
        tenant_id, conn_id = await _basic_tenant_conn(db)
        integration_id = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1")
        flow_id = await _seed_flow(db, tenant_id, conn_id, integration_id, celigo_id="flow_1")
        script_id = await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="null_kind", content="a")
        await self._seed_site(db, tenant_id, conn_id, integration_id, flow_id, script_id, "s1", site_type=None)
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.families[0].kind in valid_kinds
        assert result.families[0].kind == "mixed"


class TestFunctionNameMode:
    async def test_mode_ties_broken_alphabetically(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        integration_id = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1")
        flow_id = await _seed_flow(db, tenant_id, conn_id, integration_id, celigo_id="flow_1")
        script_id = await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="fnmode", content="a")
        await _seed_attachment(
            db,
            tenant_id,
            conn_id,
            flow_id,
            script_celigo_id="s1",
            script_id=script_id,
            function_name="run",
            json_path="p1",
        )
        await _seed_attachment(
            db,
            tenant_id,
            conn_id,
            flow_id,
            script_celigo_id="s1",
            script_id=script_id,
            function_name="run",
            json_path="p2",
        )
        await _seed_attachment(
            db,
            tenant_id,
            conn_id,
            flow_id,
            script_celigo_id="s1",
            script_id=script_id,
            function_name="validate",
            json_path="p3",
        )
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.families[0].function_name == "run"

    async def test_true_tie_breaks_alphabetically(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        integration_id = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1")
        flow_id = await _seed_flow(db, tenant_id, conn_id, integration_id, celigo_id="flow_1")
        script_id = await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="fnmode_tie", content="a")
        await _seed_attachment(
            db,
            tenant_id,
            conn_id,
            flow_id,
            script_celigo_id="s1",
            script_id=script_id,
            function_name="zzz",
            json_path="p1",
        )
        await _seed_attachment(
            db,
            tenant_id,
            conn_id,
            flow_id,
            script_celigo_id="s1",
            script_id=script_id,
            function_name="aaa",
            json_path="p2",
        )
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.families[0].function_name == "aaa"


class TestSitesFlowsIntegrationsCounts:
    async def test_counts_across_two_flows_and_two_integrations(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        int1 = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1", name="Integration One")
        int2 = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_2", name="Integration Two")
        flow1 = await _seed_flow(db, tenant_id, conn_id, int1, celigo_id="flow_1", name="Flow One")
        flow2 = await _seed_flow(db, tenant_id, conn_id, int2, celigo_id="flow_2", name="Flow Two")
        script_id = await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="shared_script", content="a")
        await _seed_attachment(
            db, tenant_id, conn_id, flow1, script_celigo_id="s1", script_id=script_id, json_path="p1"
        )
        await _seed_attachment(
            db, tenant_id, conn_id, flow1, script_celigo_id="s1", script_id=script_id, json_path="p2"
        )
        await _seed_attachment(
            db, tenant_id, conn_id, flow2, script_celigo_id="s1", script_id=script_id, json_path="p3"
        )
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        family = result.families[0]
        assert family.sites_count == 3
        assert family.flows_count == 2
        assert family.integrations_count == 2
        assert sorted(family.flow_names) == ["Flow One", "Flow Two"]
        assert set(family.integration_ids) == {int1, int2}


class TestOpenErrorRollup:
    async def test_open_error_count_excludes_resolved_and_purged(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        integration_id = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1")
        flow_id = await _seed_flow(db, tenant_id, conn_id, integration_id, celigo_id="flow_1")
        script_id = await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="errscript", content="a")
        step_id = await _seed_flow_step(db, tenant_id, conn_id, flow_id, celigo_id="exp_1")
        await _seed_attachment(
            db, tenant_id, conn_id, flow_id, script_celigo_id="s1", script_id=script_id, flow_step_id=step_id
        )
        await _seed_error(db, tenant_id, conn_id, celigo_id="err_open", flow_id=flow_id, flow_step_id=step_id)
        await _seed_error(db, tenant_id, conn_id, celigo_id="err_resolved", flow_id=flow_id, flow_step_id=step_id)
        await mark_flow_errors_resolved(db, tenant_id=tenant_id, connection_id=conn_id, celigo_ids=["err_resolved"])
        # Purged-but-UNRESOLVED (Celigo's ~30-day window caught it before this
        # app ever saw it resolve) -- the other half of spec §2.2's predicate,
        # `resolved_at IS NULL AND purged_at IS NULL`. Only `purged_at` is set.
        await _seed_error(db, tenant_id, conn_id, celigo_id="err_purged", flow_id=flow_id, flow_step_id=step_id)
        await mark_flow_errors_purged(db, tenant_id=tenant_id, connection_id=conn_id, celigo_ids=["err_purged"])
        await db.flush()

        detail = await sf.get_script_family(db, tenant_id=tenant_id, connection_id=conn_id, dedup_key="s1")

        assert len(detail.sites) == 1
        assert detail.sites[0].open_error_count == 1
        assert detail.summary.sites_with_open_errors == 1

    async def test_router_level_site_has_no_step_and_no_open_error_count(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        integration_id = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1")
        flow_id = await _seed_flow(db, tenant_id, conn_id, integration_id, celigo_id="flow_1")
        script_id = await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="router_script", content="a")
        await _seed_attachment(
            db,
            tenant_id,
            conn_id,
            flow_id,
            script_celigo_id="s1",
            script_id=script_id,
            flow_step_id=None,
            json_path="routers[0].script",
            site_type="router",
        )
        await db.flush()

        detail = await sf.get_script_family(db, tenant_id=tenant_id, connection_id=conn_id, dedup_key="s1")

        assert detail.sites[0].flow_step_id is None
        assert detail.sites[0].open_error_count is None


class TestSitesUnchecked:
    async def test_sites_unchecked_reflects_flow_errors_checked_at(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        integration_id = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1")
        checked_flow = await _seed_flow(
            db, tenant_id, conn_id, integration_id, celigo_id="flow_checked", name="Checked Flow"
        )
        unchecked_flow = await _seed_flow(
            db, tenant_id, conn_id, integration_id, celigo_id="flow_unchecked", name="Unchecked Flow"
        )
        await mark_flow_errors_checked(
            db, tenant_id=tenant_id, flow_id=checked_flow, checked_at=datetime.now(timezone.utc)
        )
        script_id = await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="checkscript", content="a")
        await _seed_attachment(
            db, tenant_id, conn_id, checked_flow, script_celigo_id="s1", script_id=script_id, json_path="p1"
        )
        await _seed_attachment(
            db, tenant_id, conn_id, unchecked_flow, script_celigo_id="s1", script_id=script_id, json_path="p2"
        )
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.families[0].sites_unchecked == 1


class TestOtherFamiliesWithName:
    async def test_counts_other_families_sharing_the_same_name(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        # Three single-copy families all named "inventory_sync_filter".
        await _seed_script(db, tenant_id, conn_id, celigo_id="a1", name="inventory_sync_filter", content="a")
        await _seed_script(db, tenant_id, conn_id, celigo_id="a2", name="inventory_sync_filter", content="b")
        await _seed_script(db, tenant_id, conn_id, celigo_id="a3", name="inventory_sync_filter", content="c")
        await _seed_script(db, tenant_id, conn_id, celigo_id="a4", name="unique_name", content="d")
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)
        by_key = {f.dedup_key: f for f in result.families}
        assert by_key["a1"].other_families_with_name == 2
        assert by_key["a4"].other_families_with_name == 0

        detail = await sf.get_script_family(db, tenant_id=tenant_id, connection_id=conn_id, dedup_key="a1")
        assert detail.summary.other_families_with_name == 2


class TestTotalsReconcileWithTheList:
    async def test_totals_reconcile(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        int1 = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1")
        flow1 = await _seed_flow(db, tenant_id, conn_id, int1, celigo_id="flow_1")
        await _seed_flow(db, tenant_id, conn_id, int1, celigo_id="flow_2")  # no sites -> flows_total > flows_with_sites
        step_id = await _seed_flow_step(db, tenant_id, conn_id, flow1, celigo_id="exp_1")

        s_attached = await _seed_script(
            db, tenant_id, conn_id, celigo_id="s_attached", name="attached", content="a", last_modified=_ts(1)
        )
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="s_clone",
            name="attached copy",
            content="b",
            source_id="s_attached",
            last_modified=_ts(2),
        )
        await _seed_script(db, tenant_id, conn_id, celigo_id="s_unattached", name="unattached", content="c")

        await _seed_attachment(
            db, tenant_id, conn_id, flow1, script_celigo_id="s_attached", script_id=s_attached, flow_step_id=step_id
        )
        await _seed_error(db, tenant_id, conn_id, celigo_id="e1", flow_id=flow1, flow_step_id=step_id)
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.totals.scripts == 3
        assert result.totals.families == 2  # s_attached+s_clone form one family; s_unattached is its own
        assert result.totals.sites == sum(f.sites_count for f in result.families)
        assert result.totals.diverged_families == sum(1 for f in result.families if f.content_diverged)
        assert result.totals.attached_families + result.totals.unattached_families == result.totals.families
        assert result.totals.sites_with_open_errors == sum(f.sites_with_open_errors for f in result.families)
        assert result.totals.flows_total >= result.totals.flows_with_sites
        assert result.totals.flows_total == 2
        assert result.totals.flows_with_sites == 1

    async def test_integrations_with_sites_counts_distinct_integrations_connection_wide(self, db: AsyncSession):
        """`totals.integrations_with_sites` must be the number of distinct integrations
        that have at least one site ACROSS THE WHOLE CONNECTION -- not summed/scoped
        per family. Two families attached under the same integration (via different
        flows) must not double-count that integration, and a third family under a
        different integration must add exactly one more."""
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        int_a = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_a", name="Integration A")
        int_b = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_b", name="Integration B")
        flow_a1 = await _seed_flow(db, tenant_id, conn_id, int_a, celigo_id="flow_a1", name="Flow A1")
        flow_a2 = await _seed_flow(db, tenant_id, conn_id, int_a, celigo_id="flow_a2", name="Flow A2")
        flow_b1 = await _seed_flow(db, tenant_id, conn_id, int_b, celigo_id="flow_b1", name="Flow B1")

        s1 = await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="family_one", content="a")
        s2 = await _seed_script(db, tenant_id, conn_id, celigo_id="s2", name="family_two", content="b")
        s3 = await _seed_script(db, tenant_id, conn_id, celigo_id="s3", name="family_three", content="c")
        # Family 1 and family 2 both sit under int_a (via two different flows) --
        # a per-family union would still count int_a twice; the connection-wide
        # count must collapse it to one.
        await _seed_attachment(db, tenant_id, conn_id, flow_a1, script_celigo_id="s1", script_id=s1, json_path="p1")
        await _seed_attachment(db, tenant_id, conn_id, flow_a2, script_celigo_id="s2", script_id=s2, json_path="p2")
        # Family 3 sits under the distinct int_b.
        await _seed_attachment(db, tenant_id, conn_id, flow_b1, script_celigo_id="s3", script_id=s3, json_path="p3")
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.totals.integrations_with_sites == 2
        distinct_integration_ids = set.union(*(set(f.integration_ids) for f in result.families))
        assert distinct_integration_ids == {int_a, int_b}
        assert result.totals.integrations_with_sites == len(distinct_integration_ids)


class TestSortOrder:
    async def test_families_sorted_by_sites_desc_copies_desc_name_asc_dedupkey_asc(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        integration_id = await _seed_integration(db, tenant_id, conn_id, celigo_id="int_1")
        flow_id = await _seed_flow(db, tenant_id, conn_id, integration_id, celigo_id="flow_1")

        # Family "z" has 2 sites.
        z = await _seed_script(db, tenant_id, conn_id, celigo_id="z", name="zzz_family", content="a")
        await _seed_attachment(db, tenant_id, conn_id, flow_id, script_celigo_id="z", script_id=z, json_path="p1")
        await _seed_attachment(db, tenant_id, conn_id, flow_id, script_celigo_id="z", script_id=z, json_path="p2")

        # Family "a" has 1 site.
        a = await _seed_script(db, tenant_id, conn_id, celigo_id="a", name="aaa_family", content="b")
        await _seed_attachment(db, tenant_id, conn_id, flow_id, script_celigo_id="a", script_id=a, json_path="p3")

        # Family "b" has 0 sites (unattached).
        await _seed_script(db, tenant_id, conn_id, celigo_id="b", name="bbb_family", content="c")
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert [f.dedup_key for f in result.families] == ["z", "a", "b"]


class TestListHasNoContentOrHash:
    async def test_summary_dataclass_never_carries_content_or_hash(self, db: AsyncSession):
        sf = _import_module()
        field_names = {f.name for f in dataclasses.fields(sf.ScriptFamilySummary)}
        assert "content" not in field_names
        assert "content_hash" not in field_names

        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(
            db, tenant_id, conn_id, celigo_id="s1", name="hidden_body", content="top secret synthetic body"
        )
        await db.flush()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)
        summary_dict = dataclasses.asdict(result.families[0])
        assert "content" not in summary_dict
        assert "content_hash" not in summary_dict


class TestListPathNeverLoadsContent:
    """Review finding (Task 2 round 1, brief item 1): `_fetch_production_scripts`
    used to SELECT full `CeligoScript` rows (content included) for every
    production script on BOTH endpoints, even though the list endpoint's own
    `ScriptFamilySummary` never carries `content`. `load_content=False` (the
    list endpoint's own path) must leave `.content` unloaded on the returned
    rows -- checking `inspect(script).unloaded` proves the SELECT itself
    never touched the column, a stronger claim than `TestListHasNoContentOrHash`
    (which only checks the OUTPUT shape, not what was actually SELECTed)."""

    async def test_list_path_leaves_content_unloaded(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="big", content="x" * 5000)
        await db.flush()
        db.expire_all()  # forget the full-row cache the seeding insert left behind

        rows = await sf._fetch_production_scripts(db, tenant_id=tenant_id, connection_id=conn_id, load_content=False)

        assert len(rows) == 1
        script, size_bytes = rows[0]
        assert "content" in inspect(script).unloaded
        assert size_bytes == 5000

    async def test_list_max_size_bytes_correct_without_loading_content(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="sized", content="y" * 42)
        await db.flush()
        db.expire_all()

        result = await sf.list_script_families(db, tenant_id=tenant_id, connection_id=conn_id)

        assert result.families[0].max_size_bytes == 42

    async def test_detail_path_content_fetch_is_scoped_to_the_targets_family(self, db: AsyncSession):
        """`get_script_family` must load `.content` ONLY for the requested
        family's own members -- `_fetch_scripts_by_celigo_id` is the helper
        that scopes that fetch, and must never pull in a sibling family's
        script just because it shares the connection."""
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="family_one", content="a" * 10)
        await _seed_script(db, tenant_id, conn_id, celigo_id="s2", name="family_two", content="b" * 20)
        await db.flush()

        rows = await sf._fetch_scripts_by_celigo_id(db, tenant_id=tenant_id, connection_id=conn_id, celigo_ids=["s1"])

        assert [s.celigo_id for s, _ in rows] == ["s1"]
        assert rows[0][0].content == "a" * 10

    async def test_detail_endpoint_still_returns_correct_content_and_other_families_count(self, db: AsyncSession):
        """End-to-end: the two-phase fetch (light scan for grouping/names,
        then a scoped full-content fetch for the target family) must not
        change `get_script_family`'s observable behaviour."""
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(db, tenant_id, conn_id, celigo_id="s1", name="dup_name", content="a" * 10)
        await _seed_script(db, tenant_id, conn_id, celigo_id="s2", name="dup_name", content="b" * 20)
        await db.flush()

        detail = await sf.get_script_family(db, tenant_id=tenant_id, connection_id=conn_id, dedup_key="s1")

        assert detail.members[0].content == "a" * 10
        assert detail.summary.other_families_with_name == 1


class TestDetailMembersCarryContentAndAreOrdered:
    async def test_members_ordered_last_modified_asc_nulls_last_then_celigo_id(self, db: AsyncSession):
        sf = _import_module()
        tenant_id, conn_id = await _basic_tenant_conn(db)
        await _seed_script(db, tenant_id, conn_id, celigo_id="orig", name="fam", content="body-a", last_modified=_ts(2))
        await _seed_script(
            db, tenant_id, conn_id, celigo_id="c_no_ts", name="fam copy no ts", content="body-b", source_id="orig"
        )
        await _seed_script(
            db,
            tenant_id,
            conn_id,
            celigo_id="c_earliest",
            name="fam copy earliest",
            content="body-c",
            source_id="orig",
            last_modified=_ts(1),
        )
        await db.flush()

        detail = await sf.get_script_family(db, tenant_id=tenant_id, connection_id=conn_id, dedup_key="orig")

        assert [m.celigo_id for m in detail.members] == ["c_earliest", "orig", "c_no_ts"]
        assert all(m.content is not None for m in detail.members)
        by_id = {m.celigo_id: m for m in detail.members}
        assert by_id["orig"].content == "body-a"
        assert by_id["orig"].is_original is True
        assert by_id["c_no_ts"].is_original is False
