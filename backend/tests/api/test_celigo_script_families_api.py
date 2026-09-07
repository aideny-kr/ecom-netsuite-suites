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

from tests.api.test_celigo_flows_api import _seed_world
from tests.conftest import enable_feature_flag


@pytest.fixture(autouse=True)
async def _celigo_flag_enabled(db, admin_user):
    user, _ = admin_user
    await enable_feature_flag(db, user.tenant_id, "celigo")


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
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b
        await enable_feature_flag(db, user_b.tenant_id, "celigo")

        world_a = await _seed_world(db, user_a.tenant_id)
        await _seed_world(db, user_b.tenant_id)

        r = await client.get(f"/api/v1/celigo/scripts/families/{world_a['script'].celigo_id}", headers=headers_b)
        assert r.status_code == 404, "tenant B must not see tenant A's script family"
