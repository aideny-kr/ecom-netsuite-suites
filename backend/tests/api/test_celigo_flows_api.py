"""Task 8 — read APIs over the synced Celigo flow map.

Fixture note (same harness as ``test_celigo_connector_status.py``): ``admin_user``
returns ``(User, headers_dict)``; the DB session fixture is plain ``db``.

PII RULING (from the task-8 brief, not in the plan): ``celigo_flow_errors.message``
and ``celigo_error_signatures.sample_message`` hold raw Celigo error text (customer
emails, order refs). Returning them to an authenticated SAME-TENANT admin is
legitimate -- an operator triaging a failed flow needs to know which order broke.
The tests below deliberately assert the seeded PII text comes back in the response,
to pin that ruling as real behavior rather than a comment that could silently regress.

Every endpoint here is gated on ``connections.view`` AND ``require_feature("celigo")``
-- mirrors the four ``/connector-status/celigo*`` endpoints exactly. Every endpoint
is tested for: 403 when the flag is off, 404 (never a cross-tenant 200) when the
id belongs to another tenant, and the actual happy-path shape.

Seeding uses raw SQL for the ``connections`` row only (celigo_write_guard refuses an
ORM flush of a ``provider='celigo'`` row outside the connect/disconnect endpoints --
same reason ``test_celigo_flow_map_rls.py`` does this) and plain ORM ``db.add`` for
the eight flow-map tables themselves, which the guard does not cover.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.models.celigo import (
    CeligoConfigChange,
    CeligoErrorSignature,
    CeligoFlow,
    CeligoFlowError,
    CeligoFlowStep,
    CeligoIntegration,
    CeligoScript,
    CeligoScriptAttachment,
)
from app.models.pipeline import CursorState
from tests.conftest import enable_feature_flag

PII_MESSAGE = "Customer email jane.doe@example.com not found in NetSuite"


@pytest.fixture(autouse=True)
async def _celigo_flag_enabled(db, admin_user):
    """Enable the flag for tenant_a (admin_user) so the rest of the suite exercises
    normal (flag-on) behavior; the flag-off tests below turn it back off explicitly."""
    user, _ = admin_user
    await enable_feature_flag(db, user.tenant_id, "celigo")


async def _make_connection(db, tenant_id) -> uuid.UUID:
    conn_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO connections (id, tenant_id, provider, label, status, encrypted_credentials, encryption_key_version) "
            "VALUES (:id, :tenant_id, 'celigo', 'Celigo', 'active', 'unit-test-not-a-real-token', 1)"
        ).bindparams(id=conn_id, tenant_id=tenant_id)
    )
    await db.flush()
    return conn_id


async def _seed_world(db, tenant_id, *, pii_message: str = PII_MESSAGE) -> dict:
    """One connection -> one integration -> one flow -> one step (+ script +
    attachment) -> one error-signature -> one flow-error, for *tenant_id*."""
    conn_id = await _make_connection(db, tenant_id)
    suffix = uuid.uuid4().hex[:8]

    integration = CeligoIntegration(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        celigo_id=f"int_{suffix}",
        name="ACME ERP",
        sandbox=False,
        mode="settings",
        description="Production integration",
        raw_json={"_id": f"int_{suffix}"},
    )
    db.add(integration)
    await db.flush()

    flow = CeligoFlow(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        integration_id=integration.id,
        celigo_id=f"flow_{suffix}",
        name="Sales Order Sync",
        disabled=False,
        schedule={"type": "everyN", "unit": "minutes", "value": 15},
        raw_json={"_id": f"flow_{suffix}"},
    )
    db.add(flow)
    await db.flush()

    step = CeligoFlowStep(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        flow_id=flow.id,
        celigo_id=f"exp_{suffix}",
        role="generator",
        sequence=0,
        adaptor_type="NetSuiteExport",
        connection_celigo_id=f"conn_{suffix}",
        raw_json={},
    )
    db.add(step)
    await db.flush()

    script = CeligoScript(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        celigo_id=f"scr_{suffix}",
        name="Transform Script",
        content="function transform(record) { return record; }",
    )
    db.add(script)
    await db.flush()

    attachment = CeligoScriptAttachment(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        flow_id=flow.id,
        flow_step_id=step.id,
        script_id=script.id,
        script_celigo_id=script.celigo_id,
        function_name="transform",
        json_path="pageGenerators[0].transform.script",
        site_type="transform",
    )
    db.add(attachment)
    await db.flush()

    signature = CeligoErrorSignature(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        fingerprint=f"sig_{suffix}",
        source="import",
        code="ERR001",
        sample_message=pii_message,
        occurrence_count=1,
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )
    db.add(signature)
    await db.flush()

    error = CeligoFlowError(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        flow_id=flow.id,
        flow_step_id=step.id,
        signature_id=signature.id,
        celigo_id=f"err_{suffix}",
        trace_key=f"trace_{suffix}",
        source="import",
        code="ERR001",
        message=pii_message,
        occurred_at=datetime.now(timezone.utc),
        retriable=True,
    )
    db.add(error)
    await db.flush()

    return {
        "suffix": suffix,
        "connection_id": conn_id,
        "integration": integration,
        "flow": flow,
        "step": step,
        "script": script,
        "attachment": attachment,
        "signature": signature,
        "error": error,
    }


# ---------------------------------------------------------------------------
# GET /celigo/integrations
# ---------------------------------------------------------------------------


async def _seed_cron_flow(db, world: dict, *, schedule="? 0 */6 * * *", name: str = "Nightly Backfill") -> CeligoFlow:
    """A second flow under *world*'s integration carrying the schedule shape
    Celigo actually sends -- a cron STRING (96 of 239 live flows; `_seed_world`'s
    object form was a fixture invention never observed live). Shared by the
    list and detail tests so the two cannot drift apart (gate nit, PR #216)."""
    flow = CeligoFlow(
        tenant_id=world["integration"].tenant_id,
        celigo_connection_id=world["connection_id"],
        integration_id=world["integration"].id,
        celigo_id=f"flow_cron_{world['suffix']}",
        name=name,
        disabled=False,
        schedule=schedule,
        raw_json={},
    )
    db.add(flow)
    await db.flush()
    return flow


async def _seed_sandbox_world(db, world: dict) -> tuple[CeligoIntegration, CeligoFlow]:
    """A sandbox integration + one flow under it, on the same connection as
    *world*. Every read endpoint must treat both as if they did not exist."""
    integration = CeligoIntegration(
        tenant_id=world["integration"].tenant_id,
        celigo_connection_id=world["connection_id"],
        celigo_id=f"int_sb_{world['suffix']}",
        name="ACME ERP (sandbox)",
        sandbox=True,
        raw_json={},
    )
    db.add(integration)
    await db.flush()
    flow = CeligoFlow(
        tenant_id=world["integration"].tenant_id,
        celigo_connection_id=world["connection_id"],
        integration_id=integration.id,
        celigo_id=f"flow_sb_{world['suffix']}",
        name="Sandbox Sales Order Sync",
        disabled=False,
        raw_json={},
    )
    db.add(flow)
    await db.flush()
    return integration, flow


async def _seed_router_chain_flow(
    db, world: dict, *, name: str = "New Sales Order to NetSuite - Multi-Subsidiary"
) -> dict:
    """The real Multi-Subsidiary shape: source -> router 1 (one pass-through branch
    holding a lookup with a preSavePage hook) -> router 2 (two named branches, each:
    NetSuite lookup, add customer, update customer, add salesorder with a preMap hook).
    Two scripts: one single-copy, one 3-copy family with 2 differing versions."""
    tenant_id = world["integration"].tenant_id
    conn_id = world["connection_id"]
    sfx = world["suffix"]
    flow = CeligoFlow(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        integration_id=world["integration"].id,
        celigo_id=f"flow_chain_{sfx}",
        name=name,
        disabled=False,
        schedule="? 5,20,35,50 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23 ? * *",
        timezone="America/Los_Angeles",
        last_executed_at=datetime(2026, 9, 2, 17, 51, tzinfo=timezone.utc),
        celigo_last_modified=datetime(2026, 9, 2, tzinfo=timezone.utc),
        raw_json={
            "numOpenError": 0,
            "lastErrorAt": None,
            "routers": [
                {
                    "id": "r1",
                    "name": "",
                    "branches": [
                        {
                            "branchId": "b0",
                            "name": "",
                            "nextRouterId": "r2",
                            "pageProcessors": [{"_exportId": f"lkp_{sfx}"}],
                        }
                    ],
                },
                {
                    "id": "r2",
                    "name": "",
                    "routeRecordsTo": "first_matching_branch",
                    "routeRecordsUsing": "input_filters",
                    "branches": [
                        {
                            "branchId": "bIntl",
                            "name": "Framework Intl",
                            "inputFilter": {
                                "rules": ["notequals", ["string", ["extract", "business_entity"]], "Framework Inc"]
                            },
                            "pageProcessors": [{}, {}, {}, {}],
                        },
                        {
                            "branchId": "bInc",
                            "name": "Framework Inc",
                            "inputFilter": {
                                "rules": ["equals", ["string", ["extract", "business_entity"]], "Framework Inc"]
                            },
                            "pageProcessors": [{}, {}, {}, {}],
                        },
                    ],
                },
            ],
        },
    )
    db.add(flow)
    await db.flush()

    def step(
        celigo_id,
        role,
        adaptor,
        *,
        router=None,
        branch=None,
        seq=0,
        record_type=None,
        operation=None,
        search_id=None,
        reference_name=None,
        mapping=None,
    ):
        return CeligoFlowStep(
            tenant_id=tenant_id,
            celigo_connection_id=conn_id,
            flow_id=flow.id,
            celigo_id=celigo_id,
            role=role,
            adaptor_type=adaptor,
            router_id=router,
            branch_id=branch,
            sequence=seq,
            record_type=record_type,
            operation=operation,
            search_id=search_id,
            reference_name=reference_name,
            mapping_json=mapping,
            raw_json={},
        )

    steps = [
        step(f"src_{sfx}", "generator", "HTTPExport", reference_name="Get New Sales Orders"),
        step(
            f"lkp_{sfx}",
            "processor",
            "HTTPExport",
            router="r1",
            branch="b0",
            reference_name="Lookup Sales Orders (Multi-Subsidiary)",
            mapping={"fields": [{"extract": "a", "generate": "b"}] * 23},
        ),
    ]
    for branch, suffix in (("bIntl", "BV"), ("bInc", "Inc")):
        steps += [
            step(
                f"cust_lkp_{branch}_{sfx}",
                "processor",
                "NetSuiteExport",
                router="r2",
                branch=branch,
                seq=0,
                record_type="customer",
                search_id="5090",
                reference_name="Lookup Customer",
            ),
            step(
                f"cust_add_{branch}_{sfx}",
                "processor",
                "NetSuiteDistributedImport",
                router="r2",
                branch=branch,
                seq=1,
                record_type="customer",
                operation="add",
                reference_name=f"Import Customer ({suffix})",
            ),
            step(
                f"cust_upd_{branch}_{sfx}",
                "processor",
                "NetSuiteDistributedImport",
                router="r2",
                branch=branch,
                seq=2,
                record_type="customer",
                operation="update",
                reference_name="Update Currency",
            ),
            step(
                f"so_add_{branch}_{sfx}",
                "processor",
                "NetSuiteDistributedImport",
                router="r2",
                branch=branch,
                seq=3,
                record_type="salesorder",
                operation="add",
                reference_name=f"Add New Sales Order ({suffix})",
            ),
        ]
    db.add_all(steps)
    await db.flush()

    solo = CeligoScript(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        celigo_id=f"scr_solo_{sfx}",
        name="sales_order_script_v2",
        content="x" * 34145,
        content_hash="hsolo",
        celigo_last_modified=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    fam = [
        CeligoScript(
            tenant_id=tenant_id,
            celigo_connection_id=conn_id,
            celigo_id=f"scr_fam{i}_{sfx}",
            source_id=f"scr_fam0_{sfx}" if i else None,
            name="ns_sales_order_premap",
            content=c,
            content_hash=h,
            celigo_last_modified=datetime(2026, 1, 1 + i, tzinfo=timezone.utc),
        )
        for i, (c, h) in enumerate((("a" * 2284, "hA"), ("b" * 2443, "hB"), ("b" * 2443, "hB")))
    ]
    db.add_all([solo, *fam])
    await db.flush()
    by_id = {s.celigo_id: s for s in steps}
    atts = [
        CeligoScriptAttachment(
            tenant_id=tenant_id,
            celigo_connection_id=conn_id,
            flow_id=flow.id,
            flow_step_id=by_id[f"lkp_{sfx}"].id,
            script_id=solo.id,
            script_celigo_id=solo.celigo_id,
            function_name="preSavePage",
            json_path=f"lkp_{sfx}.hooks.preSavePage",
            site_type="hook",
        )
    ]
    for branch in ("bIntl", "bInc"):
        atts.append(
            CeligoScriptAttachment(
                tenant_id=tenant_id,
                celigo_connection_id=conn_id,
                flow_id=flow.id,
                flow_step_id=by_id[f"so_add_{branch}_{sfx}"].id,
                script_id=fam[1].id,
                script_celigo_id=fam[1].celigo_id,
                function_name="preMap",
                json_path=f"so_add_{branch}_{sfx}.hooks.preMap",
                site_type="hook",
            )
        )
    db.add_all(atts)
    await db.flush()
    return {"flow": flow, "steps": by_id, "solo": solo, "family": fam, "attachments": atts}


class TestListIntegrations:
    async def test_sandbox_integrations_are_not_listed(self, client, admin_user, db):
        """Production only -- operator directive 2026-09-01 ("don't bring sandbox
        celigo, just production"). A sandbox integration synced under the same
        connection must not appear. A NULL `sandbox` (Celigo omitted the flag)
        is treated as production, never hidden: hiding on an absent flag would
        let a missing field silently erase real integrations."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        db.add(
            CeligoIntegration(
                tenant_id=user.tenant_id,
                celigo_connection_id=world["connection_id"],
                celigo_id=f"int_sb_{world['suffix']}",
                name="ACME ERP (sandbox)",
                sandbox=True,
                raw_json={},
            )
        )
        db.add(
            CeligoIntegration(
                tenant_id=user.tenant_id,
                celigo_connection_id=world["connection_id"],
                celigo_id=f"int_null_{world['suffix']}",
                name="ACME Legacy",
                sandbox=None,
                raw_json={},
            )
        )
        await db.flush()

        r = await client.get("/api/v1/celigo/integrations", headers=headers)
        assert r.status_code == 200, r.text
        assert sorted(i["name"] for i in r.json()) == ["ACME ERP", "ACME Legacy"]

    async def test_lists_integrations_for_the_tenant(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        r = await client.get("/api/v1/celigo/integrations", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body) == 1
        assert body[0]["id"] == str(world["integration"].id)
        assert body[0]["celigo_id"] == world["integration"].celigo_id
        assert body[0]["name"] == "ACME ERP"
        assert body[0]["sandbox"] is False
        assert "raw_json" not in body[0], "raw_json must never leak through the explicit response model"

    async def test_empty_list_when_no_connection(self, client, admin_user):
        _, headers = admin_user
        r = await client.get("/api/v1/celigo/integrations", headers=headers)
        assert r.status_code == 200
        assert r.json() == []

    async def test_requires_permission(self, client):
        r = await client.get("/api/v1/celigo/integrations")
        assert r.status_code in (401, 403)

    async def test_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)
        r = await client.get("/api/v1/celigo/integrations", headers=headers)
        assert r.status_code == 403

    async def test_tenant_isolation(self, client, admin_user, admin_user_b, db):
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b
        await enable_feature_flag(db, user_b.tenant_id, "celigo")

        await _seed_world(db, user_a.tenant_id)
        world_b = await _seed_world(db, user_b.tenant_id)

        r = await client.get("/api/v1/celigo/integrations", headers=headers_b)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body) == 1
        assert body[0]["id"] == str(world_b["integration"].id), "tenant B must not see tenant A's integration"

    async def test_lists_summary_counts_writes_families_and_flow_schedules(self, client, admin_user, db):
        """Task 6: one request carries every dashboard summary the integration
        card needs -- flow-schedule buckets, topology/script aggregates, the
        write mix, and the per-flow schedule list -- so the list page never
        makes N follow-up calls per integration."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        paused = await _seed_cron_flow(db, world, name="Paused one")
        paused.disabled = True
        await db.flush()

        r = await client.get("/api/v1/celigo/integrations", headers=headers)
        assert r.status_code == 200, r.text
        row = next(i for i in r.json() if i["id"] == str(world["integration"].id))
        assert row["flow_count"] == 3 and row["paused_count"] == 1 and row["scheduled_count"] >= 1
        assert row["scheduled_count"] + row["on_demand_count"] + row["paused_count"] == row["flow_count"]
        assert row["step_count"] >= 10 and row["router_count"] >= 2 and row["lookup_count"] >= 3
        assert row["script_count"] >= 2 and row["error_count"] >= 0 and row["changes_last_24h"] == 0
        assert row["last_run_at"].startswith("2026-09-02T17:51")
        assert {"record_type": "salesorder", "count": 2} in row["writes"]
        assert "NetSuite" in row["adaptor_families"] and "HTTP" in row["adaptor_families"]
        sched = next(f for f in row["flow_schedules"] if f["id"] == str(chain["flow"].id))
        assert sched["disabled"] is False and sched["schedule"].startswith("? 5,20,35,50")
        assert sched["last_executed_at"] is not None

    async def test_signature_count_dedupes_across_multiple_errors_sharing_one_signature(self, client, admin_user, db):
        """Task 18 (cross-surface consistency): `signature_count` is the
        DISTINCT root-cause count across the whole integration, the same
        predicate as `error_count` (`celigo_error_is_open()`) but counting
        distinct `signature_id` instead of rows -- mirrors the per-flow
        query in `get_flow_detail`. Before this field existed, the tile's own
        `ErrorPill` defaulted the root-cause count to the raw error count
        (`signatureCount ?? count`), so two errors sharing one root cause
        read as "2 open · 2 root causes" on the tile while the SAME two rows
        correctly read "2 open · 1 root cause" one click away on the flows
        table or the flow page. Seeding a SECOND error against `_seed_world`'s
        existing signature (not a second signature) is what makes this
        assertion fail under the naive `error_count`-as-`signature_count`
        shortcut and pass under real distinct-count aggregation -- a fixture
        with one error per signature can't tell the two apart."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        db.add(
            CeligoFlowError(
                tenant_id=user.tenant_id,
                celigo_connection_id=world["connection_id"],
                flow_id=world["flow"].id,
                flow_step_id=world["step"].id,
                signature_id=world["signature"].id,
                celigo_id=f"err2_{world['suffix']}",
                trace_key=f"trace2_{world['suffix']}",
                source="import",
                code="ERR001",
                message="A second occurrence of the same root cause",
                occurred_at=datetime.now(timezone.utc),
                retriable=True,
            )
        )
        await db.flush()

        r = await client.get("/api/v1/celigo/integrations", headers=headers)
        assert r.status_code == 200, r.text
        row = next(i for i in r.json() if i["id"] == str(world["integration"].id))
        assert row["error_count"] == 2, "both errors are open and belong to this integration"
        assert row["signature_count"] == 1, "both errors share ONE signature -- must not double-count"


# ---------------------------------------------------------------------------
# GET /celigo/integrations/{id}/flows
# ---------------------------------------------------------------------------


class TestListIntegrationFlows:
    async def test_a_cron_string_schedule_is_served_not_500(self, client, admin_user, db):
        """LIVE DEFECT (2026-09-01, Framework staging): 96 of the 239 synced flows
        carry `schedule` as Celigo's cron STRING, none as an object. The response
        model declared `dict | None`, so every integration containing a scheduled
        flow raised ResponseValidationError -> 500 -> (a 500 carries no CORS
        headers) -> the browser saw "Failed to fetch" -> the flow map rendered
        "0 flows" for 26 of 36 integrations. `_seed_world`'s object-shaped
        schedule was a fixture invention; it was never observed live."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        await _seed_cron_flow(db, world)

        r = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/flows", headers=headers)
        assert r.status_code == 200, r.text
        by_name = {f["name"]: f for f in r.json()}
        assert by_name["Nightly Backfill"]["schedule"] == "? 0 */6 * * *"
        assert by_name["Sales Order Sync"]["schedule"] == {"type": "everyN", "unit": "minutes", "value": 15}

    async def test_a_schedule_shape_nobody_has_seen_yet_is_served_not_500(self, client, admin_user, db):
        """GATE FINDING (plausible, round 1): widening `dict | None` to
        `dict | str | None` repeated the reasoning that caused the 500 -- an
        enumeration of observed shapes. This column mirrors whatever Celigo
        sends; the API's job is to relay it, not to vouch for its shape. So the
        model is typed as JSON, and a list (or anything else) comes through."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        await _seed_cron_flow(db, world, schedule=[{"type": "cron", "expr": "? 0 */6 * * *"}], name="Listed")

        r = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/flows", headers=headers)
        assert r.status_code == 200, r.text
        by_name = {f["name"]: f for f in r.json()}
        assert by_name["Listed"]["schedule"] == [{"type": "cron", "expr": "? 0 */6 * * *"}]

    async def test_a_sandbox_integration_is_not_found_here_either(self, client, admin_user, db):
        """GATE FINDING (round 1): production-only was enforced in
        `/integrations`' WHERE clause alone; this route and the flow-detail
        route looked rows up by tenant only, so a sandbox integration hidden
        from the list was still fully readable by id (a bookmark, a stale
        cache). `celigo_integration_is_production()` is now the ONE predicate
        every read of these tables applies -- the `celigo_error_is_open()`
        idiom, not a second call-site patch."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        sandbox_integration, _ = await _seed_sandbox_world(db, world)

        r = await client.get(f"/api/v1/celigo/integrations/{sandbox_integration.id}/flows", headers=headers)
        assert r.status_code == 404, r.text

    async def test_lists_flows_with_error_and_signature_counts(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        # A second, unrelated error under the SAME signature -- error_count should
        # be 2, signature_count should stay 1 (it's one root cause, two occurrences).
        db.add(
            CeligoFlowError(
                tenant_id=user.tenant_id,
                celigo_connection_id=world["connection_id"],
                flow_id=world["flow"].id,
                signature_id=world["signature"].id,
                celigo_id=f"err2_{world['suffix']}",
                message=PII_MESSAGE,
                occurred_at=datetime.now(timezone.utc),
            )
        )
        # A RESOLVED error -- must not count as "open".
        db.add(
            CeligoFlowError(
                tenant_id=user.tenant_id,
                celigo_connection_id=world["connection_id"],
                flow_id=world["flow"].id,
                signature_id=world["signature"].id,
                celigo_id=f"err3_{world['suffix']}",
                message=PII_MESSAGE,
                occurred_at=datetime.now(timezone.utc),
                resolved_at=datetime.now(timezone.utc),
            )
        )
        await db.flush()

        r = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/flows", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body) == 1
        flow_out = body[0]
        assert flow_out["id"] == str(world["flow"].id)
        assert flow_out["name"] == "Sales Order Sync"
        assert flow_out["disabled"] is False
        assert flow_out["schedule"] == {"type": "everyN", "unit": "minutes", "value": 15}
        assert flow_out["error_count"] == 2, "the resolved error must not count as open"
        assert flow_out["signature_count"] == 1, "both open errors share one signature -- one root cause"

    async def test_purged_but_unresolved_error_does_not_count_as_open(self, client, admin_user, db):
        """WHOLE-BRANCH REVIEW FINDING 5: this endpoint's own definition of
        "open" (`resolved_at IS NULL AND purged_at IS NULL`, now sourced from
        `app.models.celigo.celigo_error_is_open`) must exclude a row Celigo's
        own ~30-day purge caught up with, even though this app never saw it
        resolve -- `purged_at` is set independently of `resolved_at`
        (`sync_service._purge_expired_errors`)."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        db.add(
            CeligoFlowError(
                tenant_id=user.tenant_id,
                celigo_connection_id=world["connection_id"],
                flow_id=world["flow"].id,
                signature_id=world["signature"].id,
                celigo_id=f"err_purged_{world['suffix']}",
                message=PII_MESSAGE,
                occurred_at=datetime.now(timezone.utc),
                purged_at=datetime.now(timezone.utc),  # resolved_at stays NULL, deliberately
            )
        )
        await db.flush()

        r = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/flows", headers=headers)
        assert r.status_code == 200, r.text
        flow_out = r.json()[0]
        assert flow_out["error_count"] == 1, "the purged-but-unresolved error must not count as open"
        assert flow_out["signature_count"] == 1

    async def test_paused_flow_stays_visible(self, client, admin_user, db):
        """MUST NOT be filtered out of the list -- the UI dims it via `disabled`."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        world["flow"].disabled = True
        await db.flush()

        r = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/flows", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body) == 1
        assert body[0]["disabled"] is True

    async def test_404_for_unknown_integration(self, client, admin_user):
        _, headers = admin_user
        r = await client.get(f"/api/v1/celigo/integrations/{uuid.uuid4()}/flows", headers=headers)
        assert r.status_code == 404

    async def test_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)
        r = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/flows", headers=headers)
        assert r.status_code == 403

    async def test_tenant_isolation(self, client, admin_user, admin_user_b, db):
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b
        await enable_feature_flag(db, user_b.tenant_id, "celigo")

        world_a = await _seed_world(db, user_a.tenant_id)

        r = await client.get(f"/api/v1/celigo/integrations/{world_a['integration'].id}/flows", headers=headers_b)
        assert r.status_code == 404, "tenant B must get 404, never tenant A's flows"

    async def test_lists_topology_script_and_write_aggregates_per_flow(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        r = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/flows", headers=headers)
        assert r.status_code == 200
        row = next(f for f in r.json() if f["id"] == str(chain["flow"].id))
        assert (row["step_count"], row["router_count"], row["branch_count"], row["lookup_count"]) == (10, 2, 3, 3)
        assert (row["script_count"], row["diverged_family_count"]) == (2, 1)
        assert sorted(row["writes"], key=lambda w: w["record_type"]) == [
            {"record_type": "customer", "count": 4},
            {"record_type": "salesorder", "count": 2},
        ]
        assert row["celigo_last_modified"].startswith("2026-09-02")
        base = next(f for f in r.json() if f["id"] == str(world["flow"].id))
        assert base["writes"] == [] or all("count" in w for w in base["writes"])

    async def test_diverged_family_count_ignores_sandbox_copies(self, client, admin_user, db):
        """GATE FINDING (round 1, Important): `diverged_keys` must be scoped to
        PRODUCTION scripts only (`celigo_script_is_production()`), same as
        every other read of `celigo_scripts` in this module -- see that
        predicate's own docstring ("a clone-family count that summed both
        environments was wrong by about half"). Two scenarios:

        1. A sandbox copy added to the ALREADY-diverged chain-flow family
           (2 distinct hashes among its production members) must not change
           `diverged_family_count` -- it's still exactly one diverged family,
           counted once (`COUNT(DISTINCT dedup_key)`), regardless of how many
           extra hash values a non-production copy contributes.
        2. A SECOND family whose production members share ONE content hash
           (genuinely not diverged) but whose ONLY sandbox copy carries a
           different hash must NOT be flagged diverged -- before the fix,
           `diverged_keys`' `HAVING COUNT(DISTINCT content_hash) > 1` counted
           the sandbox row too, incorrectly marking this family diverged and
           inflating `diverged_family_count` to 2."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        conn_id = world["connection_id"]
        sfx = world["suffix"]

        # Scenario 1 -- junk sandbox copy in the family that's already diverged.
        db.add(
            CeligoScript(
                tenant_id=user.tenant_id,
                celigo_connection_id=conn_id,
                celigo_id=f"scr_fam_sb_{sfx}",
                source_id=f"scr_fam0_{sfx}",  # same family as chain["family"]
                name="ns_sales_order_premap",
                content="sandbox junk",
                content_hash="hSandboxJunk",
                sandbox=True,
            )
        )

        # Scenario 2 -- a second family, NOT diverged among its production
        # copies (both share "hSame"), with a sandbox-only clone carrying a
        # different hash. The production original is attached to the chain
        # flow so its (non-)divergence is actually exercised by this endpoint.
        orig2 = CeligoScript(
            tenant_id=user.tenant_id,
            celigo_connection_id=conn_id,
            celigo_id=f"scr_fam2_orig_{sfx}",
            name="ns_customer_postmap",
            content="x",
            content_hash="hSame",
            sandbox=False,
        )
        clone2 = CeligoScript(
            tenant_id=user.tenant_id,
            celigo_connection_id=conn_id,
            celigo_id=f"scr_fam2_clone_{sfx}",
            source_id=f"scr_fam2_orig_{sfx}",
            name="ns_customer_postmap",
            content="x",
            content_hash="hSame",
            sandbox=False,
        )
        sandbox2 = CeligoScript(
            tenant_id=user.tenant_id,
            celigo_connection_id=conn_id,
            celigo_id=f"scr_fam2_sandbox_{sfx}",
            source_id=f"scr_fam2_orig_{sfx}",
            name="ns_customer_postmap",
            content="y",
            content_hash="hDifferentSandboxOnly",
            sandbox=True,
        )
        db.add_all([orig2, clone2, sandbox2])
        await db.flush()

        db.add(
            CeligoScriptAttachment(
                tenant_id=user.tenant_id,
                celigo_connection_id=conn_id,
                flow_id=chain["flow"].id,
                flow_step_id=chain["steps"][f"src_{sfx}"].id,
                script_id=orig2.id,
                script_celigo_id=orig2.celigo_id,
                function_name="postMap",
                json_path=f"src_{sfx}.hooks.postMap",
                site_type="hook",
            )
        )
        await db.flush()

        r = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/flows", headers=headers)
        assert r.status_code == 200, r.text
        row = next(f for f in r.json() if f["id"] == str(chain["flow"].id))
        assert row["script_count"] == 3, "solo + fam[1] + the new family-2 original, all production"
        assert row["diverged_family_count"] == 1, (
            "only the original chain family is diverged; family 2's production copies "
            "share one hash and its sandbox-only clone must not count"
        )


# ---------------------------------------------------------------------------
# GET /celigo/flows/{id}
# ---------------------------------------------------------------------------


class TestGetFlowDetail:
    async def test_a_cron_string_schedule_is_served_not_500(self, client, admin_user, db):
        """Twin of TestListIntegrationFlows' test: the detail model declared
        `schedule: dict | None` too, so opening any scheduled flow 500d."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        cron_flow = await _seed_cron_flow(db, world)

        r = await client.get(f"/api/v1/celigo/flows/{cron_flow.id}", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["schedule"] == "? 0 */6 * * *"

    async def test_a_flow_under_a_sandbox_integration_is_not_found(self, client, admin_user, db):
        """Twin of TestListIntegrationFlows' sandbox test: the detail route
        must apply the same shared production predicate, through the flow's
        integration."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        _, sandbox_flow = await _seed_sandbox_world(db, world)

        r = await client.get(f"/api/v1/celigo/flows/{sandbox_flow.id}", headers=headers)
        assert r.status_code == 404, r.text

    async def test_a_list_shaped_filter_is_served_not_500(self, client, admin_user, db):
        """filter_json/mapping_json are opaque Celigo config relayed as-is; a shape
        nobody has seen yet must not 500 a whole flow (the schedule lesson)."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        world["step"].filter_json = ["and", ["equals", ["string", ["extract", "status"]], "open"]]
        world["step"].mapping_json = "unexpected"
        await db.flush()
        r = await client.get(f"/api/v1/celigo/flows/{world['flow'].id}", headers=headers)
        assert r.status_code == 200
        assert r.json()["steps"][0]["filter_json"][0] == "and"
        assert r.json()["steps"][0]["mapping_json"] == "unexpected"

    async def test_step_carries_its_celigo_name_when_synced(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        world["step"].reference_name = "Lookup Customer"
        await db.flush()
        r = await client.get(f"/api/v1/celigo/flows/{world['flow'].id}", headers=headers)
        assert r.status_code == 200
        assert r.json()["steps"][0]["reference_name"] == "Lookup Customer"

    async def test_returns_flow_with_steps_and_attachments(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        r = await client.get(f"/api/v1/celigo/flows/{world['flow'].id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == str(world["flow"].id)
        assert body["name"] == "Sales Order Sync"
        assert body["integration_id"] == str(world["integration"].id)
        assert "raw_json" not in body

        assert len(body["steps"]) == 1
        step_out = body["steps"][0]
        assert step_out["id"] == str(world["step"].id)
        assert step_out["role"] == "generator"
        assert step_out["adaptor_type"] == "NetSuiteExport"
        assert step_out["branch_key"] == "$root", "STORED GENERATED column must round-trip"

        assert len(step_out["attachments"]) == 1
        att_out = step_out["attachments"][0]
        assert att_out["json_path"] == "pageGenerators[0].transform.script"
        assert att_out["function_name"] == "transform"
        assert att_out["script_id"] == str(world["script"].id)

        assert body["unassigned_attachments"] == []

    async def test_step_order_is_deterministic_across_a_three_way_sequence_tie(self, client, admin_user, db):
        """WHOLE-BRANCH REVIEW FINDING 7 (2026-08-27, PROVEN by execution):
        the docstring above `get_flow_detail` promises "generators then
        processors then router-branch processors, sequence within each", but
        the query was `ORDER BY sequence` alone, and `extract_flow_steps`
        restarts `sequence` at 0 per branch -- so a generator and two
        branch-processors in DIFFERENT branches all land at `sequence=0`, a
        three-way tie with no tiebreaker, making render order arbitrary and
        unstable across queries. Four steps, ALL at sequence=0, seeded in an
        order that does NOT match the expected output -- if the query has no
        real tiebreaker, this fails (or passes by accident, differently
        across re-runs); with one, it is exact and repeatable."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        flow_id = world["flow"].id
        conn_id = world["connection_id"]

        # Seeded out of expected-output order on purpose.
        db.add_all(
            [
                CeligoFlowStep(
                    tenant_id=user.tenant_id,
                    celigo_connection_id=conn_id,
                    flow_id=flow_id,
                    celigo_id="imp_branch_z",
                    role="processor",
                    router_id="router_1",
                    branch_id="branch_z",
                    sequence=0,
                    raw_json={},
                ),
                CeligoFlowStep(
                    tenant_id=user.tenant_id,
                    celigo_connection_id=conn_id,
                    flow_id=flow_id,
                    celigo_id="imp_top",
                    role="processor",
                    router_id=None,
                    branch_id=None,
                    sequence=0,
                    raw_json={},
                ),
                CeligoFlowStep(
                    tenant_id=user.tenant_id,
                    celigo_connection_id=conn_id,
                    flow_id=flow_id,
                    celigo_id="imp_branch_a",
                    role="processor",
                    router_id="router_1",
                    branch_id="branch_a",
                    sequence=0,
                    raw_json={},
                ),
            ]
        )
        await db.flush()
        # world["step"] is a pre-seeded generator (role=generator, sequence=0)
        # -- the fourth arm of the tie.

        r = await client.get(f"/api/v1/celigo/flows/{flow_id}", headers=headers)
        assert r.status_code == 200, r.text
        celigo_ids = [s["celigo_id"] for s in r.json()["steps"]]
        assert celigo_ids == [
            world["step"].celigo_id,  # generator -- always first
            "imp_top",  # top-level processor -- before any router branch
            "imp_branch_a",  # router-branch processor, branch "branch_a" before "branch_z"
            "imp_branch_z",
        ]

    async def test_step_order_is_total_even_when_the_router_or_branch_id_is_absent(self, client, admin_user, db):
        """SCOPED RE-REVIEW R2 (2026-08-27, PROVEN on the scratch DB): the
        finding-7 fix ordered by `(role_priority, router_id, branch_id,
        sequence)`, which is a large improvement but still not TOTAL. Two tie
        shapes survive it, both seeded below:

          * one router, two branches that carry NO `branchId`
            -> ('processor', 'router_tie', NULL, 0) twice;
          * two routers that carry NO `id`, under the same `branchId`
            -> ('processor', NULL, 'branch_shared', 0) twice.

        Both rows persist legitimately in each case -- `celigo_id` differs, so
        the `branch_key` unique constraint does not collapse them -- and the
        `ORDER BY` had no further key, leaving render order arbitrary.
        `celigo_id` is appended as a final tiebreaker: it is NOT NULL and
        unique within a flow's step set, so the order is now total.

        Seeded in reverse of the expected output on purpose -- with no real
        final tiebreaker this returns insertion order and fails."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        flow_id = world["flow"].id
        conn_id = world["connection_id"]

        def _step(celigo_id: str, *, router_id: str | None, branch_id: str | None) -> CeligoFlowStep:
            return CeligoFlowStep(
                tenant_id=user.tenant_id,
                celigo_connection_id=conn_id,
                flow_id=flow_id,
                celigo_id=celigo_id,
                role="processor",
                router_id=router_id,
                branch_id=branch_id,
                sequence=0,
                raw_json={},
            )

        db.add_all(
            [
                # Tie shape 1 -- a router whose branches carry no branchId.
                _step("imp_tie_1b", router_id="router_tie", branch_id=None),
                # Tie shape 2 -- routers with no id, sharing one branchId.
                _step("imp_tie_2b", router_id=None, branch_id="branch_shared"),
                _step("imp_tie_1a", router_id="router_tie", branch_id=None),
                _step("imp_tie_2a", router_id=None, branch_id="branch_shared"),
            ]
        )
        await db.flush()

        r = await client.get(f"/api/v1/celigo/flows/{flow_id}", headers=headers)
        assert r.status_code == 200, r.text
        celigo_ids = [s["celigo_id"] for s in r.json()["steps"]]
        assert celigo_ids == [
            world["step"].celigo_id,  # generator -- always first
            # router_id IS NULL -> the "top-level processor" priority group,
            # then celigo_id breaks the otherwise-total tie inside it.
            "imp_tie_2a",
            "imp_tie_2b",
            # router-branch group; branch_id NULL on both, so celigo_id is
            # the only thing left to order by.
            "imp_tie_1a",
            "imp_tie_1b",
        ]

    async def test_router_level_attachment_has_no_step_appears_unassigned(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        db.add(
            CeligoScriptAttachment(
                tenant_id=user.tenant_id,
                celigo_connection_id=world["connection_id"],
                flow_id=world["flow"].id,
                flow_step_id=None,
                script_id=world["script"].id,
                script_celigo_id=world["script"].celigo_id,
                function_name=None,
                json_path="routers[0].script",
                site_type="router",
            )
        )
        await db.flush()

        r = await client.get(f"/api/v1/celigo/flows/{world['flow'].id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["unassigned_attachments"]) == 1
        assert body["unassigned_attachments"][0]["json_path"] == "routers[0].script"

    async def test_detail_projects_kinds_facts_routers_and_script_families(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        r = await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}", headers=headers)
        assert r.status_code == 200
        body = r.json()
        kinds = {s["celigo_id"]: s["kind"] for s in body["steps"]}
        sfx = world["suffix"]
        assert (
            kinds[f"src_{sfx}"] == "source"
            and kinds[f"lkp_{sfx}"] == "lookup"
            and kinds[f"cust_lkp_bIntl_{sfx}"] == "lookup"
            and kinds[f"so_add_bInc_{sfx}"] == "destination"
        )
        so = next(s for s in body["steps"] if s["celigo_id"] == f"so_add_bIntl_{sfx}")
        assert (so["record_type"], so["operation"], so["search_id"], so["reference_name"]) == (
            "salesorder",
            "add",
            None,
            "Add New Sales Order (BV)",
        )
        lk = next(s for s in body["steps"] if s["celigo_id"] == f"cust_lkp_bIntl_{sfx}")
        assert (lk["record_type"], lk["search_id"]) == ("customer", "5090")
        assert [rt["id"] for rt in body["routers"]] == ["r1", "r2"]
        assert body["routers"][0]["branches"][0]["next_router_id"] == "r2"
        assert [b["name"] for b in body["routers"][1]["branches"]] == ["Framework Intl", "Framework Inc"]
        assert body["routers"][1]["branches"][0]["rule_count"] == 1
        assert body["celigo_open_error_count"] == 0 and body["last_error_at"] is None
        hook = next(s for s in body["steps"] if s["celigo_id"] == f"so_add_bInc_{sfx}")["attachments"][0]
        assert (
            hook["script_name"],
            hook["script_copies_count"],
            hook["script_versions_count"],
            hook["script_version_letter"],
            hook["script_content_diverged"],
        ) == ("ns_sales_order_premap", 3, 2, "B", True)
        solo = next(s for s in body["steps"] if s["celigo_id"] == f"lkp_{sfx}")["attachments"][0]
        assert (solo["script_copies_count"], solo["script_version_letter"], solo["script_size_chars"]) == (
            1,
            None,
            34145,
        )

    async def test_404_for_unknown_flow(self, client, admin_user):
        _, headers = admin_user
        r = await client.get(f"/api/v1/celigo/flows/{uuid.uuid4()}", headers=headers)
        assert r.status_code == 404

    async def test_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)
        r = await client.get(f"/api/v1/celigo/flows/{world['flow'].id}", headers=headers)
        assert r.status_code == 403

    async def test_tenant_isolation(self, client, admin_user, admin_user_b, db):
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b
        await enable_feature_flag(db, user_b.tenant_id, "celigo")

        world_a = await _seed_world(db, user_a.tenant_id)

        r = await client.get(f"/api/v1/celigo/flows/{world_a['flow'].id}", headers=headers_b)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /celigo/flows/{id}/errors
# ---------------------------------------------------------------------------


class TestFlowErrors:
    async def _seed_two_step_errors(self, db, world, chain):
        sfx = world["suffix"]
        tenant_id = world["integration"].tenant_id
        sig = world["signature"]
        rows = []
        for i, step_key in enumerate((f"lkp_{sfx}", f"lkp_{sfx}", f"so_add_bIntl_{sfx}")):
            rows.append(
                CeligoFlowError(
                    tenant_id=tenant_id,
                    celigo_connection_id=world["connection_id"],
                    flow_id=chain["flow"].id,
                    flow_step_id=chain["steps"][step_key].id,
                    signature_id=sig.id,
                    celigo_id=f"err_{i}_{sfx}",
                    trace_key=f"1582211{i}",
                    source="pre_save_page_hook",
                    code="script_error",
                    message="TypeError: null",
                    occurred_at=datetime(2026, 8, 17, 6 + i, tzinfo=timezone.utc),
                    purge_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    retriable=False,
                )
            )
        rows.append(
            CeligoFlowError(
                tenant_id=tenant_id,
                celigo_connection_id=world["connection_id"],
                flow_id=chain["flow"].id,
                flow_step_id=chain["steps"][f"lkp_{sfx}"].id,
                signature_id=sig.id,
                celigo_id=f"err_resolved_{sfx}",
                source="pre_save_page_hook",
                code="script_error",
                message="old",
                occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                resolved_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            )
        )
        db.add_all(rows)
        await db.flush()

    async def test_detail_carries_open_counts_per_step_and_per_flow(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        await self._seed_two_step_errors(db, world, chain)
        body = (await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}", headers=headers)).json()
        sfx = world["suffix"]
        counts = {s["celigo_id"]: s["error_count"] for s in body["steps"]}
        assert counts[f"lkp_{sfx}"] == 2 and counts[f"so_add_bIntl_{sfx}"] == 1 and counts[f"src_{sfx}"] == 0
        assert body["error_count"] == 3 and body["signature_count"] == 1

    async def test_flow_error_count_includes_errors_no_step_owns(self, client, admin_user, db):
        """Final-review finding I9. Celigo can report an error against a FLOW
        with no `flow_step_id` (a router-level or pre-dispatch failure). The
        flow's `error_count` counts it -- the flow total is every open error,
        full stop -- which means the steps can legitimately sum to LESS than
        the flow. The old docstring claimed the two agreed "by construction";
        this pins the real contract so the next reader trusts the number
        rather than the sentence."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        await self._seed_two_step_errors(db, world, chain)
        db.add(
            CeligoFlowError(
                tenant_id=world["integration"].tenant_id,
                celigo_connection_id=world["connection_id"],
                flow_id=chain["flow"].id,
                flow_step_id=None,
                signature_id=world["signature"].id,
                celigo_id=f"err_unattributed_{world['suffix']}",
                source="router",
                code="script_error",
                message="TypeError: null",
                occurred_at=datetime(2026, 8, 17, 9, tzinfo=timezone.utc),
            )
        )
        await db.flush()

        body = (await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}", headers=headers)).json()
        steps_sum = sum(s["error_count"] for s in body["steps"])
        assert steps_sum == 3, "no step owns the new error"
        assert body["error_count"] == steps_sum + 1 == 4
        assert body["signature_count"] == 1, "one root cause, however it is attributed"

    async def test_grouped_errors_by_signature_with_step_attribution_and_trace_keys(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        await self._seed_two_step_errors(db, world, chain)
        r = await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}/errors", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "open" and body["total"] == 3 and len(body["groups"]) == 1
        g = body["groups"][0]
        assert g["signature"]["id"] == str(world["signature"].id)
        assert g["count"] == 3 and sorted(g["trace_keys"]) == ["15822110", "15822111", "15822112"]
        assert set(g["step_ids"]) == {
            str(chain["steps"][f"lkp_{world['suffix']}"].id),
            str(chain["steps"][f"so_add_bIntl_{world['suffix']}"].id),
        }
        assert g["first_seen_at"].startswith("2026-08-17T06") and g["last_seen_at"].startswith("2026-08-17T08")
        assert g["retriable"] is False and g["purge_at"].startswith("2026-09-16")
        assert "message" in g["errors"][0]

    async def test_resolved_filter_and_404s(self, client, admin_user, admin_user_b, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)
        await self._seed_two_step_errors(db, world, chain)
        r = await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}/errors?status=resolved", headers=headers)
        assert r.status_code == 200 and r.json()["total"] == 1
        assert (
            await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}/errors?status=bogus", headers=headers)
        ).status_code == 422
        assert (await client.get(f"/api/v1/celigo/flows/{uuid.uuid4()}/errors", headers=headers)).status_code == 404
        user_b, headers_b = admin_user_b
        # Brief's literal test omitted this -- every OTHER tenant-isolation test in
        # this file enables the flag for tenant B first (see TestGetFlowDetail's
        # own test_tenant_isolation) so the 404 asserted below is proven by the
        # flow lookup, not a 403 from a flag this tenant never had.
        await enable_feature_flag(db, user_b.tenant_id, "celigo")
        assert (
            await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}/errors", headers=headers_b)
        ).status_code == 404


# ---------------------------------------------------------------------------
# GET /celigo/scripts/{id}
# ---------------------------------------------------------------------------


class TestGetScriptDetail:
    async def test_a_sandbox_script_is_not_found(self, client, admin_user, db):
        """Scripts carry their own `sandbox` flag (132 of the live account's 259
        are sandbox copies). Same shared-predicate rule as the flow routes:
        hidden means hidden by id too, not only from a listing."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        sandbox_script = CeligoScript(
            tenant_id=user.tenant_id,
            celigo_connection_id=world["connection_id"],
            celigo_id=f"scr_sb_{world['suffix']}",
            name="Sandbox Transform",
            content="function transform(record) { return record; }",
            sandbox=True,
        )
        db.add(sandbox_script)
        await db.flush()

        r = await client.get(f"/api/v1/celigo/scripts/{sandbox_script.id}", headers=headers)
        assert r.status_code == 404, r.text

    async def test_attachment_count_and_used_by_are_the_same_production_rows(self, client, admin_user, db):
        """GATE FINDING (round 3, major): `used_by` was production-joined but
        `attachment_count` (from `list_logical_scripts`) counted every
        attachment of the clone family, sandbox flows included, so the two
        numbers on one response could disagree. Both now come from the same
        production-filtered row set -- there is nothing left to keep in
        step. Seeds the exact case: one script attached under a production
        flow AND under a sandbox one."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        _, sandbox_flow = await _seed_sandbox_world(db, world)
        db.add(
            CeligoScriptAttachment(
                tenant_id=user.tenant_id,
                celigo_connection_id=world["connection_id"],
                flow_id=sandbox_flow.id,
                flow_step_id=None,
                script_id=world["script"].id,
                script_celigo_id=world["script"].celigo_id,
                function_name="transform",
                json_path="routers[0].script",
                site_type="router",
            )
        )
        await db.flush()

        r = await client.get(f"/api/v1/celigo/scripts/{world['script'].id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert [site["flow_name"] for site in body["used_by"]] == ["Sales Order Sync"]
        assert body["attachment_count"] == len(body["used_by"]) == 1
        assert body["integration_count"] == 1

    async def test_returns_content_and_used_by(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        r = await client.get(f"/api/v1/celigo/scripts/{world['script'].id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == str(world["script"].id)
        assert body["content"] == "function transform(record) { return record; }"
        assert body["dedup_key"] == world["script"].celigo_id, "original script's dedup_key is its own celigo_id"
        assert body["copies_count"] == 1
        assert body["content_diverged"] is False
        assert body["integration_count"] == 1

        assert len(body["used_by"]) == 1
        site = body["used_by"][0]
        assert site["flow_id"] == str(world["flow"].id)
        assert site["flow_name"] == "Sales Order Sync"
        assert site["integration_id"] == str(world["integration"].id)
        assert site["json_path"] == "pageGenerators[0].transform.script"
        assert site["function_name"] == "transform"

    async def test_collapses_clone_family(self, client, admin_user, db):
        """A clone (source_id pointing at the original) must be counted and its
        own attachment site surfaced under the SAME logical script."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        clone = CeligoScript(
            tenant_id=user.tenant_id,
            celigo_connection_id=world["connection_id"],
            celigo_id=f"scr_clone_{world['suffix']}",
            name="Transform Script (clone)",
            content="function transform(record) { return record; }",
            source_id=world["script"].celigo_id,
        )
        db.add(clone)
        await db.flush()

        db.add(
            CeligoScriptAttachment(
                tenant_id=user.tenant_id,
                celigo_connection_id=world["connection_id"],
                flow_id=world["flow"].id,
                flow_step_id=None,
                script_id=clone.id,
                script_celigo_id=clone.celigo_id,
                function_name="transform",
                json_path="pageProcessors[0].transform.script",
                site_type="transform",
            )
        )
        await db.flush()

        # Query by the ORIGINAL's id -- the clone must still show up in the group.
        r = await client.get(f"/api/v1/celigo/scripts/{world['script'].id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["copies_count"] == 2
        assert len(body["used_by"]) == 2
        paths = {s["json_path"] for s in body["used_by"]}
        assert paths == {"pageGenerators[0].transform.script", "pageProcessors[0].transform.script"}

        # And querying by the CLONE's id resolves to the same logical group.
        r2 = await client.get(f"/api/v1/celigo/scripts/{clone.id}", headers=headers)
        assert r2.status_code == 200, r2.text
        assert r2.json()["copies_count"] == 2
        assert r2.json()["dedup_key"] == world["script"].celigo_id

    async def test_integration_count_reflects_distinct_integrations(self, client, admin_user, db):
        """The mockup's headline pill is "20 copies · 14 integrations" -- a
        second integration/flow in the SAME connection that also attaches the
        SAME script must push `integration_count` to 2, computed from the
        already-fetched `used_by` rows (no second query)."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        integration2 = CeligoIntegration(
            tenant_id=user.tenant_id,
            celigo_connection_id=world["connection_id"],
            celigo_id=f"int2_{world['suffix']}",
            name="Second ERP",
            raw_json={},
        )
        db.add(integration2)
        await db.flush()

        flow2 = CeligoFlow(
            tenant_id=user.tenant_id,
            celigo_connection_id=world["connection_id"],
            integration_id=integration2.id,
            celigo_id=f"flow2_{world['suffix']}",
            name="Second Flow",
            raw_json={},
        )
        db.add(flow2)
        await db.flush()

        db.add(
            CeligoScriptAttachment(
                tenant_id=user.tenant_id,
                celigo_connection_id=world["connection_id"],
                flow_id=flow2.id,
                flow_step_id=None,
                script_id=world["script"].id,
                script_celigo_id=world["script"].celigo_id,
                function_name="transform",
                json_path="pageProcessors[0].transform.script",
                site_type="transform",
            )
        )
        await db.flush()

        r = await client.get(f"/api/v1/celigo/scripts/{world['script'].id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["integration_count"] == 2
        integration_ids_seen = {site["integration_id"] for site in body["used_by"]}
        assert integration_ids_seen == {str(world["integration"].id), str(integration2.id)}

    async def test_used_by_never_includes_another_tenants_flow_or_step_context(
        self, client, admin_user, admin_user_b, db
    ):
        """Strong form: with a SECOND tenant's full world coexisting in the
        DB, `used_by`'s joined flow/step/integration context must never
        surface tenant B's rows. Not exploitable today (an attachment's own
        `flow_id` always points at a same-tenant flow by construction -- see
        `celigo_flows.py`'s comment on this join), but the join predicates
        must hold on their own merit, not merely inherit safety from the
        attachment-level filter -- this is what actually proves that."""
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b
        await enable_feature_flag(db, user_b.tenant_id, "celigo")

        world_a = await _seed_world(db, user_a.tenant_id)
        world_b = await _seed_world(db, user_b.tenant_id)

        r = await client.get(f"/api/v1/celigo/scripts/{world_a['script'].id}", headers=headers_a)
        assert r.status_code == 200, r.text
        body = r.json()

        flow_ids_seen = {site["flow_id"] for site in body["used_by"]}
        integration_ids_seen = {site["integration_id"] for site in body["used_by"]}
        assert flow_ids_seen == {str(world_a["flow"].id)}, "tenant B's flow must never appear"
        assert integration_ids_seen == {str(world_a["integration"].id)}, "tenant B's integration must never appear"
        assert str(world_b["flow"].id) not in flow_ids_seen
        assert str(world_b["integration"].id) not in integration_ids_seen

    async def test_404_for_unknown_script(self, client, admin_user):
        _, headers = admin_user
        r = await client.get(f"/api/v1/celigo/scripts/{uuid.uuid4()}", headers=headers)
        assert r.status_code == 404

    async def test_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)
        r = await client.get(f"/api/v1/celigo/scripts/{world['script'].id}", headers=headers)
        assert r.status_code == 403

    async def test_tenant_isolation(self, client, admin_user, admin_user_b, db):
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b
        await enable_feature_flag(db, user_b.tenant_id, "celigo")

        world_a = await _seed_world(db, user_a.tenant_id)

        r = await client.get(f"/api/v1/celigo/scripts/{world_a['script'].id}", headers=headers_b)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /celigo/errors?signature=...
# ---------------------------------------------------------------------------


class TestGetErrorsForSignature:
    async def test_returns_signature_and_its_errors(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        r = await client.get("/api/v1/celigo/errors", params={"signature": str(world["signature"].id)}, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["signature"]["id"] == str(world["signature"].id)
        assert body["signature"]["fingerprint"] == world["signature"].fingerprint
        assert body["signature"]["sample_message"] == PII_MESSAGE, (
            "an authenticated same-tenant admin must see the raw error text -- it's the diagnosis"
        )

        assert len(body["errors"]) == 1
        err_out = body["errors"][0]
        assert err_out["id"] == str(world["error"].id)
        assert err_out["message"] == PII_MESSAGE
        assert err_out["trace_key"] == world["error"].trace_key
        assert err_out["flow_id"] == str(world["flow"].id)

    async def test_requires_signature_param(self, client, admin_user):
        _, headers = admin_user
        r = await client.get("/api/v1/celigo/errors", headers=headers)
        assert r.status_code == 422

    async def test_404_for_unknown_signature(self, client, admin_user):
        _, headers = admin_user
        r = await client.get("/api/v1/celigo/errors", params={"signature": str(uuid.uuid4())}, headers=headers)
        assert r.status_code == 404

    async def test_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)
        r = await client.get("/api/v1/celigo/errors", params={"signature": str(world["signature"].id)}, headers=headers)
        assert r.status_code == 403

    async def test_tenant_isolation(self, client, admin_user, admin_user_b, db):
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b
        await enable_feature_flag(db, user_b.tenant_id, "celigo")

        world_a = await _seed_world(db, user_a.tenant_id)

        r = await client.get(
            "/api/v1/celigo/errors", params={"signature": str(world_a["signature"].id)}, headers=headers_b
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /celigo/sync-status
# ---------------------------------------------------------------------------


class TestSyncStatus:
    async def test_null_when_never_synced(self, client, admin_user, db):
        """A connection exists but Task 7's worker has never completed a run
        for it -- no `cursor_states` row at all."""
        user, headers = admin_user
        await _seed_world(db, user.tenant_id)

        r = await client.get("/api/v1/celigo/sync-status", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["last_synced_at"] is None

    async def test_null_when_no_connection_at_all(self, client, admin_user):
        _, headers = admin_user
        r = await client.get("/api/v1/celigo/sync-status", headers=headers)
        assert r.status_code == 200
        assert r.json()["last_synced_at"] is None

    async def test_returns_last_synced_at_after_a_sync(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        synced_at = datetime.now(timezone.utc)
        db.add(
            CursorState(
                connection_id=world["connection_id"],
                object_type="celigo_flow_map",
                cursor_value=synced_at.isoformat(),
                last_synced_at=synced_at,
            )
        )
        await db.flush()

        r = await client.get("/api/v1/celigo/sync-status", headers=headers)
        assert r.status_code == 200, r.text
        returned = r.json()["last_synced_at"]
        assert returned is not None
        assert abs((datetime.fromisoformat(returned) - synced_at).total_seconds()) < 1

    async def test_a_different_cursor_object_type_is_ignored(self, client, admin_user, db):
        """`cursor_states` is a shared table across every ingestion feature --
        a row for a DIFFERENT `object_type` on the SAME connection must never
        be mistaken for a flow-map sync."""
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        db.add(
            CursorState(
                connection_id=world["connection_id"],
                object_type="celigo_something_else",
                last_synced_at=datetime.now(timezone.utc),
            )
        )
        await db.flush()

        r = await client.get("/api/v1/celigo/sync-status", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["last_synced_at"] is None

    async def test_requires_permission(self, client):
        r = await client.get("/api/v1/celigo/sync-status")
        assert r.status_code in (401, 403)

    async def test_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)
        r = await client.get("/api/v1/celigo/sync-status", headers=headers)
        assert r.status_code == 403

    async def test_tenant_isolation(self, client, admin_user, admin_user_b, db):
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b
        await enable_feature_flag(db, user_b.tenant_id, "celigo")

        world_a = await _seed_world(db, user_a.tenant_id)
        synced_at = datetime.now(timezone.utc)
        db.add(
            CursorState(
                connection_id=world_a["connection_id"],
                object_type="celigo_flow_map",
                cursor_value=synced_at.isoformat(),
                last_synced_at=synced_at,
            )
        )
        await db.flush()

        r = await client.get("/api/v1/celigo/sync-status", headers=headers_b)
        assert r.status_code == 200, r.text
        assert r.json()["last_synced_at"] is None, "tenant B must not see tenant A's sync cursor"


# ---------------------------------------------------------------------------
# GET /celigo/integrations/{id}/changes, GET /celigo/flows/{id}/changes
# ---------------------------------------------------------------------------


class TestChanges:
    """Task 7 -- config-change routes. Both resolve their parent through the
    same production join `list_integration_flows`/`get_flow_detail` use (404
    otherwise, same as every other route in this module), tenant-scope the
    `celigo_config_changes` rows, and order newest first (`created_at desc,
    id desc` -- `id desc` is only a tiebreak for rows that share the exact
    same `created_at`; the test below pins two rows to distinct explicit
    `created_at` values so the ordering assertion exercises `created_at desc`
    itself, not the tiebreak)."""

    async def test_integration_and_flow_changes_newest_first(self, client, admin_user, admin_user_b, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        # Explicit, distinct `created_at` values on each row -- NOT reliance
        # on flushing between two `db.add` calls. Postgres's `now()` (which
        # `TimestampMixin.created_at`'s server_default uses) is fixed for the
        # whole transaction, so two statements in the same still-open
        # transaction get the IDENTICAL `now()` regardless of how many
        # flushes separate them; `db.flush()` pushes pending SQL over the
        # same connection, it does not start a new transaction. That made
        # the two rows tie on `created_at`, and the "newest first" ordering
        # then fell through to `id desc` on a random UUID4 -- a coin flip,
        # confirmed by running this test 8x back-to-back pre-fix (5/8
        # failed with the exact assertion shape below). Passing `created_at`
        # explicitly bypasses the server default entirely, so the ordering
        # assertion actually exercises `created_at desc`.
        t_older = datetime.now(timezone.utc) - timedelta(milliseconds=10)
        t_newer = datetime.now(timezone.utc)
        older = CeligoConfigChange(
            tenant_id=user.tenant_id,
            celigo_connection_id=world["connection_id"],
            flow_id=world["flow"].id,
            object_kind="flow",
            object_id=world["flow"].id,
            celigo_id=world["flow"].celigo_id,
            field="disabled",
            old_value=False,
            new_value=True,
            created_at=t_older,
        )
        newer = CeligoConfigChange(
            tenant_id=user.tenant_id,
            celigo_connection_id=world["connection_id"],
            flow_id=world["flow"].id,
            object_kind="flow_step",
            object_id=world["step"].id,
            celigo_id=world["step"].celigo_id,
            field="mapping_json",
            old_value={"a": 1},
            new_value=["x"],
            created_at=t_newer,
        )
        db.add(older)
        db.add(newer)
        await db.flush()

        r = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/changes", headers=headers)
        assert r.status_code == 200 and [c["field"] for c in r.json()] == ["mapping_json", "disabled"]
        assert r.json()[0]["new_value"] == ["x"]

        r = await client.get(f"/api/v1/celigo/flows/{world['flow'].id}/changes", headers=headers)
        assert r.status_code == 200 and len(r.json()) == 2

        assert (
            await client.get(f"/api/v1/celigo/integrations/{uuid.uuid4()}/changes", headers=headers)
        ).status_code == 404

        # CONTROLLER RULING R6 (overrides the brief's literal test): every
        # OTHER tenant-isolation test in this file enables the flag for
        # tenant B first (see e.g. TestGetFlowDetail's own
        # test_tenant_isolation) -- without it this request 403s on the flag
        # and proves nothing about tenant isolation.
        user_b, headers_b = admin_user_b
        await enable_feature_flag(db, user_b.tenant_id, "celigo")
        assert (
            await client.get(f"/api/v1/celigo/flows/{world['flow'].id}/changes", headers=headers_b)
        ).status_code == 404
