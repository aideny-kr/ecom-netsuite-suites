"""MCP `schedule_ops` tool (Slice 2, Task 4). Spec §B5 (binding):

    MCP `schedule_ops.execute_create` -> the compile path. `execute_run`
    implemented (no longer a stub).

Same tool names, new behaviour — `app.mcp.registry.TOOL_REGISTRY` still
registers `schedule.create` -> `execute_create`, `schedule.list` ->
`execute_list`, `schedule.run` -> `execute_run` (see
`test_registry_still_wires_the_same_tool_names`); `tests/test_prompt_tool_sync.py`
covers the CI invariant that no tool name in the agent's prompt drifts from
this registry.

`execute_run` monkeypatches a `fake.read` step onto the SAME `STEP_REGISTRY`
dict object the executor looks up at run time — the technique
`tests/jobs/test_executor.py` already established — so this drives a REAL
`jobs` row through `run_schedule_now` with no BigQuery/Drive/WeasyPrint
credentials involved. `execute_create`'s compile path patches
`app.services.jobs.compiler.compile_instruction` (patched where it is
imported FROM at call time — `execute_create` does a local `from
app.services.jobs.compiler import ... compile_instruction` inside the
function body, so patching the module attribute is picked up on the next
call) with a canned `CompiledPlan`/`Clarification`, exactly like
`tests/api/test_schedules_api.py`'s API-level compile tests, so no real LLM
call happens here either.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.registry import TOOL_REGISTRY
from app.mcp.tools import schedule_ops
from app.models.pipeline import Schedule
from app.models.tenant import Tenant
from app.services.jobs.compiler import Clarification, CompiledPlan
from app.services.jobs.registry import STEP_REGISTRY, StepSpec


def _compiled_plan() -> CompiledPlan:
    return CompiledPlan(
        plan_json={"steps": [{"id": "q1", "type": "bigquery_sql", "params": {"query": "SELECT 1"}}]},
        summary_line="1 step · BigQuery SQL query",
        kinds={"read"},
        model="claude-test-model",
    )


def _fake_read_spec(executor, step_type: str = "fake.read") -> StepSpec:
    return StepSpec(
        type=step_type, label="Fake step (test)", kind="read", params_schema={"type": "object"}, executor=executor
    )


async def _seed_job_schedule(
    db: AsyncSession, tenant: Tenant, *, plan_json: dict, plan_status: str = "approved"
) -> Schedule:
    schedule = Schedule(
        tenant_id=tenant.id,
        name="Inventory Aging Weekly (test)",
        schedule_type="job",
        cron_expression="0 6 * * 1",
        timezone="UTC",
        is_active=True,
        instruction="every Monday, deliver the inventory aging report",
        plan_json=plan_json,
        plan_version=1,
        plan_status=plan_status,
    )
    db.add(schedule)
    await db.flush()
    return schedule


# ---------------------------------------------------------------------------
# Same tool names, new behaviour (spec §B5)
# ---------------------------------------------------------------------------


def test_registry_still_wires_the_same_tool_names():
    assert TOOL_REGISTRY["schedule.create"]["execute"] is schedule_ops.execute_create
    assert TOOL_REGISTRY["schedule.list"]["execute"] is schedule_ops.execute_list
    assert TOOL_REGISTRY["schedule.run"]["execute"] is schedule_ops.execute_run


# ---------------------------------------------------------------------------
# execute_run — no longer a stub
# ---------------------------------------------------------------------------


class TestExecuteRun:
    async def test_execute_run_actually_runs_the_plan_and_returns_a_real_jobs_id(
        self, db: AsyncSession, admin_user, monkeypatch
    ):
        calls = []

        async def fake_exec(ctx, params):
            calls.append(params)
            return {"ok": True}

        monkeypatch.setitem(STEP_REGISTRY, "fake.read", _fake_read_spec(fake_exec))

        user, _ = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db, tenant, plan_json={"steps": [{"id": "s1", "type": "fake.read", "params": {}}]}
        )
        await db.commit()

        result = await schedule_ops.execute_run(
            {"schedule_id": str(schedule.id)},
            context={"db": db, "tenant_id": str(user.tenant_id), "actor_id": str(user.id)},
        )
        assert not result.get("error")
        assert result["reason"] == "done"
        assert result["jobs_id"]
        assert len(calls) == 1
        # The old canned stub message must be gone.
        assert "Stub" not in str(result)

    async def test_execute_run_rejects_a_never_approved_plan(self, db: AsyncSession, admin_user, monkeypatch):
        """HITL gate (review finding): the chat agent must not be able to
        call `schedule.create` then `schedule.run` back-to-back and execute
        a compiled-but-unreviewed plan's steps. `use_pending=False` (the
        default) against a `plan_status == "pending_approval"` schedule —
        even with a real non-empty `plan_json` — must be refused."""
        calls = []

        async def fake_exec(ctx, params):
            calls.append(params)
            return {"ok": True}

        monkeypatch.setitem(STEP_REGISTRY, "fake.read", _fake_read_spec(fake_exec))

        user, _ = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db,
            tenant,
            plan_json={"steps": [{"id": "s1", "type": "fake.read", "params": {}}]},
            plan_status="pending_approval",
        )
        await db.commit()

        result = await schedule_ops.execute_run(
            {"schedule_id": str(schedule.id)},
            context={"db": db, "tenant_id": str(user.tenant_id), "actor_id": str(user.id)},
        )
        assert result["reason"] == "blocked"
        assert not result.get("jobs_id")
        assert calls == []

    async def test_execute_run_use_pending_runs_the_pending_plan(self, db: AsyncSession, admin_user, monkeypatch):
        async def fake_exec(ctx, params):
            return {"ok": True}

        monkeypatch.setitem(STEP_REGISTRY, "fake.read", _fake_read_spec(fake_exec))

        user, _ = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = Schedule(
            tenant_id=tenant.id,
            name="Pending job (test)",
            schedule_type="job",
            is_active=True,
            plan_json=None,
            pending_plan_json={"steps": [{"id": "s1", "type": "fake.read", "params": {}}]},
            plan_status="pending_approval",
            plan_version=0,
        )
        db.add(schedule)
        await db.commit()

        result = await schedule_ops.execute_run(
            {"schedule_id": str(schedule.id), "use_pending": True},
            context={"db": db, "tenant_id": str(user.tenant_id)},
        )
        assert result["reason"] == "done"

    async def test_execute_run_missing_schedule_id_returns_error_not_a_crash(self, db: AsyncSession, admin_user):
        user, _ = admin_user
        result = await schedule_ops.execute_run({}, context={"db": db, "tenant_id": str(user.tenant_id)})
        assert result.get("error") is True

    async def test_execute_run_unknown_schedule_returns_error_not_a_crash(self, db: AsyncSession, admin_user):
        user, _ = admin_user
        result = await schedule_ops.execute_run(
            {"schedule_id": str(uuid.uuid4())},
            context={"db": db, "tenant_id": str(user.tenant_id)},
        )
        assert result.get("error") is True

    async def test_execute_run_no_context_returns_error_not_a_crash(self):
        result = await schedule_ops.execute_run({"schedule_id": str(uuid.uuid4())}, context={})
        assert result.get("error") is True


# ---------------------------------------------------------------------------
# execute_create — the compile path
# ---------------------------------------------------------------------------


class TestExecuteCreate:
    async def test_execute_create_with_instruction_compiles_into_a_pending_job(
        self, db: AsyncSession, admin_user, monkeypatch
    ):
        user, _ = admin_user

        async def fake_compile(db, *, tenant_id, instruction, actor_id, llm=None, plan_version=None):
            assert tenant_id == user.tenant_id
            assert plan_version == 0
            return _compiled_plan()

        monkeypatch.setattr("app.services.schedule_service.compile_instruction", fake_compile)

        result = await schedule_ops.execute_create(
            {"instruction": "weekly inventory aging report"},
            context={"db": db, "tenant_id": str(user.tenant_id), "actor_id": str(user.id)},
        )
        assert not result.get("error")
        assert result["plan_status"] == "pending_approval"
        assert result["schedule_id"]
        # Item 5 (gate fix): create_scheduled_job returns only the persisted
        # Schedule row, not the transient CompiledPlan — summary_line is
        # recomputed from plan_json (same STEP_REGISTRY-derived rendering
        # app.api.v1.schedules._plan_summary_line uses for the API's own
        # response), not passed through from the compiler's own value.
        assert result["summary_line"] == "1 steps · BigQuery SQL query"

        row = (await db.execute(select(Schedule).where(Schedule.id == uuid.UUID(result["schedule_id"])))).scalar_one()
        assert row.schedule_type == "job"
        assert row.instruction == "weekly inventory aging report"
        assert row.plan_json == _compiled_plan().plan_json

    async def test_execute_create_with_instruction_and_clarification_creates_nothing(
        self, db: AsyncSession, admin_user, monkeypatch
    ):
        user, _ = admin_user

        async def fake_compile(db, *, tenant_id, instruction, actor_id, llm=None, plan_version=None):
            return Clarification(question="Which subsidiary?")

        monkeypatch.setattr("app.services.schedule_service.compile_instruction", fake_compile)

        result = await schedule_ops.execute_create(
            {"instruction": "deliver inventory aging weekly"},
            context={"db": db, "tenant_id": str(user.tenant_id)},
        )
        assert result.get("error") is True
        assert result["clarification"] is True
        assert result["message"] == "Which subsidiary?"

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

    async def test_execute_create_legacy_path_unchanged(self, db: AsyncSession, admin_user):
        """`instruction` absent -> the pre-Slice-2 direct-create behaviour."""
        user, _ = admin_user
        result = await schedule_ops.execute_create(
            {"name": "Legacy MCP Sync", "schedule_type": "sync", "cron": "0 0 * * *"},
            context={"db": db, "tenant_id": str(user.tenant_id)},
        )
        assert not result.get("error")
        assert result["name"] == "Legacy MCP Sync"
        assert result["schedule_type"] == "sync"
        assert result["cron_expression"] == "0 0 * * *"

    async def test_execute_create_legacy_path_missing_required_params_is_error(self, db: AsyncSession, admin_user):
        user, _ = admin_user
        result = await schedule_ops.execute_create({}, context={"db": db, "tenant_id": str(user.tenant_id)})
        assert result.get("error") is True

    async def test_execute_create_no_context_is_error_not_a_crash(self):
        result = await schedule_ops.execute_create({"name": "x", "schedule_type": "sync"}, context={})
        assert result.get("error") is True

    async def test_execute_create_with_a_10000_char_instruction_is_error_no_row(
        self, db: AsyncSession, admin_user, monkeypatch
    ):
        """Item 5 (gate fix): `execute_create`'s instruction branch used to
        bypass every `ScheduleCreate` validator (instruction max length
        included) by building the `Schedule` row directly — it must now run
        through the SAME `ScheduleCreate` model the API validates against.
        The compile call must never even happen: the length is rejected
        before `create_scheduled_job`'s compile step is reached."""
        user, _ = admin_user

        async def fail_if_called(*a, **k):
            raise AssertionError("compile_instruction must not be called for an over-length instruction")

        monkeypatch.setattr("app.services.schedule_service.compile_instruction", fail_if_called)

        result = await schedule_ops.execute_create(
            {"instruction": "x" * 10_000},
            context={"db": db, "tenant_id": str(user.tenant_id), "actor_id": str(user.id)},
        )
        assert result.get("error") is True

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

    async def test_execute_create_over_quota_is_error_no_row(
        self, db: AsyncSession, admin_user, monkeypatch
    ):
        """Item 5 (gate fix): `execute_create`'s instruction branch used to
        bypass the plan-quota entitlement check entirely."""
        user, _ = admin_user

        async def fake_check_entitlement(db, tenant_id, feature):
            assert feature == "schedules"
            return False

        monkeypatch.setattr("app.services.schedule_service.entitlement_service.check_entitlement", fake_check_entitlement)

        result = await schedule_ops.execute_create(
            {"instruction": "weekly inventory aging report"},
            context={"db": db, "tenant_id": str(user.tenant_id), "actor_id": str(user.id)},
        )
        assert result.get("error") is True

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


# ---------------------------------------------------------------------------
# execute_list — unaffected, still lists both legacy and job rows
# ---------------------------------------------------------------------------


class TestExecuteList:
    async def test_execute_list_includes_plan_status_for_job_rows(self, db: AsyncSession, admin_user):
        user, _ = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        await _seed_job_schedule(db, tenant, plan_json={"steps": []}, plan_status="approved")
        await db.commit()

        result = await schedule_ops.execute_list({}, context={"db": db, "tenant_id": str(user.tenant_id)})
        assert not result.get("error")
        assert any(s["plan_status"] == "approved" for s in result["schedules"])
