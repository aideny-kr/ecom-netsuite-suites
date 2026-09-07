"""Task 2 -- API gates over `app/services/celigo/script_families.py`
(spec `docs/superpowers/specs/2026-09-06-celigo-scripts-view-design.md` §2.4).

Reuses `_seed_world` from `test_celigo_flows_api.py` (one connection -> one
integration -> one flow -> one step -> one script + attachment) rather than
duplicating a world-builder -- same discipline as
`test_celigo_read_queries_parity.py`. Fixture script content is synthetic
JavaScript (`"function transform(record) { return record; }"`, set by
`_seed_world` itself) -- never real customer script text.

These tests exercise the SHAPE the API layer owns per the spec: connection
resolution, the empty-list-not-500 rule when no connection exists, the
content/content_hash boundary between the LIST and DETAIL responses, the
404s, and route precedence over `/scripts/{script_id}`. Family-rule
correctness itself (kind classification, version letters, divergence, ...)
is Task 1's `test_celigo_script_families.py`, not repeated here.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.celigo import CeligoFlow, CeligoIntegration, CeligoScript, CeligoScriptAttachment
from tests.api.test_celigo_flows_api import _make_connection, _seed_world
from tests.conftest import enable_feature_flag


@pytest.fixture(autouse=True)
async def _celigo_flag_enabled(db, admin_user):
    user, _ = admin_user
    await enable_feature_flag(db, user.tenant_id, "celigo")


async def _seed_family_with_fixed_celigo_id(db, tenant_id, *, celigo_id: str, name: str, content: str) -> dict:
    """A minimal one-script, one-site family under a NEW connection for
    *tenant_id*, with the script's OWN `celigo_id` (and therefore
    `dedup_key`, since no `_sourceId` is set) pinned to *celigo_id* --
    unlike `_seed_world` (which generates a random suffix per call, so two
    calls can never collide), this lets two different tenants share the
    exact same dedup_key on purpose, for `test_tenant_isolation` below."""
    conn_id = await _make_connection(db, tenant_id)
    suffix = uuid.uuid4().hex[:8]

    integration = CeligoIntegration(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        celigo_id=f"int_{suffix}",
        name=f"{name} Integration",
        sandbox=False,
        raw_json={},
    )
    db.add(integration)
    await db.flush()

    flow = CeligoFlow(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        integration_id=integration.id,
        celigo_id=f"flow_{suffix}",
        name=f"{name} Flow",
        disabled=False,
        raw_json={},
    )
    db.add(flow)
    await db.flush()

    script = CeligoScript(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        celigo_id=celigo_id,
        name=name,
        content=content,
    )
    db.add(script)
    await db.flush()

    attachment = CeligoScriptAttachment(
        tenant_id=tenant_id,
        celigo_connection_id=conn_id,
        flow_id=flow.id,
        flow_step_id=None,
        script_id=script.id,
        script_celigo_id=script.celigo_id,
        function_name="transform",
        json_path="transform.script",
        site_type="transform",
    )
    db.add(attachment)
    await db.flush()

    return {"connection_id": conn_id, "integration": integration, "flow": flow, "script": script}


def _walk_keys(obj):
    """Yield every dict key anywhere in *obj*, recursively -- list/detail
    responses nest families/members/sites, so a top-level-only key check
    would miss a leak two levels down."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


