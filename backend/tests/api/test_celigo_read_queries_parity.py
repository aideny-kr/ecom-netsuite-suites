"""Task 1 -- parity tests for the extraction of celigo_flows.py's route
aggregations into `app/services/celigo/read_queries.py`.

ACCEPTANCE TEST for the refactor: `read_queries.py` must return the SAME
numbers the routes return today. For each of the five moved functions this
file seeds the shared fixture world (`_seed_world` / `_seed_router_chain_flow`
/ `_seed_cron_flow`, imported from `test_celigo_flows_api.py` rather than
duplicated -- a second copy of the world-builder is exactly the drift this
task exists to prevent), calls the LIVE route over HTTP, then separately
calls the new `read_queries` function and maps its dataclass through the
route's own Out-model mapping helper, and asserts the two JSON bodies are
byte-identical (`json.dumps(sort_keys=True)`).

RED RUN (recorded 2026-09-04, before `read_queries.py` existed):
    $ .venv/bin/python -m pytest tests/api/test_celigo_read_queries_parity.py -q
    ModuleNotFoundError: No module named 'app.services.celigo.read_queries'
This file imports `read_queries` at module scope on purpose so that failure
mode is a collection-time error, not a buried assertion -- exactly the
"prove the test fails against the broken code" standard.

`test_celigo_flows_api.py` (66 tests, untouched) stays the parity oracle for
BEHAVIOR; this file is the oracle for the SHAPE of the extraction.
"""

from __future__ import annotations

import ast
import json
import uuid

import pytest

from app.api.v1 import celigo_flows
from app.services.celigo import read_queries
from tests.api.test_celigo_flows_api import (
    _seed_cron_flow,
    _seed_router_chain_flow,
    _seed_sandbox_world,
    _seed_world,
)
from tests.conftest import enable_feature_flag


@pytest.fixture(autouse=True)
async def _celigo_flag_enabled(db, admin_user):
    user, _ = admin_user
    await enable_feature_flag(db, user.tenant_id, "celigo")


def _canon(obj) -> str:
    """`sort_keys` JSON, matching the brief's "byte-equal" acceptance test.
    `default=str` only guards against a stray non-JSON-native value (e.g. a
    `uuid.UUID` that slipped through un-stringified) turning into a hard
    TypeError instead of a loud diff -- neither route JSON nor a correctly
    mapped dataclass should ever need it."""
    return json.dumps(obj, sort_keys=True, default=str)


