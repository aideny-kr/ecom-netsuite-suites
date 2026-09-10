"""MCP `schedule_ops` tool (Slice 2, Task 4). Spec §B5 (binding):

    MCP `schedule_ops.execute_create` -> the compile path. `execute_run`
    implemented (no longer a stub).

Same tool names, new behaviour — `app.mcp.registry.TOOL_REGISTRY` still
registers `schedule.create` -> `execute_create`, `schedule.list` ->
`execute_list`, `schedule.run` -> `execute_run` (see
`test_registry_still_wires_the_same_tool_names`); `tests/test_prompt_tool_sync.py`
covers the CI invariant that no tool name in the agent's prompt drifts from
this registry.

`execute_run` (item 7, gate fix) now ENQUEUES via Celery instead of running
the plan's steps inline — mirroring `POST /schedules/{id}/run` exactly (both
call the shared `schedule_service.enqueue_run`), so tests here fake
`app.mcp.tools.schedule_ops.celery_app.send_task` (`_capture_send_task`
below — the SAME technique `tests/api/test_schedules_api.py`'s own
`_capture_send_task` uses for the route) rather than monkeypatching
`STEP_REGISTRY`; actual step execution is covered end-to-end by
`tests/jobs/test_executor.py`. `execute_create`'s compile path (item 5, gate
fix) goes through the shared `schedule_service.create_scheduled_job`, which
imports `compile_instruction` at MODULE level — tests patch
`app.services.schedule_service.compile_instruction` (patched where it is
USED, not where it is defined) with a canned `CompiledPlan`/`Clarification`,
exactly like `tests/api/test_schedules_api.py`'s API-level compile tests, so
no real LLM call happens here either.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.registry import TOOL_REGISTRY
from app.mcp.server import mcp_server
from app.mcp.tools import schedule_ops
from app.models.job import Job
from app.models.pipeline import Schedule
from app.models.tenant import Tenant
from app.services.jobs.compiler import Clarification, CompiledPlan


def _compiled_plan() -> CompiledPlan:
    return CompiledPlan(
        plan_json={"steps": [{"id": "q1", "type": "bigquery_sql", "params": {"query": "SELECT 1"}}]},
        summary_line="1 step · BigQuery SQL query",
        kinds={"read"},
        model="claude-test-model",
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


def _capture_send_task(monkeypatch) -> list[tuple[str, dict]]:
    """Fakes `celery_app.send_task` at the point `schedule_ops.py` calls it —
    the SAME technique `tests/api/test_schedules_api.py`'s `_capture_send_task`
    already established for the API route's identical enqueue-then-dispatch
    shape (item 7, gate fix: the two must not drift)."""

    class _FakeResult:
        id = "fake-celery-task-id"

    sent: list[tuple[str, dict]] = []

    def fake_send_task(name, kwargs=None, **_kw):
        sent.append((name, kwargs or {}))
        return _FakeResult()

    monkeypatch.setattr("app.mcp.tools.schedule_ops.celery_app.send_task", fake_send_task)
    return sent


def _spy_commit_then_ctx(monkeypatch, db) -> list[str]:
    """Item 2 (delta gate fix): `db.commit()` on a chat turn's shared session
    clears `SET LOCAL app.current_tenant_id`, but the test fixture's own
    savepoint-based commit does NOT (`tests/conftest.py`'s own documented
    caveat: "a service-level commit here is RELEASE SAVEPOINT, which --
    unlike a real COMMIT -- does NOT clear SET LOCAL GUCs... assert the
    set_tenant_context CALL ORDERING instead"). So this spies call order
    (`commit` then `ctx`) rather than querying `current_setting(...)`
    afterward, which would false-pass in this fixture regardless of whether
    the fix is applied — see `tests/test_report_refresh.py`'s own
    `_spy_events` for the same established pattern.

    Item 5 (delta gate fix E): the re-establish-context call this spies on
    moved from each handler (`schedule_ops.py`) to the ONE choke point every
    tool call passes through, `governance.governed_execute` — so this now
    spies `app.mcp.governance.set_tenant_context`, and the caller must
    dispatch through `mcp_server.call_tool` (not call the handler directly)
    for `governed_execute` to run at all."""
    events: list[str] = []
    real_commit = db.commit

    async def spy_commit():
        events.append("commit")
        await real_commit()

    async def spy_ctx(session, tenant_id):
        events.append("ctx")

    monkeypatch.setattr(db, "commit", spy_commit)
    monkeypatch.setattr("app.mcp.governance.set_tenant_context", spy_ctx)
    return events


class TestExecuteRun:
    """Item 7 (gate fix): `execute_run` now ENQUEUES via Celery, mirroring
    `POST /schedules/{id}/run` exactly, instead of running the plan's steps
    INLINE on the chat request's own session — the same nginx-timeout problem
    Task 5's residual fix solved for the API route (a real Inventory Aging
    run can take minutes). The actual step execution is covered end-to-end
    by `tests/jobs/test_executor.py`; these tests assert the enqueue (a
    pending `jobs` row + the Celery dispatch), not a run outcome."""

    async def test_execute_run_enqueues_and_returns_the_jobs_id(self, db: AsyncSession, admin_user, monkeypatch):
        sent = _capture_send_task(monkeypatch)

        user, _ = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        plan_json = {"steps": [{"id": "s1", "type": "bigquery_sql", "params": {"query": "SELECT 1"}}]}
        schedule = await _seed_job_schedule(db, tenant, plan_json=plan_json)
        await db.commit()

        result = await schedule_ops.execute_run(
            {"schedule_id": str(schedule.id)},
            context={"db": db, "tenant_id": str(user.tenant_id), "actor_id": str(user.id)},
        )
        assert not result.get("error"), result
        assert result["status"] == "queued"
        assert result["jobs_id"]
        assert result["schedule_id"] == str(schedule.id)

        job = (await db.execute(select(Job).where(Job.id == uuid.UUID(result["jobs_id"])))).scalar_one()
        assert job.status == "pending"
        assert job.parameters["schedule_id"] == str(schedule.id)
        assert job.parameters["use_pending"] is False
        assert job.parameters["plan"] == plan_json  # the plan THIS call validated, snapshotted

        assert len(sent) == 1
        task_name, kwargs = sent[0]
        assert task_name == "tasks.scheduled_jobs_run_now"
        assert kwargs["schedule_id"] == str(schedule.id)
        assert kwargs["job_id"] == result["jobs_id"]
        assert kwargs["use_pending"] is False

    async def test_execute_run_reestablishes_tenant_context_after_its_commit(
        self, db: AsyncSession, admin_user, monkeypatch
    ):
        """Item 5 (delta gate fix E): the "re-set tenant context after
        commit" block moved out of `execute_run` itself and into
        `governance.governed_execute` -- the ONE choke point every tool call
        passes through, right after `execute_fn` returns -- so `execute_run`
        commits on the chat turn's shared session before dispatching the
        Celery task, and `governed_execute` must reset `SET LOCAL
        app.current_tenant_id` immediately afterward so its own
        `tool.executed` audit write (and the rest of the chat turn) does not
        silently fail its RLS `WITH CHECK`. Must be routed through the REAL
        dispatch path (`mcp_server.call_tool`), not a direct
        `schedule_ops.execute_run()` call, since the handler no longer calls
        `set_tenant_context` itself."""
        _capture_send_task(monkeypatch)

        user, _ = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        plan_json = {"steps": [{"id": "s1", "type": "bigquery_sql", "params": {"query": "SELECT 1"}}]}
        schedule = await _seed_job_schedule(db, tenant, plan_json=plan_json)
        await db.commit()

        events = _spy_commit_then_ctx(monkeypatch, db)

        result = await mcp_server.call_tool(
            tool_name="schedule.run",
            params={"schedule_id": str(schedule.id)},
            tenant_id=str(user.tenant_id),
            actor_id=str(user.id),
            db=db,
        )
        assert not result.get("error"), result
        assert "commit" in events
        commit_idx = events.index("commit")
        assert events[commit_idx + 1] == "ctx", (
            f"set_tenant_context must immediately follow execute_run's commit; events={events}"
        )

    async def test_execute_run_rejects_a_never_approved_plan(self, db: AsyncSession, admin_user, monkeypatch):
        """HITL gate (review finding): the chat agent must not be able to
        call `schedule.create` then `schedule.run` back-to-back and enqueue
        a compiled-but-unreviewed plan's steps. `use_pending=False` (the
        default) against a `plan_status == "pending_approval"` schedule —
        even with a real non-empty `plan_json` — must be refused, with NO
        `jobs` row created and NO Celery dispatch."""
        sent = _capture_send_task(monkeypatch)

        user, _ = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        schedule = await _seed_job_schedule(
            db,
            tenant,
            plan_json={"steps": [{"id": "s1", "type": "bigquery_sql", "params": {"query": "SELECT 1"}}]},
            plan_status="pending_approval",
        )
        await db.commit()

        result = await schedule_ops.execute_run(
            {"schedule_id": str(schedule.id)},
            context={"db": db, "tenant_id": str(user.tenant_id), "actor_id": str(user.id)},
        )
        assert result.get("error") is True
        assert not result.get("jobs_id")
        assert sent == []

        jobs = (
            (await db.execute(select(Job).where(Job.parameters["schedule_id"].astext == str(schedule.id))))
            .scalars()
            .all()
        )
        assert jobs == []

    async def test_execute_run_use_pending_enqueues_with_the_pending_plan_and_next_version(
        self, db: AsyncSession, admin_user, monkeypatch
    ):
        sent = _capture_send_task(monkeypatch)

        user, _ = admin_user
        tenant = (await db.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()
        pending_plan = {"steps": [{"id": "s1", "type": "bigquery_sql", "params": {"query": "SELECT 1"}}]}
        schedule = Schedule(
            tenant_id=tenant.id,
            name="Pending job (test)",
            schedule_type="job",
            is_active=True,
            plan_json=None,
            pending_plan_json=pending_plan,
            plan_status="pending_approval",
            plan_version=0,
        )
        db.add(schedule)
        await db.commit()

        result = await schedule_ops.execute_run(
            {"schedule_id": str(schedule.id), "use_pending": True},
            context={"db": db, "tenant_id": str(user.tenant_id)},
        )
        assert not result.get("error"), result
        assert result["status"] == "queued"

        job = (await db.execute(select(Job).where(Job.id == uuid.UUID(result["jobs_id"])))).scalar_one()
        assert job.parameters["use_pending"] is True
        assert job.parameters["plan_version"] == 1  # 0 + 1, records what approval WOULD produce
        assert job.parameters["plan"] == pending_plan

        assert len(sent) == 1
        assert sent[0][1]["use_pending"] is True

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

    async def test_execute_create_over_quota_is_error_no_row(self, db: AsyncSession, admin_user, monkeypatch):
        """Item 5 (gate fix): `execute_create`'s instruction branch used to
        bypass the plan-quota entitlement check entirely."""
        user, _ = admin_user

        async def fake_check_entitlement(db, tenant_id, feature):
            assert feature == "schedules"
            return False

        monkeypatch.setattr(
            "app.services.schedule_service.entitlement_service.check_entitlement", fake_check_entitlement
        )

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

    async def test_execute_create_commits_after_a_successful_instruction_create(
        self, db: AsyncSession, admin_user, monkeypatch
    ):
        """Item 2 (delta gate fix): `execute_create`'s instruction branch
        called `create_scheduled_job` (flush-only) and never committed, while
        its own inline comment claimed this handler owns the commit — a
        schedule created via chat was never actually durable past the
        request. `commit` is spied with `AsyncMock(wraps=db.commit)` so the
        REAL commit still runs (the row must actually land)."""
        user, _ = admin_user

        async def fake_compile(db, *, tenant_id, instruction, actor_id, llm=None, plan_version=None):
            return _compiled_plan()

        monkeypatch.setattr("app.services.schedule_service.compile_instruction", fake_compile)
        commit_spy = AsyncMock(wraps=db.commit)
        monkeypatch.setattr(db, "commit", commit_spy)

        result = await schedule_ops.execute_create(
            {"instruction": "weekly inventory aging report"},
            context={"db": db, "tenant_id": str(user.tenant_id), "actor_id": str(user.id)},
        )
        assert not result.get("error")
        commit_spy.assert_awaited_once()

    async def test_execute_create_commits_after_a_successful_legacy_create(
        self, db: AsyncSession, admin_user, monkeypatch
    ):
        """Item 2 (delta gate fix): the legacy direct-create branch (no
        `instruction`) never committed either -- both branches must, per the
        same convention `execute_run`/`recon_approve.py` already follow."""
        user, _ = admin_user
        commit_spy = AsyncMock(wraps=db.commit)
        monkeypatch.setattr(db, "commit", commit_spy)

        result = await schedule_ops.execute_create(
            {"name": "Legacy MCP Sync", "schedule_type": "sync", "cron": "0 0 * * *"},
            context={"db": db, "tenant_id": str(user.tenant_id)},
        )
        assert not result.get("error")
        commit_spy.assert_awaited_once()

    async def test_execute_create_instruction_branch_reestablishes_tenant_context_after_commit(
        self, db: AsyncSession, admin_user, monkeypatch
    ):
        """Item 5 (delta gate fix E): the "re-set tenant context after
        commit" block moved out of `execute_create` itself and into
        `governance.governed_execute` -- so `execute_create`'s instruction
        branch commits on the chat turn's shared session (clearing `SET
        LOCAL app.current_tenant_id`), and `governed_execute` must reset it
        immediately afterward so its own `tool.executed` audit write does
        not silently fail its RLS `WITH CHECK`. Routed through the REAL
        dispatch path (`mcp_server.call_tool`) since the handler no longer
        calls `set_tenant_context` itself."""
        user, _ = admin_user

        async def fake_compile(db, *, tenant_id, instruction, actor_id, llm=None, plan_version=None):
            return _compiled_plan()

        monkeypatch.setattr("app.services.schedule_service.compile_instruction", fake_compile)
        events = _spy_commit_then_ctx(monkeypatch, db)

        result = await mcp_server.call_tool(
            tool_name="schedule.create",
            params={"instruction": "weekly inventory aging report"},
            tenant_id=str(user.tenant_id),
            actor_id=str(user.id),
            db=db,
        )
        assert not result.get("error")
        assert "commit" in events
        commit_idx = events.index("commit")
        assert events[commit_idx + 1] == "ctx", (
            f"set_tenant_context must immediately follow the instruction branch's commit; events={events}"
        )

    async def test_execute_create_legacy_branch_reestablishes_tenant_context_after_commit(
        self, db: AsyncSession, admin_user, monkeypatch
    ):
        """Item 5 (delta gate fix E): the legacy direct-create branch's
        commit must ALSO be followed by `governed_execute`'s re-established
        tenant context -- both branches share the same trap, and both are
        now covered by the ONE dispatcher-level fix rather than two pasted
        blocks. Routed through the REAL dispatch path for the same reason."""
        user, _ = admin_user
        events = _spy_commit_then_ctx(monkeypatch, db)

        result = await mcp_server.call_tool(
            tool_name="schedule.create",
            params={"name": "Legacy MCP Sync", "schedule_type": "sync", "cron": "0 0 * * *"},
            tenant_id=str(user.tenant_id),
            db=db,
        )
        assert not result.get("error")
        assert "commit" in events
        commit_idx = events.index("commit")
        assert events[commit_idx + 1] == "ctx", (
            f"set_tenant_context must immediately follow the legacy branch's commit; events={events}"
        )

    async def test_execute_create_does_not_commit_on_clarification(self, db: AsyncSession, admin_user, monkeypatch):
        """Item 2 (delta gate fix): a Clarification creates NOTHING -- commit
        must not be called on this error path."""
        user, _ = admin_user

        async def fake_compile(db, *, tenant_id, instruction, actor_id, llm=None, plan_version=None):
            return Clarification(question="Which subsidiary?")

        monkeypatch.setattr("app.services.schedule_service.compile_instruction", fake_compile)
        commit_spy = AsyncMock(wraps=db.commit)
        monkeypatch.setattr(db, "commit", commit_spy)

        result = await schedule_ops.execute_create(
            {"instruction": "deliver inventory aging weekly"},
            context={"db": db, "tenant_id": str(user.tenant_id)},
        )
        assert result.get("clarification") is True
        commit_spy.assert_not_awaited()

    async def test_execute_create_does_not_commit_on_validation_error(self, db: AsyncSession, admin_user, monkeypatch):
        """Item 2 (delta gate fix): an over-length instruction is rejected by
        `ScheduleCreate` before `create_scheduled_job` ever runs -- no commit."""
        user, _ = admin_user

        async def fail_if_called(*a, **k):
            raise AssertionError("compile_instruction must not be called for an over-length instruction")

        monkeypatch.setattr("app.services.schedule_service.compile_instruction", fail_if_called)
        commit_spy = AsyncMock(wraps=db.commit)
        monkeypatch.setattr(db, "commit", commit_spy)

        result = await schedule_ops.execute_create(
            {"instruction": "x" * 10_000},
            context={"db": db, "tenant_id": str(user.tenant_id), "actor_id": str(user.id)},
        )
        assert result.get("error") is True
        commit_spy.assert_not_awaited()

    async def test_execute_create_does_not_commit_on_over_quota_error(self, db: AsyncSession, admin_user, monkeypatch):
        """Item 2 (delta gate fix): a quota-exceeded rejection creates
        nothing -- no commit."""
        user, _ = admin_user

        async def fake_check_entitlement(db, tenant_id, feature):
            return False

        monkeypatch.setattr(
            "app.services.schedule_service.entitlement_service.check_entitlement", fake_check_entitlement
        )
        commit_spy = AsyncMock(wraps=db.commit)
        monkeypatch.setattr(db, "commit", commit_spy)

        result = await schedule_ops.execute_create(
            {"instruction": "weekly inventory aging report"},
            context={"db": db, "tenant_id": str(user.tenant_id), "actor_id": str(user.id)},
        )
        assert result.get("error") is True
        commit_spy.assert_not_awaited()

    async def test_schedule_create_instruction_survives_real_dispatch(self, db: AsyncSession, admin_user, monkeypatch):
        """Governance regression (item 6): schedule.create's OLD allowlist
        (name, schedule_type, cron, params) stripped instruction/timezone/
        delivery before execute_create ever saw them through the REAL
        dispatch path — a chat-created Scheduled Job silently fell through
        to the legacy path even though a direct execute_create() call (every
        other test in this class) never exercises governance at all. Must
        go through mcp_server.call_tool -> governed_execute ->
        validate_params (mirrors
        test_recon_resolution_chat_tools.py::test_approve_group_included_
        above_materiality_ids_survives_real_dispatch's pattern)."""
        user, _ = admin_user

        async def fake_compile(db, *, tenant_id, instruction, actor_id, llm=None, plan_version=None):
            return _compiled_plan()

        monkeypatch.setattr("app.services.schedule_service.compile_instruction", fake_compile)

        out = await mcp_server.call_tool(
            tool_name="schedule.create",
            params={"instruction": "weekly inventory aging report"},
            tenant_id=str(user.tenant_id),
            actor_id=str(user.id),
            db=db,
        )
        assert not out.get("error"), out
        assert out["plan_status"] == "pending_approval"

        row = (
            (
                await db.execute(
                    select(Schedule).where(Schedule.tenant_id == user.tenant_id, Schedule.schedule_type == "job")
                )
            )
            .scalars()
            .one()
        )
        # Proves instruction survived validate_params through the real
        # dispatch, not just a direct execute_create() call.
        assert row.instruction == "weekly inventory aging report"


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
