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

`POST /schedules/{id}/run` now enqueues via Celery instead of executing the
plan inline (Task 5 residual — see the endpoint's own docstring): these tests
fake `app.api.v1.schedules.celery_app.send_task` (`_capture_send_task` below,
this repo's established pattern — see `tests/workers/test_report_auto_
refresh.py`'s identical `monkeypatch.setattr(mod.celery_app, "send_task",
...)`) rather than monkeypatching `STEP_REGISTRY`, since the request itself
never runs a step any more — only the Celery task does, and that task's own
`existing_job_id` reuse behaviour is covered end-to-end in
`tests/jobs/test_executor.py`, not here.
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
from app.services import schedule_service
from app.services.jobs.compiler import Clarification, CompiledPlan
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
            "params": {"report_step": "compose"},
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

    async def test_create_schedule_type_job_without_instruction_is_422(
        self, client: AsyncClient, admin_user
    ):
        """Item 4 (gate fix): `schedule_type="job"` with no `instruction`
        used to fall through to the legacy direct-create path — a Scheduled
        Job with no instruction is meaningless (nothing to compile)."""
        user, headers = admin_user
        resp = await client.post(
            "/api/v1/schedules",
            json={"name": "A job with no instruction", "schedule_type": "job"},
            headers=headers,
        )
        assert resp.status_code == 422
        assert "a Scheduled Job needs an instruction" in str(resp.json())

    async def test_create_legacy_schedule_without_name_is_422_not_500(
        self, client: AsyncClient, admin_user
    ):
        """Item 4 (gate fix): `name` lost its `min_length=1` when it became
        optional (for the compile path, where a name is derived from the
        instruction) — the legacy branch (`instruction` absent) must not be
        able to reach `schedules.name NOT NULL` with `None`."""
        user, headers = admin_user
        resp = await client.post(
            "/api/v1/schedules",
            json={"schedule_type": "sync", "cron_expression": "0 0 * * *"},
            headers=headers,
        )
        assert resp.status_code == 422

    async def test_create_legacy_schedule_with_empty_name_is_422(
        self, client: AsyncClient, admin_user
    ):
        user, headers = admin_user
        resp = await client.post(
            "/api/v1/schedules",
            json={"name": "   ", "schedule_type": "sync"},
            headers=headers,
        )
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
# Cron/timezone validated where they are written (gate fix, item 1): the
# executor already pauses a schedule whose cron/timezone cannot be computed
# (`_claim_due_schedules`), but an operator writing a bad value at
# create/update time must see a 422 immediately, not silently get a paused
# schedule at the next sweep tick.
# ---------------------------------------------------------------------------


class TestScheduleCronTimezoneValidation:
    async def test_create_invalid_cron_expression_is_422(self, client: AsyncClient, admin_user):
        user, headers = admin_user
        # Matches ScheduleCreate's existing character-set pattern (digits/
        # spaces only) but is not a real cron — proves this is croniter
        # validation, not just the pre-existing regex.
        resp = await client.post(
            "/api/v1/schedules",
            json={"name": "Bad Cron", "schedule_type": "sync", "cron_expression": "60 24 32 13 8"},
            headers=headers,
        )
        assert resp.status_code == 422
        assert "cron_expression" in str(resp.json())
        assert "60 24 32 13 8" in str(resp.json())

    async def test_create_invalid_timezone_is_422(self, client: AsyncClient, admin_user):
        user, headers = admin_user
        resp = await client.post(
            "/api/v1/schedules",
            json={"name": "Bad TZ", "schedule_type": "sync", "timezone": "Mars/Olympus"},
            headers=headers,
        )
        assert resp.status_code == 422
        assert "timezone" in str(resp.json())
        assert "Mars/Olympus" in str(resp.json())

    async def test_update_invalid_cron_expression_is_422(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved")
        await db.commit()

        resp = await client.patch(
            f"/api/v1/schedules/{schedule.id}",
            json={"cron_expression": "60 24 32 13 8"},
            headers=headers,
        )
        assert resp.status_code == 422
        assert "cron_expression" in str(resp.json())

    async def test_update_invalid_timezone_is_422(self, client: AsyncClient, admin_user, db: AsyncSession):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved")
        await db.commit()

        resp = await client.patch(
            f"/api/v1/schedules/{schedule.id}",
            json={"timezone": "Mars/Olympus"},
            headers=headers,
        )
        assert resp.status_code == 422
        assert "timezone" in str(resp.json())

    async def test_create_with_valid_cron_and_timezone_still_succeeds(
        self, client: AsyncClient, admin_user
    ):
        user, headers = admin_user
        resp = await client.post(
            "/api/v1/schedules",
            json={
                "name": "Good Cron",
                "schedule_type": "sync",
                "cron_expression": "0 6 * * 1",
                "timezone": "America/Los_Angeles",
            },
            headers=headers,
        )
        assert resp.status_code == 201


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
        assert len(data["schedules"]) == 1
        assert data["schedules"][0]["has_pending_plan"] is True

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
        assert data["schedules"][0]["has_pending_plan"] is False

    async def test_list_returns_tenant_wide_7day_totals_and_per_row_run_stats_owner_and_created_via(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        """Task 5 residual (spec §B6): the list response carries the page's
        tenant-wide "Last 7 days" tile totals — ONE aggregate query, not N+1
        — plus each row's own last_run_duration_seconds/runs_last_7_days/
        owner_name/created_via."""
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        now = datetime.now(timezone.utc)

        schedule_a = await _seed_job_schedule(
            db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved", name="Inventory Aging Weekly"
        )
        schedule_b = await _seed_job_schedule(
            db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved", name="Stripe payout reconciliation"
        )
        schedule_a.owner_id = user.id
        schedule_a.created_via = "chat"
        schedule_b.created_via = "page"
        await db.flush()

        # schedule_a: most recent completed run — 1m52s, 2 minutes ago (within 7d).
        recent_started = now - timedelta(minutes=2)
        db.add(
            Job(
                tenant_id=user.tenant_id,
                job_type="scheduled_job",
                status="completed",
                started_at=recent_started,
                completed_at=recent_started + timedelta(minutes=1, seconds=52),
                parameters={"schedule_id": str(schedule_a.id)},
                result_summary={"reason": "done"},
            )
        )
        # schedule_a: an OLDER completed run, also within 7d — must not win the duration.
        older_started = now - timedelta(days=1)
        db.add(
            Job(
                tenant_id=user.tenant_id,
                job_type="scheduled_job",
                status="completed",
                started_at=older_started,
                completed_at=older_started + timedelta(minutes=10),
                parameters={"schedule_id": str(schedule_a.id)},
                result_summary={"reason": "done"},
            )
        )
        # schedule_a: a run OUTSIDE the 7-day window — must not be counted either place.
        stale_started = now - timedelta(days=10)
        db.add(
            Job(
                tenant_id=user.tenant_id,
                job_type="scheduled_job",
                status="completed",
                started_at=stale_started,
                completed_at=stale_started + timedelta(minutes=1),
                parameters={"schedule_id": str(schedule_a.id)},
                result_summary={"reason": "done"},
            )
        )
        # schedule_b: one FAILED run within 7 days.
        failed_started = now - timedelta(hours=1)
        db.add(
            Job(
                tenant_id=user.tenant_id,
                job_type="scheduled_job",
                status="failed",
                started_at=failed_started,
                completed_at=failed_started + timedelta(minutes=5),
                parameters={"schedule_id": str(schedule_b.id)},
                result_summary={"reason": "error"},
            )
        )
        await db.commit()

        resp = await client.get("/api/v1/schedules", headers=headers)
        assert resp.status_code == 200
        data = resp.json()

        # Tenant-wide tile: 3 runs in the trailing 7 days (2 for a + 1 for b); 1 failed.
        assert data["runs_last_7_days_total"] == 3
        assert data["runs_last_7_days_failed"] == 1

        rows = {r["name"]: r for r in data["schedules"]}
        row_a = rows["Inventory Aging Weekly"]
        row_b = rows["Stripe payout reconciliation"]

        assert row_a["runs_last_7_days"] == 2
        assert row_a["last_run_duration_seconds"] == 112.0  # 1m52s — the MOST RECENT run, not the older one
        assert row_a["owner_name"] == user.full_name
        assert row_a["created_via"] == "chat"

        assert row_b["runs_last_7_days"] == 1
        assert row_b["owner_name"] is None
        assert row_b["created_via"] == "page"


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

    async def test_patch_cron_on_approved_schedule_recomputes_next_run_at(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        """Item 2 (gate fix): `approve`/`resume` both recompute `next_run_at`
        when their preconditions hold — a bare `PATCH` changing the cron must
        mirror that, or the schedule keeps firing at the STALE (weekly) time
        until the next approve/resume happens to touch it."""
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db,
            tenant,
            plan_json=_INVENTORY_AGING_PLAN,
            plan_status="approved",
            cron_expression="0 6 * * 1",  # weekly, Monday 06:00
            tz="UTC",
        )
        await db.commit()

        resp = await client.patch(
            f"/api/v1/schedules/{schedule.id}",
            json={"cron_expression": "0 6 * * *"},  # daily 06:00
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["next_run_at"] is not None

        expected_next = schedule_service.compute_next_run("0 6 * * *", "UTC", datetime.now(timezone.utc))
        actual_next = datetime.fromisoformat(data["next_run_at"].replace("Z", "+00:00"))
        assert abs((actual_next - expected_next).total_seconds()) < 5

    async def test_patch_timezone_on_approved_schedule_recomputes_next_run_at(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db,
            tenant,
            plan_json=_INVENTORY_AGING_PLAN,
            plan_status="approved",
            cron_expression="0 6 * * *",
            tz="UTC",
        )
        await db.commit()

        resp = await client.patch(
            f"/api/v1/schedules/{schedule.id}",
            json={"timezone": "America/Los_Angeles"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        expected_next = schedule_service.compute_next_run(
            "0 6 * * *", "America/Los_Angeles", datetime.now(timezone.utc)
        )
        actual_next = datetime.fromisoformat(data["next_run_at"].replace("Z", "+00:00"))
        assert abs((actual_next - expected_next).total_seconds()) < 5

    async def test_patch_cron_on_unapproved_schedule_does_not_set_next_run_at(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        """No approved plan yet -> nothing should be scheduled to fire."""
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db,
            tenant,
            plan_json=_INVENTORY_AGING_PLAN,
            plan_status="pending_approval",
            cron_expression="0 6 * * 1",
            tz="UTC",
        )
        await db.commit()

        resp = await client.patch(
            f"/api/v1/schedules/{schedule.id}",
            json={"cron_expression": "0 6 * * *"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["next_run_at"] is None

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

    async def test_patch_instruction_on_legacy_schedule_is_409(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        """Item 3 (gate fix): a job-only edit must not act on a pre-Slice-2
        `sync|report|recon` row — there is no `plan_json`/compiler pipeline
        on that row type at all."""
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        legacy = Schedule(
            tenant_id=tenant.id,
            name="Legacy Sync",
            schedule_type="sync",
            cron_expression="0 0 * * *",
            is_active=True,
        )
        db.add(legacy)
        await db.commit()

        resp = await client.patch(
            f"/api/v1/schedules/{legacy.id}",
            json={"instruction": "do something"},
            headers=headers,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "not a scheduled job"

    async def test_patch_discard_pending_on_legacy_schedule_is_409(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        legacy = Schedule(
            tenant_id=tenant.id,
            name="Legacy Sync",
            schedule_type="sync",
            cron_expression="0 0 * * *",
            is_active=True,
        )
        db.add(legacy)
        await db.commit()

        resp = await client.patch(
            f"/api/v1/schedules/{legacy.id}",
            json={"discard_pending": True},
            headers=headers,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "not a scheduled job"

    async def test_patch_cron_and_name_on_legacy_schedule_still_allowed(
        self, client: AsyncClient, admin_user, db: AsyncSession
    ):
        """Legacy-style direct field edits are unaffected — only job-only
        edits (instruction, discard_pending) are refused."""
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        legacy = Schedule(
            tenant_id=tenant.id,
            name="Legacy Sync",
            schedule_type="sync",
            cron_expression="0 0 * * *",
            is_active=True,
        )
        db.add(legacy)
        await db.commit()

        resp = await client.patch(
            f"/api/v1/schedules/{legacy.id}",
            json={"name": "Legacy Sync Renamed", "cron_expression": "0 1 * * *"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Legacy Sync Renamed"
        assert data["cron_expression"] == "0 1 * * *"

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

    async def test_approve_on_legacy_schedule_is_409(self, client: AsyncClient, admin_user, db: AsyncSession):
        """Item 3 (gate fix): approve is a job-only concept (plan_status /
        plan_json don't mean anything on a legacy row)."""
        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        legacy = Schedule(
            tenant_id=tenant.id,
            name="Legacy Sync",
            schedule_type="sync",
            cron_expression="0 0 * * *",
            is_active=True,
        )
        db.add(legacy)
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{legacy.id}/approve", headers=headers)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "not a scheduled job"

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


def _capture_send_task(monkeypatch) -> list[tuple[str, dict]]:
    """Fakes `celery_app.send_task` at the point `schedules.py` calls it —
    this repo's established convention (`tests/workers/test_report_auto_
    refresh.py`'s `monkeypatch.setattr(mod.celery_app, "send_task", ...)`) —
    so `/run` never actually touches a broker; returns the list of
    `(task_name, kwargs)` calls captured."""

    class _FakeResult:
        id = "fake-celery-task-id"

    sent: list[tuple[str, dict]] = []

    def fake_send_task(name, kwargs=None, **_kw):
        sent.append((name, kwargs or {}))
        return _FakeResult()

    monkeypatch.setattr("app.api.v1.schedules.celery_app.send_task", fake_send_task)
    return sent


class TestScheduleRun:
    async def test_run_now_enqueues_and_returns_202_with_the_jobs_id(
        self, client: AsyncClient, admin_user, db: AsyncSession, monkeypatch
    ):
        """Task 5 residual: `/run` now enqueues via Celery instead of running
        the plan inline on the request — a real Inventory Aging run can take
        minutes, which used to hold the request open long enough for nginx to
        cut it. The endpoint itself never runs a step; it creates the `jobs`
        row (so the `202` response carries a real id immediately) and
        dispatches `tasks.scheduled_jobs_run_now` with that row's id."""
        sent = _capture_send_task(monkeypatch)

        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db, tenant, plan_json={"steps": [{"id": "s1", "type": "fake.read", "params": {}}]}, plan_status="approved"
        )
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/run", json={"use_pending": False}, headers=headers)
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "queued"
        assert data["reason"] is None
        assert data["jobs_id"]

        job_row = (await db.execute(select(Job).where(Job.id == uuid.UUID(data["jobs_id"])))).scalar_one()
        assert job_row.tenant_id == user.tenant_id
        assert job_row.job_type == "scheduled_job"
        assert job_row.status == "pending"  # not executed by this request — the task hasn't run

        assert len(sent) == 1
        task_name, kwargs = sent[0]
        assert task_name == "tasks.scheduled_jobs_run_now"
        assert kwargs["schedule_id"] == str(schedule.id)
        assert kwargs["tenant_id"] == str(user.tenant_id)
        assert kwargs["use_pending"] is False
        assert kwargs["actor_id"] == str(user.id)
        assert kwargs["job_id"] == data["jobs_id"]

    async def test_run_now_snapshots_the_plan_it_saw_onto_the_pre_created_jobs_row(
        self, client: AsyncClient, admin_user, db: AsyncSession, monkeypatch
    ):
        """review finding, MAJOR: an instruction edit or discard landing
        between enqueue and the Celery task's execution used to silently
        change what runs, because `run_schedule_now` re-read
        `row.plan_json`/`row.pending_plan_json` live when the task actually
        executed. The endpoint now snapshots the plan it validated onto the
        pre-created jobs row's own `parameters["plan"]`."""
        _capture_send_task(monkeypatch)

        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db, tenant, plan_json=_INVENTORY_AGING_PLAN, plan_status="approved", plan_version=1
        )
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/run", json={"use_pending": False}, headers=headers)
        assert resp.status_code == 202

        job_row = (await db.execute(select(Job).where(Job.id == uuid.UUID(resp.json()["jobs_id"])))).scalar_one()
        assert job_row.parameters["plan"] == _INVENTORY_AGING_PLAN

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
        sent = _capture_send_task(monkeypatch)

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
        assert sent == []  # blocked before a jobs row was created or a task enqueued

        job_count = await db.execute(select(Job).where(Job.tenant_id == user.tenant_id))
        assert job_count.scalars().all() == []

    async def test_run_now_use_pending_enqueues_with_the_pending_flag_and_next_plan_version(
        self, client: AsyncClient, admin_user, db: AsyncSession, monkeypatch
    ):
        sent = _capture_send_task(monkeypatch)

        user, headers = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        pending = {"steps": [{"id": "s1", "type": "fake.read", "params": {}}]}
        schedule = await _seed_job_schedule(
            db, tenant, plan_json=None, pending_plan_json=pending, plan_status="pending_approval", plan_version=0
        )
        await db.commit()

        resp = await client.post(f"/api/v1/schedules/{schedule.id}/run", json={"use_pending": True}, headers=headers)
        assert resp.status_code == 202
        assert resp.json()["status"] == "queued"

        job_row = (await db.execute(select(Job).where(Job.id == uuid.UUID(resp.json()["jobs_id"])))).scalar_one()
        # plan_version + 1 -- the version pending_plan_json WOULD become on approval
        # (matches run_schedule_now's own use_pending convention, spec §B4).
        assert job_row.parameters["plan_version"] == 1

        assert len(sent) == 1
        assert sent[0][1]["use_pending"] is True

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
