"""Task 3 -- the four `celigo.*` chat tools (`mcp/tools/celigo_flow_map.py`).

Mirrors `tests/test_data_sample.py`'s shape (invalid arg / no db / real rows),
widened for this family's honesty contract (spec
`docs/superpowers/specs/2026-09-04-celigo-chat-access.md` §8): every envelope
carries `{"columns", "rows", "row_count", "query", "truncated", "caveats"}`,
always every key, and gating (flag / connection / never-synced) each returns
an EMPTY envelope with exactly one explanatory caveat rather than an error.

Seeding reuses the exact helpers `tests/api/test_celigo_flows_api.py` already
built and tested for the read routes (`_make_connection`, `_seed_world`,
`_seed_cron_flow`, `_seed_router_chain_flow`, `_seed_sandbox_world`) --
building a second copy of that fixture data here would be the two-copies-of-
one-fact anti-pattern this repo's own instructions warn against.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp import governance, registry
from app.mcp.tools import celigo_flow_map
from app.models.celigo import CeligoScript, CeligoScriptAttachment
from app.models.pipeline import CursorState
from app.services.chat.nodes import ALLOWED_CHAT_TOOLS
from app.services.chat.tool_categories import categorize, is_celigo_source
from tests.api.test_celigo_flows_api import (
    _make_connection,
    _seed_cron_flow,
    _seed_router_chain_flow,
    _seed_sandbox_world,
    _seed_world,
)
from tests.conftest import create_test_tenant, enable_feature_flag

_CELIGO_TOOL_NAMES = ("celigo.integrations", "celigo.flows", "celigo.flow_steps", "celigo.flow_errors")

_INTEGRATIONS_COLUMNS = [
    "integration",
    "flows",
    "scheduled",
    "on_demand",
    "paused",
    "open_errors",
    "root_causes",
    "errors_checked",
    "last_run",
    "modified_in_celigo",
]
_FLOWS_COLUMNS = [
    "integration",
    "flow",
    "state",
    "schedule",
    "timezone",
    "last_run",
    "missed_runs",
    "steps",
    "routers",
    "branches",
    "lookups",
    "open_errors",
    "root_causes",
    "errors_checked",
]
_FLOW_STEPS_COLUMNS = [
    "sequence",
    "kind",
    "name",
    "adaptor",
    "branch",
    "operation",
    "record_type",
    "open_errors",
    "scripts",
    "script_sites",
]
_FLOW_ERRORS_COLUMNS = [
    "flow",
    "source",
    "code",
    "occurrences",
    "first_seen",
    "last_seen",
    "sample_message",
    "trace_keys",
    "steps",
    "purge_at",
]

_TOOLS = [
    (celigo_flow_map.execute_integrations, {}, _INTEGRATIONS_COLUMNS),
    (celigo_flow_map.execute_flows, {}, _FLOWS_COLUMNS),
    (celigo_flow_map.execute_flow_steps, {"flow": "does-not-matter"}, _FLOW_STEPS_COLUMNS),
    (celigo_flow_map.execute_flow_errors, {}, _FLOW_ERRORS_COLUMNS),
]

_ENVELOPE_KEYS = {"columns", "rows", "row_count", "query", "truncated", "caveats"}


async def _sync_now(db: AsyncSession, connection_id, *, last_synced_at: datetime | None = None) -> datetime:
    ts = last_synced_at or datetime.now(timezone.utc)
    db.add(
        CursorState(
            connection_id=connection_id,
            object_type="celigo_flow_map",
            cursor_value=ts.isoformat(),
            last_synced_at=ts,
        )
    )
    await db.flush()
    return ts


def _assert_envelope_shape(result: dict) -> None:
    assert set(result.keys()) == _ENVELOPE_KEYS, result.keys()
    assert isinstance(result["columns"], list)
    assert isinstance(result["rows"], list)
    assert result["row_count"] == len(result["rows"])
    assert isinstance(result["query"], str)
    assert isinstance(result["truncated"], bool)
    assert isinstance(result["caveats"], list)


# ---------------------------------------------------------------------------
# Param validation -- ValueError is the ONLY raise.
# ---------------------------------------------------------------------------


class TestParamValidation:
    async def test_unknown_param_raises(self):
        with pytest.raises(ValueError, match="Unknown parameter"):
            await celigo_flow_map.execute_flows({"bogus": 1})

    async def test_bad_limit_type_raises(self):
        with pytest.raises(ValueError, match="limit"):
            await celigo_flow_map.execute_flows({"limit": "fifty"})

    async def test_limit_out_of_range_raises(self):
        with pytest.raises(ValueError, match="between 1 and 200"):
            await celigo_flow_map.execute_flows({"limit": 500})

    async def test_flow_errors_limit_out_of_range_raises(self):
        with pytest.raises(ValueError, match="between 1 and 50"):
            await celigo_flow_map.execute_flow_errors({"limit": 100})

    async def test_bad_status_raises(self):
        with pytest.raises(ValueError, match="status"):
            await celigo_flow_map.execute_flow_errors({"status": "pending"})

    async def test_flow_steps_requires_flow(self):
        with pytest.raises(ValueError, match="flow is required"):
            await celigo_flow_map.execute_flow_steps({})

    async def test_integrations_takes_no_params(self):
        with pytest.raises(ValueError, match="Unknown parameter"):
            await celigo_flow_map.execute_integrations({"anything": 1})

    async def test_bad_bool_type_raises(self):
        with pytest.raises(ValueError, match="only_open_errors"):
            await celigo_flow_map.execute_flows({"only_open_errors": "yes"})


# ---------------------------------------------------------------------------
# Gating -- no db / flag off / no connection / never synced. Every tool.
# ---------------------------------------------------------------------------


class TestGating:
    @pytest.mark.parametrize("execute_fn,params,columns", _TOOLS)
    async def test_no_db_returns_empty_envelope(self, execute_fn, params, columns):
        result = await execute_fn(params)
        _assert_envelope_shape(result)
        assert result["columns"] == columns
        assert result["rows"] == []
        assert result["caveats"] == ["No database session — nothing was read."]

    @pytest.mark.parametrize("execute_fn,params,columns", _TOOLS)
    async def test_flag_off_returns_empty_envelope(self, execute_fn, params, columns, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, slug=f"celigo-flagoff-{uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        await _sync_now(db, conn_id)

        async def _disabled(*_args, **_kwargs):
            return False

        monkeypatch.setattr(celigo_flow_map.feature_flag_service, "is_enabled", _disabled)

        result = await execute_fn(params, context={"db": db, "tenant_id": str(tenant.id)})
        _assert_envelope_shape(result)
        assert result["rows"] == []
        assert result["caveats"] == ["Celigo is turned off for this workspace."]

    async def test_flag_gate_is_real_rows_come_back_when_flag_enabled(self, db: AsyncSession):
        """The counterpart to the flag-off test above: proves the gate is not
        a no-op that always empties the envelope regardless of the flag --
        with the flag ON (and a synced connection, no data yet) the tool
        reaches the query layer instead of short-circuiting on the flag
        caveat. (This is the executable form of "prove the flag test goes
        red before you add the gate" -- run against the pre-gate code, this
        assertion is what a flag-off call would ALSO satisfy; the flag-off
        test above is what tells the two apart.)"""
        tenant = await create_test_tenant(db, slug=f"celigo-flagon-{uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        await _sync_now(db, conn_id)
        await enable_feature_flag(db, tenant.id, "celigo")

        result = await celigo_flow_map.execute_integrations({}, context={"db": db, "tenant_id": str(tenant.id)})
        assert result["caveats"] != ["Celigo is turned off for this workspace."]

    @pytest.mark.parametrize("execute_fn,params,columns", _TOOLS)
    async def test_no_connection_returns_empty_envelope(self, execute_fn, params, columns, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-noconn-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")

        result = await execute_fn(params, context={"db": db, "tenant_id": str(tenant.id)})
        _assert_envelope_shape(result)
        assert result["rows"] == []
        assert result["caveats"] == ["This workspace has no Celigo connection."]

    @pytest.mark.parametrize("execute_fn,params,columns", _TOOLS)
    async def test_never_synced_returns_empty_even_with_rows(self, execute_fn, params, columns, db: AsyncSession):
        """No `cursor_states` row at all -- rows exist in the tables (a full
        `_seed_world`), but the tool must report empty + the never-synced
        caveat regardless."""
        tenant = await create_test_tenant(db, slug=f"celigo-nosync-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        await _seed_world(db, tenant.id)

        result = await execute_fn(params, context={"db": db, "tenant_id": str(tenant.id)})
        _assert_envelope_shape(result)
        assert result["rows"] == []
        assert result["caveats"] == ["This workspace has never completed a Celigo sync."]

    async def test_internal_exception_returns_generic_empty_envelope(self, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, slug=f"celigo-boom-{uuid.uuid4().hex[:6]}")
        conn_id = await _make_connection(db, tenant.id)
        await _sync_now(db, conn_id)
        await enable_feature_flag(db, tenant.id, "celigo")

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("the mirror is on fire")

        monkeypatch.setattr(celigo_flow_map.read_queries, "integration_summaries", _boom)

        result = await celigo_flow_map.execute_integrations({}, context={"db": db, "tenant_id": str(tenant.id)})
        _assert_envelope_shape(result)
        assert result["caveats"] == ["Celigo flow map could not be read."]


# ---------------------------------------------------------------------------
# Seeded tenant -- real rows, filters, resolvers, ambiguity, truncation.
# ---------------------------------------------------------------------------


class TestSeededTenant:
    async def test_integrations_returns_real_row_and_sandbox_is_absent(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-integ-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        await _seed_sandbox_world(db, world)
        await _sync_now(db, world["connection_id"])

        result = await celigo_flow_map.execute_integrations({}, context={"db": db, "tenant_id": str(tenant.id)})
        _assert_envelope_shape(result)
        assert result["columns"] == _INTEGRATIONS_COLUMNS
        assert result["row_count"] == 1
        row = dict(zip(result["columns"], result["rows"][0]))
        assert row["integration"] == "ACME ERP"
        assert row["flows"] == 1
        assert row["open_errors"] == 1
        assert all("sandbox" not in str(v).lower() for v in row.values())
        assert result["caveats"][0].startswith("Snapshot of production flows as of")

    async def test_errors_checked_at_null_vs_verified(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-checked-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        await _sync_now(db, world["connection_id"])

        # NULL by default (`_seed_world` never sets it).
        result = await celigo_flow_map.execute_flows({}, context={"db": db, "tenant_id": str(tenant.id)})
        row = dict(zip(result["columns"], result["rows"][0]))
        assert row["errors_checked"] == "not fully checked"
        assert any("not fully checked" in c for c in result["caveats"])

        checked_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        world["flow"].errors_checked_at = checked_at
        await db.flush()

        result = await celigo_flow_map.execute_flows({}, context={"db": db, "tenant_id": str(tenant.id)})
        row = dict(zip(result["columns"], result["rows"][0]))
        assert row["errors_checked"] == f"verified {checked_at.isoformat()}"
        assert not any("not fully checked" in c for c in result["caveats"])

    async def test_integration_resolves_by_uuid_and_by_name_fragment(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-resolve-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        await _sync_now(db, world["connection_id"])

        by_id = await celigo_flow_map.execute_flows(
            {"integration": str(world["integration"].id)}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        by_fragment = await celigo_flow_map.execute_flows(
            {"integration": "acme"}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        assert by_id["row_count"] == 1
        assert by_fragment["row_count"] == 1
        assert by_id["rows"] == by_fragment["rows"]

    async def test_unknown_integration_key_is_empty_with_caveat(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-noint-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        await _sync_now(db, world["connection_id"])

        result = await celigo_flow_map.execute_flows(
            {"integration": "no-such-integration"}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        assert result["rows"] == []
        assert any("No Celigo integration matches" in c for c in result["caveats"])

    async def test_ambiguous_integration_fragment_caveats_candidates(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-ambig-int-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        from app.models.celigo import CeligoIntegration

        second = CeligoIntegration(
            tenant_id=tenant.id,
            celigo_connection_id=world["connection_id"],
            celigo_id=f"int2_{world['suffix']}",
            name="ACME Retail",
            sandbox=False,
            raw_json={},
        )
        db.add(second)
        await db.flush()
        await _sync_now(db, world["connection_id"])

        result = await celigo_flow_map.execute_flows(
            {"integration": "acme"}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        assert result["rows"] == []
        caveat = next(c for c in result["caveats"] if "matches" in c)
        assert "ACME ERP" in caveat and "ACME Retail" in caveat

    async def test_only_open_errors_and_only_stalled_filters(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-filters-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)  # has 1 open error, schedule kind="unknown"
        cron_flow = await _seed_cron_flow(db, world)  # valid 6h cron, no run yet -> "no_run", 0 errors
        as_of = await _sync_now(db, world["connection_id"])

        only_errors = await celigo_flow_map.execute_flows(
            {"only_open_errors": True}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        assert only_errors["row_count"] == 1
        assert dict(zip(only_errors["columns"], only_errors["rows"][0]))["flow"] == world["flow"].name

        # Force the cron flow stalled: last run far enough in the past that
        # its own 6h interval is exceeded relative to the sync cursor above.
        cron_flow.last_executed_at = as_of - timedelta(hours=20)
        await db.flush()

        only_stalled = await celigo_flow_map.execute_flows(
            {"only_stalled": True}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        assert only_stalled["row_count"] == 1
        row = dict(zip(only_stalled["columns"], only_stalled["rows"][0]))
        assert row["flow"] == cron_flow.name
        assert row["state"] == "stalled"
        assert row["missed_runs"] is not None and row["missed_runs"] >= 2

    async def test_limit_truncates_and_says_so(self, db: AsyncSession):
        from app.models.celigo import CeligoFlow

        tenant = await create_test_tenant(db, slug=f"celigo-limit-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        # _seed_cron_flow derives celigo_id from world["suffix"] alone, so a
        # second call against the SAME world collides on the identity unique
        # constraint -- build the extra flows directly instead.
        for i in range(3):
            db.add(
                CeligoFlow(
                    tenant_id=tenant.id,
                    celigo_connection_id=world["connection_id"],
                    integration_id=world["integration"].id,
                    celigo_id=f"flow_extra_{i}_{world['suffix']}",
                    name=f"Extra Flow {i}",
                    disabled=False,
                    schedule="? 0 */6 * * *",
                    raw_json={},
                )
            )
        await db.flush()
        await _sync_now(db, world["connection_id"])

        result = await celigo_flow_map.execute_flows({"limit": 2}, context={"db": db, "tenant_id": str(tenant.id)})
        assert result["row_count"] == 2
        assert result["truncated"] is True
        assert "Showing 2 of 4." in result["query"]

    async def test_flow_steps_returns_sequenced_rows_with_router(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-steps-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        chain = await _seed_router_chain_flow(db, world)
        await _sync_now(db, world["connection_id"])

        result = await celigo_flow_map.execute_flow_steps(
            {"flow": str(chain["flow"].id)}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        _assert_envelope_shape(result)
        assert result["columns"] == _FLOW_STEPS_COLUMNS
        kinds = [row[1] for row in result["rows"]]
        assert "router" in kinds
        assert "source" in kinds
        # sequence is strictly increasing and starts at 1
        sequences = [row[0] for row in result["rows"]]
        assert sequences == list(range(1, len(sequences) + 1))
        # the solo script hook attached to the lookup step shows up joined by name
        scripts_cell = [row[8] for row in result["rows"] if row[8]]
        assert any("sales_order_script_v2" in s for s in scripts_cell)

    async def test_flow_resolves_by_exact_name_and_by_uuid(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-flowres-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        await _sync_now(db, world["connection_id"])

        by_name = await celigo_flow_map.execute_flow_steps(
            {"flow": "sales order sync"}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        by_id = await celigo_flow_map.execute_flow_steps(
            {"flow": str(world["flow"].id)}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        assert by_name["rows"] == by_id["rows"]
        assert by_name["row_count"] >= 1

    async def test_ambiguous_flow_name_caveats_candidates_and_empty_rows(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-ambig-flow-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        await _seed_cron_flow(db, world, name="Sales Order Sync")  # exact name collision
        await _sync_now(db, world["connection_id"])

        result = await celigo_flow_map.execute_flow_steps(
            {"flow": "Sales Order Sync"}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        assert result["rows"] == []
        assert any("matches 2 flows" in c for c in result["caveats"])

        errors_result = await celigo_flow_map.execute_flow_errors(
            {"flow": "Sales Order Sync"}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        assert errors_result["rows"] == []
        assert any("matches 2 flows" in c for c in errors_result["caveats"])

    async def test_flow_errors_single_flow(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-ferr-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        await _sync_now(db, world["connection_id"])

        result = await celigo_flow_map.execute_flow_errors(
            {"flow": str(world["flow"].id)}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        _assert_envelope_shape(result)
        assert result["columns"] == _FLOW_ERRORS_COLUMNS
        assert result["row_count"] == 1
        row = dict(zip(result["columns"], result["rows"][0]))
        assert row["occurrences"] == 1
        assert len(row["sample_message"]) <= 300
        assert row["sample_message"] == world["signature"].sample_message

    async def test_flow_errors_tenant_wide_when_flow_omitted(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-ferr-all-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        await _seed_cron_flow(db, world)
        await _sync_now(db, world["connection_id"])

        result = await celigo_flow_map.execute_flow_errors({}, context={"db": db, "tenant_id": str(tenant.id)})
        assert result["row_count"] == 1  # only _seed_world's flow carries an error
        assert result["rows"][0][0] == world["flow"].name

    async def test_flow_errors_empty_for_checked_flow_states_verified(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-ferr-checked-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        await _sync_now(db, world["connection_id"])
        checked_at = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
        world["flow"].errors_checked_at = checked_at
        await db.flush()

        result = await celigo_flow_map.execute_flow_errors(
            {"flow": str(world["flow"].id), "status": "resolved"}, context={"db": db, "tenant_id": str(tenant.id)}
        )
        assert result["rows"] == []
        assert result["query"] == f"No resolved errors as of the snapshot; verified {checked_at.isoformat()}."

    async def test_second_tenant_sees_nothing(self, db: AsyncSession):
        tenant_a = await create_test_tenant(db, slug=f"celigo-iso-a-{uuid.uuid4().hex[:6]}")
        tenant_b = await create_test_tenant(db, slug=f"celigo-iso-b-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant_a.id, "celigo")
        await enable_feature_flag(db, tenant_b.id, "celigo")
        world_a = await _seed_world(db, tenant_a.id)
        await _sync_now(db, world_a["connection_id"])

        conn_b = await _make_connection(db, tenant_b.id)
        await _sync_now(db, conn_b)

        result_b = await celigo_flow_map.execute_integrations({}, context={"db": db, "tenant_id": str(tenant_b.id)})
        assert result_b["row_count"] == 0
        assert result_b["rows"] == []


# ---------------------------------------------------------------------------
# N2 shape test -- walked over the SERIALIZED JSON, not the code.
# ---------------------------------------------------------------------------


class TestN2Shape:
    async def test_no_script_content_and_no_oversized_cells_in_any_envelope(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-n2-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id, pii_message="x" * 500 + " customer@example.com")
        await _seed_router_chain_flow(db, world)
        await _sync_now(db, world["connection_id"])
        context = {"db": db, "tenant_id": str(tenant.id)}

        envelopes = [
            await celigo_flow_map.execute_integrations({}, context=context),
            await celigo_flow_map.execute_flows({}, context=context),
            await celigo_flow_map.execute_flow_steps({"flow": str(world["flow"].id)}, context=context),
            await celigo_flow_map.execute_flow_errors({}, context=context),
        ]

        for envelope in envelopes:
            blob = json.dumps(envelope, default=str)
            parsed = json.loads(blob)

            def _walk(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        assert key not in ("content", "content_hash"), f"leaked key: {key}"
                        _walk(value)
                elif isinstance(node, list):
                    for item in node:
                        _walk(item)
                elif isinstance(node, str):
                    assert len(node) <= 300, f"oversized string cell: {node[:50]}..."

            _walk(parsed)


class TestScriptCellCap:
    """Task 4G: `_join_scripts` joined every attachment on a step with "; " and
    NO cap, while every other text cell in this module is capped at 300 chars
    (`_cap_message`). A step with several script attachments could therefore
    produce a `scripts`/`script_sites` cell far past 300 chars -- exactly the
    oversized-cell condition `TestN2Shape`'s generic walk already guards
    against for every OTHER cell, so this fixture must make THAT walk fail
    before the cap exists."""

    async def test_many_script_attachments_on_one_step_stay_within_the_cap(self, db: AsyncSession):
        tenant = await create_test_tenant(db, slug=f"celigo-scriptcap-{uuid.uuid4().hex[:6]}")
        await enable_feature_flag(db, tenant.id, "celigo")
        world = await _seed_world(db, tenant.id)
        conn_id = world["connection_id"]
        step = world["step"]

        # world["step"] already carries one attachment (world["script"], "Transform
        # Script"). Add three more with long names on the SAME step -- four
        # attachments joined with "; " comfortably exceeds 300 chars unjoined.
        long_names = [
            "Pre-Save Page Validate Customer Business Entity Subsidiary Currency And Payment "
            "Terms Mapping Before The Record Is Committed Script",
            "Post-Save Page Synchronize The NetSuite Internal Id Back Onto The Celigo Custom "
            "Field For Downstream Lookups Script",
            "Input Filter Exclude Cancelled Refunded And Internal Test Orders From The "
            "Nightly Export Batch Before It Reaches NetSuite Script",
        ]
        for i, name in enumerate(long_names):
            script = CeligoScript(
                tenant_id=tenant.id,
                celigo_connection_id=conn_id,
                celigo_id=f"scr_extra_{i}_{world['suffix']}",
                name=name,
                content="function onSave(scriptContext) { return true; }",
            )
            db.add(script)
            await db.flush()
            db.add(
                CeligoScriptAttachment(
                    tenant_id=tenant.id,
                    celigo_connection_id=conn_id,
                    flow_id=step.flow_id,
                    flow_step_id=step.id,
                    script_id=script.id,
                    script_celigo_id=script.celigo_id,
                    function_name="onSave",
                    json_path=f"pageGenerators[0].extra_{i}.script",
                    site_type="hook",
                )
            )
        await db.flush()
        await _sync_now(db, conn_id)

        result = await celigo_flow_map.execute_flow_steps(
            {"flow": str(world["flow"].id)}, context={"db": db, "tenant_id": str(tenant.id)}
        )

        scripts_col = result["columns"].index("scripts")
        step_row = next(row for row in result["rows"] if row[scripts_col])
        scripts_cell = step_row[scripts_col]

        assert len(scripts_cell) <= 300, f"scripts cell not capped: {len(scripts_cell)} chars"
        assert "more" in scripts_cell, "an oversized joined cell must say how many attachments were dropped"


# ---------------------------------------------------------------------------
# Static wiring -- registry / governance / categories / allow-list.
# ---------------------------------------------------------------------------


class TestStaticWiring:
    def test_every_registry_entry_execute_lives_in_celigo_flow_map(self):
        for name in _CELIGO_TOOL_NAMES:
            entry = registry.TOOL_REGISTRY[name]
            assert entry["execute"].__module__ == "app.mcp.tools.celigo_flow_map"

    def test_every_registry_entry_has_a_governance_twin(self):
        for name in _CELIGO_TOOL_NAMES:
            assert name in governance.TOOL_CONFIGS

    def test_every_tool_is_in_the_chat_allowlist(self):
        for name in _CELIGO_TOOL_NAMES:
            assert name in ALLOWED_CHAT_TOOLS

    def test_categorize_both_spellings_is_data_table(self):
        for name in _CELIGO_TOOL_NAMES:
            sanitized = name.replace(".", "_")
            assert categorize(name) == "data_table"
            assert categorize(sanitized) == "data_table"

    def test_is_celigo_source(self):
        assert is_celigo_source("celigo_flows") is True
        assert is_celigo_source("celigo.flows") is True
        connector_id = "ab" * 16
        assert is_celigo_source(f"ext__{connector_id}__list_flows") is True
        assert is_celigo_source("netsuite_suiteql") is False

    def test_no_registry_entry_names_a_handler_outside_the_read_module(self):
        """Read-only by construction (spec §5): the mutation guard's HITL
        classifier only ever inspects `ext__` names, so the ONE thing that
        keeps this family safe to skip that path is that every handler
        really does live in the read-only module -- pinned again here,
        independent of the module-name check above, against the actual
        governance allowlist rather than a hardcoded name tuple."""
        for name, entry in registry.TOOL_REGISTRY.items():
            if name.startswith("celigo."):
                assert entry["execute"].__module__ == "app.mcp.tools.celigo_flow_map", name
