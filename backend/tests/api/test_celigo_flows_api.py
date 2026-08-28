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
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.models.celigo import (
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


class TestListIntegrations:
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


# ---------------------------------------------------------------------------
# GET /celigo/integrations/{id}/flows
# ---------------------------------------------------------------------------


class TestListIntegrationFlows:
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


# ---------------------------------------------------------------------------
# GET /celigo/flows/{id}
# ---------------------------------------------------------------------------


class TestGetFlowDetail:
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
# GET /celigo/scripts/{id}
# ---------------------------------------------------------------------------


class TestGetScriptDetail:
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