class TestIntegrationSummariesParity:
    async def test_matches_list_integrations_route(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        await _seed_router_chain_flow(db, world)
        paused = await _seed_cron_flow(db, world, name="Paused one")
        paused.disabled = True
        await _seed_sandbox_world(db, world)
        await db.flush()

        route_resp = await client.get("/api/v1/celigo/integrations", headers=headers)
        assert route_resp.status_code == 200, route_resp.text

        summaries = await read_queries.integration_summaries(db, tenant_id=user.tenant_id)
        rebuilt = [celigo_flows._integration_summary_out(s).model_dump(mode="json") for s in summaries]

        assert _canon(rebuilt) == _canon(route_resp.json())

    async def test_matches_route_when_no_connection(self, client, admin_user, db):
        user, headers = admin_user
        route_resp = await client.get("/api/v1/celigo/integrations", headers=headers)
        assert route_resp.status_code == 200, route_resp.text

        summaries = await read_queries.integration_summaries(db, tenant_id=user.tenant_id)
        rebuilt = [celigo_flows._integration_summary_out(s).model_dump(mode="json") for s in summaries]

        assert _canon(rebuilt) == _canon(route_resp.json()) == "[]"


class TestSyncStatusParity:
    async def test_matches_sync_status_route(self, client, admin_user, db):
        user, headers = admin_user
        await _seed_world(db, user.tenant_id)

        route_resp = await client.get("/api/v1/celigo/sync-status", headers=headers)
        assert route_resp.status_code == 200, route_resp.text

        status = await read_queries.sync_status(db, tenant_id=user.tenant_id)
        rebuilt = celigo_flows._sync_status_out(status).model_dump(mode="json")

        assert _canon(rebuilt) == _canon(route_resp.json())


class TestFlowSummariesParity:
    async def test_matches_list_integration_flows_route(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        await _seed_router_chain_flow(db, world)
        await _seed_cron_flow(db, world)

        route_resp = await client.get(f"/api/v1/celigo/integrations/{world['integration'].id}/flows", headers=headers)
        assert route_resp.status_code == 200, route_resp.text

        summaries = await read_queries.flow_summaries(
            db, tenant_id=user.tenant_id, integration_id=world["integration"].id
        )
        rebuilt = [celigo_flows._flow_summary_out(s).model_dump(mode="json") for s in summaries]

        assert _canon(rebuilt) == _canon(route_resp.json())


class TestFlowDetailParity:
    async def test_matches_get_flow_detail_route(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        chain = await _seed_router_chain_flow(db, world)

        route_resp = await client.get(f"/api/v1/celigo/flows/{chain['flow'].id}", headers=headers)
        assert route_resp.status_code == 200, route_resp.text

        detail = await read_queries.flow_detail(db, tenant_id=user.tenant_id, flow_id=chain["flow"].id)
        assert detail is not None
        rebuilt = celigo_flows._flow_detail_out(detail).model_dump(mode="json")

        assert _canon(rebuilt) == _canon(route_resp.json())

    async def test_returns_none_for_missing_flow(self, db, admin_user):
        user, _ = admin_user
        assert await read_queries.flow_detail(db, tenant_id=user.tenant_id, flow_id=uuid.uuid4()) is None


class TestFlowErrorGroupsParity:
    async def test_matches_list_flow_errors_route(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)

        route_resp = await client.get(f"/api/v1/celigo/flows/{world['flow'].id}/errors", headers=headers)
        assert route_resp.status_code == 200, route_resp.text

        groups = await read_queries.flow_error_groups(
            db, tenant_id=user.tenant_id, flow_id=world["flow"].id, status="open"
        )
        # 100 mirrors the route's own `Query(100, ...)` default -- the route
        # wasn't asked for a non-default `limit`, so this must match it.
        rebuilt = celigo_flows._flow_errors_out(groups, limit=100).model_dump(mode="json")

        assert _canon(rebuilt) == _canon(route_resp.json())

    async def test_matches_route_for_resolved_status(self, client, admin_user, db):
        user, headers = admin_user
        world = await _seed_world(db, user.tenant_id)
        world["error"].resolved_at = world["error"].occurred_at
        await db.flush()

        route_resp = await client.get(
            f"/api/v1/celigo/flows/{world['flow'].id}/errors", params={"status": "resolved"}, headers=headers
        )
        assert route_resp.status_code == 200, route_resp.text

        groups = await read_queries.flow_error_groups(
            db, tenant_id=user.tenant_id, flow_id=world["flow"].id, status="resolved"
        )
        rebuilt = celigo_flows._flow_errors_out(groups, limit=100).model_dump(mode="json")

        assert _canon(rebuilt) == _canon(route_resp.json())


class TestNoScriptContentSelected:
    """The N2 shape rule (spec §1/§4/§10): the future chat tools read through
    this same module, so a script body must never become a SELECTed column
    here -- enforced by shape, not by a guarded parameter. Filtering ON
    `content_hash` (the divergence check moved verbatim from
    `list_integration_flows`) is fine and expected; what must never appear is
    `CeligoScript.content` / `CeligoScript.content_hash` as one of a
    `select(...)` call's own projected columns."""

    def test_no_select_projects_script_content_or_hash(self):
        import inspect

        source = inspect.getsource(read_queries)
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "select"):
                continue
            for arg in node.args:
                if (
                    isinstance(arg, ast.Attribute)
                    and arg.attr in ("content", "content_hash")
                    and isinstance(arg.value, ast.Name)
                    and arg.value.id == "CeligoScript"
                ):
                    offenders.append(f"select(...) at line {node.lineno} projects CeligoScript.{arg.attr}")
        assert offenders == [], offenders
