"""Scheduled Jobs platform — API (Slice 2, Task 4). Spec §B5 (binding):

    GET /schedules, GET /schedules/{id}, POST /schedules (compiles; 201
    pending_approval + plan, or 409 + clarification), PATCH /schedules/{id}
    (instruction -> recompile into pending_plan_json + diff; direct field
    edits apply immediately), POST .../approve (pending -> approved,
    version+1), POST .../run ({use_pending} -> enqueues one run now), POST
    .../pause, POST .../resume, DELETE, GET .../runs (from `jobs`). MCP
    `schedule_ops.execute_run` implemented (no longer a stub).

`POST /schedules` and `PATCH /schedules/{id}` monkeypatch
`app.api.v1.schedules.compile_instruction` directly (patched where it is
USED, not where it is defined — this repo's established pattern, see
test_report_refresh's ctx-spy tests) with a canned `CompiledPlan` or
`Clarification`, so these tests never make a real LLM call and stay
deterministic/network-free — `tests/jobs/test_compiler.py` already covers
`compile_instruction` itself end-to-end with a FakeAdapter.

`POST /schedules/{id}/run` monkeypatches a `fake.read` step onto the SAME
`STEP_REGISTRY` dict object the executor looks up at run time (the technique
`tests/jobs/test_executor.py` already established) so a real `jobs` row is
produced with no BigQuery/Drive/WeasyPrint credentials involved.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.models.pipeline import Schedule
from app.models.tenant import Tenant
from app.services.jobs.compiler import Clarification, CompiledPlan
from app.services.jobs.registry import STEP_REGISTRY, StepSpec
from tests.conftest import create_test_tenant, create_test_user, make_auth_headers

_INVENTORY_AGING_PLAN = {
    "steps": [
        {"id": "q1", "type": "bigquery_sql", "params": {"query": "SELECT 1"}},
        {
            "id": "compose",
            "type": "report.compose",
            "params": {"playbook_key": "inventory_aging", "params": {}},
        },
        {"id": "pdf", "type": "report.render_pdf", "params": {"report_step": "compose"}},
        {"id": "xlsx", "type": "report.build_xlsx", "params": {"report_step": "compose"}},
        {
            "id": "upload",
            "type": "drive.upload",
            "params": {"report_step": "compose", "period_key": "2026-09-07"},
        },
    ]
}


def _compiled_plan(plan_json: dict = _INVENTORY_AGING_PLAN) -> CompiledPlan:
    return CompiledPlan(
        plan_json=plan_json,
        summary_line="5 steps · BigQuery SQL query → Compose report → Render PDF → Build Excel workbook → Upload to Google Drive",
        kinds={"read", "write"},
        model="claude-test-model",
    )


def _fake_read_spec(executor, step_type: str = "fake.read") -> StepSpec:
    return StepSpec(
        type=step_type, label="Fake step (test)", kind="read", params_schema={"type": "object"}, executor=executor
    )


async def _seed_job_schedule(
    db: AsyncSession,
    tenant: Tenant,
    *,
    plan_json: dict | None = None,
    plan_status: str = "approved",
    plan_version: int = 1,
    pending_plan_json: dict | None = None,
    cron_expression: str = "0 6 * * 1",
    tz: str = "UTC",
    name: str = "Inventory Aging Weekly (test)",
) -> Schedule:
    schedule = Schedule(
        tenant_id=tenant.id,
        name=name,
        schedule_type="job",
        cron_expression=cron_expression,
        timezone=tz,
        is_active=True,
        instruction="every Monday, deliver the inventory aging report",
        plan_json=plan_json,
        plan_version=plan_version,
        plan_status=plan_status,
        pending_plan_json=pending_plan_json,
    )
    db.add(schedule)
    await db.flush()
    return schedule


# ---------------------------------------------------------------------------
# POST /schedules — compile path
# ---------------------------------------------------------------------------


class TestScheduleCompileCreate:
    async def test_create_job_returns_201_pending_approval_with_plan(
        self, client: AsyncClient, admin_user, monkeypatch
    ):
        user, headers = admin_user

        async def fake_compile(db, *, tenant_id, instruction, actor_id, llm=None, plan_version=None):
            assert tenant_id == user.tenant_id
            assert instruction == "every Monday, deliver the inventory aging report to Drive"
            return _compiled_plan()

        monkeypatch.setattr("app.api.v1.schedules.compile_instruction", fake_compile)

        resp = await client.post(
            "/api/v1/schedules",
            json={"instruction": "every Monday, deliver the inventory aging report to Drive"},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["plan_status"] == "pending_approval"
        assert data["plan_version"] == 0
        assert data["schedule_type"] == "job"
        assert data["instruction"] == "every Monday, deliver the inventory aging report to Drive"
        assert set(data["kinds"]) == {"read", "write"}
        assert data["summary_line"].startswith("5 steps ·")
        # No name given -> derived from the instruction, not left blank.
        assert data["name"]

    async def test_create_job_clarification_returns_409_and_creates_nothing(
        self, client: AsyncClient, admin_user, db: AsyncSession, monkeypatch
    ):
        user, headers = admin_user

        async def fake_compile(db, *, tenant_id, instruction, actor_id, llm=None, plan_version=None):
            return Clarification(question="Which subsidiary should this cover?")

        monkeypatch.setattr("app.api.v1.schedules.compile_instruction", fake_compile)

        resp = await client.post(
            "/api/v1/schedules",
            json={"instruction": "deliver the inventory aging report weekly"},
            headers=headers,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["clarification"] == "Which subsidiary should this cover?"

        rows = (
            (
                await db.execute(
                    select(Schedule).where(Schedule.tenant_id == user.tenant_id, Schedule.schedule_type == "job")
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    async def test_create_job_with_explicit_name_keeps_it(self, client: AsyncClient, admin_user, monkeypatch):
        user, headers = admin_user
        monkeypatch.setattr("app.api.v1.schedules.compile_instruction", lambda *a, **k: _async_return(_compiled_plan()))

        resp = await client.post(
            "/api/v1/schedules",
            json={"name": "Inventory Aging Weekly", "instruction": "weekly inventory aging report"},
            headers=headers,
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "Inventory Aging Weekly"

    async def test_create_legacy_schedule_without_instruction_still_works(self, client: AsyncClient, admin_user):
        """Pre-Slice-2 shape (schedule_type in sync|report|recon) is unchanged."""
        user, headers = admin_user
        resp = await client.post(
            "/api/v1/schedules",
            json={"name": "Daily Stripe Sync", "schedule_type": "sync", "cron_expression": "0 0 * * *"},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["schedule_type"] == "sync"
        assert data["plan_status"] is None

    async def test_create_without_instruction_or_schedule_type_is_422(self, client: AsyncClient, admin_user):
        user, headers = admin_user
        resp = await client.post("/api/v1/schedules", json={"name": "Nothing useful"}, headers=headers)
        assert resp.status_code == 422

    async def test_create_job_over_quota_returns_403(self, client: AsyncClient, db: AsyncSession, monkeypatch):
        tenant = await create_test_tenant(db, name="Job Quota Trial", plan="free")
        user, _ = await create_test_user(db, tenant, role_name="admin")
        await db.commit()
        headers = make_auth_headers(user)

        for i in range(5):
            resp = await client.post(
                "/api/v1/schedules",
                json={"name": f"Sched {i}", "schedule_type": "sync"},
                headers=headers,
            )
            assert resp.status_code == 201

        monkeypatch.setattr("app.api.v1.schedules.compile_instruction", lambda *a, **k: _async_return(_compiled_plan()))
        resp = await client.post(
            "/api/v1/schedules",
            json={"instruction": "over the limit"},
            headers=headers,
        )
        assert resp.status_code == 403


async def _async_return(value):
    return value


# ---------------------------------------------------------------------------
# GET /schedules — list (spec §B6: list-level `has_pending_plan` so the list
# page's "Needs attention" tile / row indicator can see an approved schedule
# sitting on an unapproved recompiled change — that state lives only in
# `pending_plan_json`, which the list-level `ScheduleResponse` otherwise never
# exposes; see the review finding this covers.)
# ---------------------------------------------------------------------------


class TestScheduleList:
    async def test_list_flags_has_pending_plan_true_for_approved_schedule_with_pending_change(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        new_plan = {"steps": [{"id": "q1", "type": "bigquery_sql", "params": {"query": "SELECT 2"}}]}
        await _seed_job_schedule(
            db,
            tenant,
            plan_json=_INVENTORY_AGING_PLAN,
            plan_status="approved",
            pending_plan_json=new_plan,
        )
        await db.commit()

        resp = await client.get("/api/v1/schedules", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["has_pending_plan"] is True

    async def test_list_flags_has_pending_plan_false_with_no_pending_change(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        await _seed_job_schedule(db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved")
        await db.commit()

        resp = await client.get("/api/v1/schedules", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["has_pending_plan"] is False


# ---------------------------------------------------------------------------
# GET /schedules/{id} — detail
# ---------------------------------------------------------------------------


class TestScheduleDetail:
    async def test_get_schedule_detail_full_shape(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(db, tenant, plan_json=_INVENTORY_AGING_PLAN)
        await db.commit()

        resp = await client.get(f"/api/v1/schedules/{schedule.id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_json"] == _INVENTORY_AGING_PLAN
        assert data["pending_plan_json"] is None
        assert data["pending_plan_diff"] == []
        assert data["instruction"]

    async def test_get_nonexistent_schedule_is_404(self, client: AsyncClient, admin_user):
        user, headers = admin_user
        resp = await client.get(f"/api/v1/schedules/{uuid.uuid4()}", headers=headers)
        assert resp.status_code == 404

    async def test_tenant_b_cannot_see_tenant_a_job_detail(
        self, client: AsyncClient, admin_user, admin_user_b, db: AsyncSession
    ):
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b
        tenant_a = (await db.execute(select(Tenant).where(Tenant.id == user_a.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(db, tenant_a, plan_json=_INVENTORY_AGING_PLAN)
        await db.commit()

        resp = await client.get(f"/api/v1/schedules/{schedule.id}", headers=headers_b)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /schedules/{id} — instruction recompile + direct edits
# ---------------------------------------------------------------------------


class TestScheduleUpdate:
    async def test_patch_instruction_on_approved_schedule_fills_pending_plan_and_diff(
        self, client: AsyncClient, admin_user, db: AsyncSession, monkeypatch
    ):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved")
        await db.commit()

        new_plan = {
            "steps": [
                {"id": "q1", "type": "bigquery_sql", "params": {"query": "SELECT 2"}},
                *_INVENTORY_AGING_PLAN["steps"][1:],
            ]
        }

        async def fake_compile(db, *, tenant_id, instruction, actor_id, llm=None, plan_version=None):
            assert plan_version == 1
            return _compiled_plan(new_plan)

        monkeypatch.setattr("app.api.v1.schedules.compile_instruction", fake_compile)

        resp = await client.patch(
            f"/api/v1/schedules/{schedule.id}",
            json={"instruction": "and Virtual"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_status"] == "approved"  # unchanged — still what actually runs
        assert data["plan_json"] == _INVENTORY_AGING_PLAN  # untouched until approved
        assert data["pending_plan_json"] == new_plan
        assert len(data["pending_plan_diff"]) > 0

    async def test_patch_instruction_on_never_approved_schedule_replaces_plan_directly(
        self, client: AsyncClient, admin_user, db: AsyncSession, monkeypatch
    ):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="pending_approval", plan_version=0
        )
        await db.commit()

        monkeypatch.setattr("app.api.v1.schedules.compile_instruction", lambda *a, **k: _async_return(_compiled_plan()))

        resp = await client.patch(
            f"/api/v1/schedules/{schedule.id}",
            json={"instruction": "weekly inventory aging, revised"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_status"] == "pending_approval"
        assert data["pending_plan_json"] is None
        assert data["plan_json"] == _INVENTORY_AGING_PLAN

    async def test_patch_instruction_clarification_returns_409(
        self, client: AsyncClient, admin_user, db: AsyncSession, monkeypatch
    ):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved")
        await db.commit()

        async def fake_compile(db, *, tenant_id, instruction, actor_id, llm=None, plan_version=None):
            return Clarification(question="Which subsidiary?")

        monkeypatch.setattr("app.api.v1.schedules.compile_instruction", fake_compile)

        resp = await client.patch(
            f"/api/v1/schedules/{schedule.id}", json={"instruction": "add a subsidiary"}, headers=headers
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["clarification"] == "Which subsidiary?"

    async def test_patch_direct_fields_apply_immediately_without_touching_plan(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved")
        await db.commit()

        resp = await client.patch(
            f"/api/v1/schedules/{schedule.id}",
            json={"timezone": "America/Los_Angeles", "catch_up": "skip", "budget": {"usd": 5.0}},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["timezone"] == "America/Los_Angeles"
        assert data["catch_up"] == "skip"
        assert data["budget_json"] == {"usd": 5.0}
        assert data["plan_json"] == _INVENTORY_AGING_PLAN
        assert data["pending_plan_json"] is None

    async def test_patch_discard_pending_clears_pending_plan_without_touching_live_plan(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        """Task 6 (frontend detail page) wires the pending-change panel's
        "Discard" button to `PATCH {discard_pending: true}` (spec §B5/§B6):
        the recompiled `pending_plan_json` a person doesn't want is dropped,
        the live `plan_json` (still what actually runs) is untouched."""
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        new_plan = {"steps": [{"id": "q1", "type": "bigquery_sql", "params": {"query": "SELECT 2"}}]}
        schedule = await _seed_job_schedule(
            db,
            tenant,
            plan_json=_INVENTORY_AGING_PLAN,
            pending_plan_json=new_plan,
            plan_status="approved",
            plan_version=1,
        )
        await db.commit()

        resp = await client.patch(
            f"/api/v1/schedules/{schedule.id}",
            json={"discard_pending": True},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending_plan_json"] is None
        assert data["pending_plan_diff"] == []
        assert data["plan_json"] == _INVENTORY_AGING_PLAN
        assert data["plan_status"] == "approved"

    async def test_patch_discard_pending_false_is_a_noop(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        new_plan = {"steps": [{"id": "q1", "type": "bigquery_sql", "params": {"query": "SELECT 2"}}]}
        schedule = await _seed_job_schedule(
            db, tenant, plan_json=_INVENTORY_AGING_PLAN, pending_plan_json=new_plan, plan_status="approved"
        )
        await db.commit()

        resp = await client.patch(
            f"/api/v1/schedules/{schedule.id}",
            json={"discard_pending": False},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["pending_plan_json"] == new_plan


# ---------------------------------------------------------------------------
# POST /schedules/{id}/approve
# ---------------------------------------------------------------------------


class TestScheduleApprove:
    async def test_approve_first_time_bumps_version_and_sets_next_run(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="pending_approval", plan_version=0
        )
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/approve", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_status"] == "approved"
        assert data["plan_version"] == 1
        assert data["next_run_at"] is not None

    async def test_approve_promotes_pending_plan_and_clears_it(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        new_plan = {"steps": [{"id": "q1", "type": "bigquery_sql", "params": {"query": "SELECT 2"}}]}
        schedule = await _seed_job_schedule(
            db,
            tenant,
            plan_json=_INVENTORY_AGING_PLAN,
            pending_plan_json=new_plan,
            plan_status="approved",
            plan_version=1,
        )
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/approve", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_version"] == 2
        assert data["plan_json"] == new_plan
        assert data["pending_plan_json"] is None

    async def test_approve_with_nothing_pending_is_400(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved", plan_version=1
        )
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/approve", headers=headers)
        assert resp.status_code == 400

    async def test_readonly_cannot_approve(self, client: AsyncClient, readonly_user, admin_user, db: AsyncSession):
        admin, admin_headers = admin_user
        ro_user, ro_headers = readonly_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == admin.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="pending_approval", plan_version=0
        )
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/approve", headers=ro_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /schedules/{id}/run
# ---------------------------------------------------------------------------


class TestScheduleRun:
    async def test_run_now_executes_the_approved_plan_and_returns_the_jobs_id(
        self, client: AsyncClient, admin_user, db: AsyncSession, monkeypatch
    ):
        calls = []

        async def fake_exec(ctx, params):
            calls.append(params)
            return {"ok": True}

        monkeypatch.setitem(STEP_REGISTRY, "fake.read", _fake_read_spec(fake_exec))

        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db, tenant, plan_json={"steps": [{"id": "s1", "type": "fake.read", "params": {}}]}, plan_status="approved"
        )
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/run", json={"use_pending": False}, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["reason"] == "done"
        assert data["jobs_id"]
        assert len(calls) == 1

        job_row = (await db.execute(select(Job).where(Job.id == uuid.UUID(data["jobs_id"])))).scalar_one()
        assert job_row.tenant_id == user.tenant_id
        assert job_row.job_type == "scheduled_job"
        assert job_row.result_summary["reason"] == "done"

    async def test_run_now_with_no_compiled_plan_is_409(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(db, tenant, plan_json=None, plan_status="draft", plan_version=0)
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/run", json={"use_pending": False}, headers=headers)
        assert resp.status_code == 409

    async def test_run_now_rejects_a_never_approved_plan_is_409(
        self, client: AsyncClient, admin_user, db: AsyncSession, monkeypatch
    ):
        """HITL gate (review finding): `plan_json` non-empty is not the same
        as a human having approved it. A schedule fresh off `POST /schedules`
        sits at `plan_status == "pending_approval"` with a real compiled
        `plan_json` already on it — `run` with `use_pending=False` must
        refuse to execute that plan's steps until `/approve` has run, even
        though the plan is non-empty."""
        calls = []

        async def fake_exec(ctx, params):
            calls.append(params)
            return {"ok": True}

        monkeypatch.setitem(STEP_REGISTRY, "fake.read", _fake_read_spec(fake_exec))

        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db,
            tenant,
            plan_json={"steps": [{"id": "s1", "type": "fake.read", "params": {}}]},
            plan_status="pending_approval",
            plan_version=0,
        )
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/run", json={"use_pending": False}, headers=headers)
        assert resp.status_code == 409
        assert calls == []

        job_count = await db.execute(select(Job).where(Job.tenant_id == user.tenant_id))
        assert job_count.scalars().all() == []

    async def test_run_now_use_pending_runs_the_pending_plan(
        self, client: AsyncClient, admin_user, db: AsyncSession, monkeypatch
    ):
        async def fake_exec(ctx, params):
            return {"ok": True}

        monkeypatch.setitem(STEP_REGISTRY, "fake.read", _fake_read_spec(fake_exec))

        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        pending = {"steps": [{"id": "s1", "type": "fake.read", "params": {}}]}
        schedule = await _seed_job_schedule(
            db, tenant, plan_json=None, pending_plan_json=pending, plan_status="pending_approval", plan_version=0
        )
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/run", json={"use_pending": True}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["reason"] == "done"

    async def test_readonly_cannot_run(self, client: AsyncClient, readonly_user, admin_user, db: AsyncSession):
        admin, admin_headers = admin_user
        ro_user, ro_headers = readonly_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == admin.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db, tenant, plan_json={"steps": [{"id": "s1", "type": "fake.read", "params": {}}]}, plan_status="approved"
        )
        await db.commit()

        resp = await client.post(
            f"/api/v1/schedules/{schedule.id}/run", json={"use_pending": False}, headers=ro_headers
        )
        assert resp.status_code == 403

    async def test_tenant_b_cannot_run_tenant_a_schedule(
        self, client: AsyncClient, admin_user, admin_user_b, db: AsyncSession
    ):
        user_a, headers_a = admin_user
        user_b, headers_b = admin_user_b
        tenant_a = (await db.execute(select(Tenant).where(Tenant.id == user_a.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db, tenant_a, plan_json={"steps": [{"id": "s1", "type": "fake.read", "params": {}}]}, plan_status="approved"
        )
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/run", json={"use_pending": False}, headers=headers_b)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /schedules/{id}/pause, /resume
# ---------------------------------------------------------------------------


class TestSchedulePauseResume:
    async def test_pause_sets_paused_at_and_reason(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved")
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/pause", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["paused_at"] is not None
        assert data["pause_reason"]

    async def test_resume_clears_pause_and_recomputes_next_run(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved")
        schedule.paused_at = datetime.now(timezone.utc) - timedelta(days=3)
        schedule.pause_reason = "paused after 2 failed attempts: boom"
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/resume", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["paused_at"] is None
        assert data["pause_reason"] is None
        assert data["next_run_at"] is not None
        assert datetime.fromisoformat(data["next_run_at"]) > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# GET /schedules/{id}/runs
# ---------------------------------------------------------------------------


class TestScheduleRunsList:
    async def test_runs_list_from_jobs(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved")

        job = Job(
            tenant_id=user.tenant_id,
            job_type="scheduled_job",
            status="completed",
            correlation_id="corr-1",
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            parameters={"schedule_id": str(schedule.id), "plan_version": 1, "period_key": "2026-09-07", "attempt": 1},
            result_summary={"reason": "done", "outputs": {"q1": {"ok": True}}},
        )
        db.add(job)

        other_job = Job(
            tenant_id=user.tenant_id,
            job_type="scheduled_job",
            status="completed",
            parameters={"schedule_id": str(uuid.uuid4())},
            result_summary={"reason": "done"},
        )
        db.add(other_job)
        await db.commit()

        resp = await client.get(f"/api/v1/schedules/{schedule.id}/runs", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == str(job.id)
        assert data[0]["reason"] == "done"
        assert data[0]["correlation_id"] == "corr-1"

    async def test_runs_list_for_nonexistent_schedule_is_404(self, client: AsyncClient, admin_user):
        user, headers = admin_user
        resp = await client.get(f"/api/v1/schedules/{uuid.uuid4()}/runs", headers=headers)
        assert resp.status_code == 404