class TestListScriptFamilies:
    async def test_requires_permission(self, client):
        r = await client.get("/api/v1/celigo/scripts/families")
        assert r.status_code in (401, 403)

    async def test_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)
        r = await client.get("/api/v1/celigo/scripts/families", headers=headers)
        assert r.status_code == 403

    async def test_empty_totals_and_list_when_no_connection(self, client, admin_user):
        _, headers = admin_user
        r = await client.get("/api/v1/celigo/scripts/families", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["families"] == []
        assert body["synced_at"] is None
        assert body["totals"] == {
            "scripts": 0,
            "families": 0,
            "attached_families": 0,
            "unattached_families": 0,
            "diverged_families": 0,
            "sites": 0,
            "flows_with_sites": 0,
            "flows_total": 0,
            "integrations_with_sites": 0,
            "sites_with_open_errors": 0,
        }

    async def test_lists_the_seeded_families_family(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        r = await client.get("/api/v1/celigo/scripts/families", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["totals"]["scripts"] == 1
        assert body["totals"]["families"] == 1
        assert len(body["families"]) == 1
        family = body["families"][0]
        assert family["dedup_key"] == world["script"].celigo_id
        assert family["name"] == world["script"].name
        assert family["copies_count"] == 1
        assert family["kind"] == "transform", "the seeded attachment's site_type is 'transform'"
        assert family["original_present"] is True

    async def test_list_never_contains_script_content_or_content_hash(self, client, admin_user, db):
        user, headers = admin_user
        await _seed_world(db, user.tenant_id)

        r = await client.get("/api/v1/celigo/scripts/families", headers=headers)
        assert r.status_code == 200, r.text
        keys = set(_walk_keys(r.json()))
        assert "content" not in keys
        assert "content_hash" not in keys

    async def test_route_is_not_swallowed_by_scripts_script_id(self, client, admin_user, db):
        """`/scripts/families` must resolve to the families list, not to
        `/scripts/{script_id}` trying (and failing) to parse "families" as a
        UUID -- the families route must be registered first."""
        user, headers = admin_user
        await _seed_world(db, user.tenant_id)

        r = await client.get("/api/v1/celigo/scripts/families", headers=headers)
        assert r.status_code == 200, r.text
        assert "families" in r.json()
        assert "totals" in r.json()


class TestGetScriptFamily:
    async def test_requires_permission(self, client):
        r = await client.get(f"/api/v1/celigo/scripts/families/{uuid.uuid4().hex}")
        assert r.status_code in (401, 403)

    async def test_403_when_flag_disabled(self, client, admin_user, db):
        user, headers = admin_user
        await enable_feature_flag(db, user.tenant_id, "celigo", enabled=False)
        r = await client.get(f"/api/v1/celigo/scripts/families/{uuid.uuid4().hex}", headers=headers)
        assert r.status_code == 403

    async def test_404_when_no_connection(self, client, admin_user):
        _, headers = admin_user
        r = await client.get("/api/v1/celigo/scripts/families/whatever", headers=headers)
        assert r.status_code == 404
        assert r.json()["detail"] == "Script family not found"

    async def test_404_for_unknown_dedup_key(self, client, admin_user, db):
        user, headers = admin_user
        await _seed_world(db, user.tenant_id)
        r = await client.get("/api/v1/celigo/scripts/families/does-not-exist", headers=headers)
        assert r.status_code == 404
        assert r.json()["detail"] == "Script family not found"

    async def test_detail_has_content_per_member(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        r = await client.get(f"/api/v1/celigo/scripts/families/{world['script'].celigo_id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["members"]) == 1
        member = body["members"][0]
        assert member["script_id"] == str(world["script"].id)
        assert member["celigo_id"] == world["script"].celigo_id
        assert member["content"] == world["script"].content
        assert member["is_original"] is True
        assert body["summary"]["dedup_key"] == world["script"].celigo_id
        assert len(body["sites"]) == 1
        assert body["sites"][0]["flow_id"] == str(world["flow"].id)

    async def test_tenant_isolation(self, client, admin_user, admin_user_b, db):
        """Review finding (brief item 5c): the previous version seeded two
        DIFFERENT dedup_keys (via `_seed_world`'s own random per-call
        suffix) for tenant A and tenant B -- so even a query missing a
        tenant filter entirely would only ever find ONE tenant's row for
        that dedup_key, and the test could not have caught a PARTIAL-filter
        bug (e.g. one sub-query scoped by tenant_id, another not). Seeding
        the SAME celigo_id/dedup_key in both tenants means a leak in ANY of
        the family queries has the other tenant's row sitting right there to
        leak into the response."""
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b
        await enable_feature_flag(db, user_b.tenant_id, "celigo")

        shared_dedup_key = f"shared_{uuid.uuid4().hex[:8]}"
        world_a = await _seed_family_with_fixed_celigo_id(
            db, user_a.tenant_id, celigo_id=shared_dedup_key, name="Tenant A Script", content="tenant a body"
        )
        await _seed_family_with_fixed_celigo_id(
            db, user_b.tenant_id, celigo_id=shared_dedup_key, name="Tenant B Script", content="tenant b body"
        )
        await db.flush()

        list_resp = await client.get("/api/v1/celigo/scripts/families", headers=headers_a)
        assert list_resp.status_code == 200, list_resp.text
        families = list_resp.json()["families"]
        assert len(families) == 1, "tenant A's list must not contain tenant B's identically-keyed family"
        assert families[0]["dedup_key"] == shared_dedup_key
        assert families[0]["name"] == "Tenant A Script"
        assert families[0]["copies_count"] == 1
        assert families[0]["sites_count"] == 1
        assert families[0]["other_families_with_name"] == 0, "tenant B's identically-named family must not count"

        detail_resp = await client.get(f"/api/v1/celigo/scripts/families/{shared_dedup_key}", headers=headers_a)
        assert detail_resp.status_code == 200, detail_resp.text
        detail = detail_resp.json()
        assert detail["summary"]["name"] == "Tenant A Script"
        assert len(detail["members"]) == 1
        assert detail["members"][0]["content"] == "tenant a body"
        assert len(detail["sites"]) == 1
        assert detail["sites"][0]["flow_id"] == str(world_a["flow"].id)

        # Symmetrically, tenant B must see only its own -- not tenant A's.
        detail_resp_b = await client.get(f"/api/v1/celigo/scripts/families/{shared_dedup_key}", headers=headers_b)
        assert detail_resp_b.status_code == 200, detail_resp_b.text
        detail_b = detail_resp_b.json()
        assert detail_b["summary"]["name"] == "Tenant B Script"
        assert len(detail_b["members"]) == 1
        assert detail_b["members"][0]["content"] == "tenant b body"
