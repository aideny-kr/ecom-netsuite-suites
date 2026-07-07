"""Slice C (live-dashboard reports) — the per-tenant auto-refresh sweep.

``sweep_tenant_reports`` finds due recipe-bearing reports and replays each through
``refresh_report(actor_id=None, actor_type="system")``. It owns the failure ladder
(spec §4C/§6.1 — launch-critical with daily-by-default): consecutive failures
increment ``refresh_failure_count``; hourly backs off to daily at 3; ~7 pauses the
report (``auto_refresh_paused_at``) until the user's explicit resume. Debounce and
supersede mean "someone else refreshed" — never ladder increments. Celery glue is
tested separately; these tests call the inner async function directly with the
``db`` fixture (house pattern: test_recon_scheduled_run_all.py).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.core.database import set_tenant_context
from app.models.report import Report
from app.services.report.refresh_service import RefreshDebouncedError, RefreshSupersededError
from app.workers.tasks.report_auto_refresh import (
    DUE_SLACK_SECONDS,
    HOURLY_BACKOFF_THRESHOLD,
    PAUSE_THRESHOLD,
    sweep_tenant_reports,
)
from tests.conftest import create_test_tenant, create_test_user

HOUR = 3600
DAY = 86400


def _recipe(query="SELECT 1"):
    return {
        "schema_version": 1,
        "captured_at": "2026-07-06T18:00:00+00:00",
        "sections": [
            {"type": "heading", "level": 1, "text": "Cash"},
            {"type": "table", "result_id": "r1"},
        ],
        "sources": {"r1": {"tool": "netsuite_suiteql", "params": {"query": query}, "connection_id": None}},
    }


def _result_str(amount=7):
    return json.dumps(
        {"success": True, "columns": ["account", "amount"], "rows": [["Cash", amount]], "row_count": 1, "query": "q"}
    )


async def _tenant(db, name="SweepCorp"):
    tenant = await create_test_tenant(db, name=name)
    user, _ = await create_test_user(db, tenant)
    await set_tenant_context(db, str(tenant.id))
    return tenant, user


async def _seed(db, tenant, user, *, query="SELECT 1", recipe=True, stamp_ago=None, **cols):
    report = Report(
        tenant_id=tenant.id,
        title="R",
        spec_json={"sections": []},
        rendered_html="<html></html>",
        created_by=user.id,
        recipe_json=_recipe(query) if recipe else None,
        last_refreshed_at=(datetime.now(timezone.utc) - timedelta(seconds=stamp_ago)) if stamp_ago else None,
        **cols,
    )
    db.add(report)
    await db.flush()
    return report


def _patch_executor(monkeypatch, fail_queries=()):
    calls: list = []

    async def fake_execute(tool_name, tool_input, tenant_id, actor_id, correlation_id, db, **kw):
        calls.append({"tool": tool_name, "params": tool_input, "actor_id": actor_id})
        if tool_input.get("query") in fail_queries:
            return json.dumps({"error": True, "message": "invalid or expired token"})
        return _result_str()

    monkeypatch.setattr("app.services.chat.tools.execute_tool_call", fake_execute)
    return calls


async def _reload(db, tenant_id, report_id) -> Report:
    await set_tenant_context(db, str(tenant_id))
    return (await db.execute(select(Report).where(Report.id == report_id))).scalar_one()


# --- due computation -------------------------------------------------------------------


async def test_never_refreshed_report_is_due_and_refreshes_as_system(db, monkeypatch):
    tenant, user = await _tenant(db)
    report = await _seed(db, tenant, user)  # NULL stamp = due
    calls = _patch_executor(monkeypatch)

    stats = await sweep_tenant_reports(db, tenant.id)

    assert stats["due"] == 1 and stats["refreshed"] == 1 and stats["failed"] == 0
    assert calls and calls[0]["actor_id"] is None  # system replay, no human actor
    row = await _reload(db, tenant.id, report.id)
    assert row.version == 2
    assert row.refresh_failure_count == 0
    audit = (
        await db.execute(
            text(
                "SELECT actor_id, actor_type FROM audit_events "
                "WHERE action='report.refresh' AND status='success' AND resource_id=:rid"
            ),
            {"rid": str(report.id)},
        )
    ).first()
    assert audit is not None and audit[0] is None and audit[1] == "system"


async def test_off_paused_and_recipeless_reports_are_never_swept(db, monkeypatch):
    tenant, user = await _tenant(db)
    await _seed(db, tenant, user, auto_refresh="off")
    await _seed(db, tenant, user, auto_refresh_paused_at=datetime.now(timezone.utc))
    # Snapshot-only, seeded EXACTLY like production compose (report_export.py passes
    # recipe_json=None explicitly): the ORM's JSONB default persists that as jsonb
    # 'null', NOT SQL NULL — an IS NOT NULL sweep predicate would select it, fail it
    # daily, and pause it with junk audit after a week.
    await _seed(db, tenant, user, recipe=False)
    calls = _patch_executor(monkeypatch)

    stats = await sweep_tenant_reports(db, tenant.id)

    assert stats["due"] == 0 and stats["refreshed"] == 0
    assert calls == []


async def test_daily_due_only_after_a_day_with_slack(db, monkeypatch):
    tenant, user = await _tenant(db)
    fresh = await _seed(db, tenant, user, stamp_ago=12 * HOUR)
    due = await _seed(db, tenant, user, query="SELECT due", stamp_ago=DAY - DUE_SLACK_SECONDS + 60)
    _patch_executor(monkeypatch)

    stats = await sweep_tenant_reports(db, tenant.id)

    assert stats["due"] == 1 and stats["refreshed"] == 1
    assert (await _reload(db, tenant.id, fresh.id)).version == 1
    assert (await _reload(db, tenant.id, due.id)).version == 2


async def test_hourly_due_after_an_hour(db, monkeypatch):
    tenant, user = await _tenant(db)
    fresh = await _seed(db, tenant, user, auto_refresh="hourly", stamp_ago=30 * 60)
    due = await _seed(db, tenant, user, query="SELECT due", auto_refresh="hourly", stamp_ago=61 * 60)
    _patch_executor(monkeypatch)

    stats = await sweep_tenant_reports(db, tenant.id)

    assert stats["due"] == 1 and stats["refreshed"] == 1
    assert (await _reload(db, tenant.id, fresh.id)).version == 1
    assert (await _reload(db, tenant.id, due.id)).version == 2


async def test_hourly_backs_off_to_daily_after_repeated_failures(db, monkeypatch):
    """§4C: hourly + refresh_failure_count >= 3 behaves as DAILY — the user's chosen
    interval is never overwritten; the backoff is derived."""
    tenant, user = await _tenant(db)
    backed_off = await _seed(
        db, tenant, user, auto_refresh="hourly", refresh_failure_count=HOURLY_BACKOFF_THRESHOLD, stamp_ago=2 * HOUR
    )
    still_hourly = await _seed(
        db,
        tenant,
        user,
        query="SELECT h",
        auto_refresh="hourly",
        refresh_failure_count=HOURLY_BACKOFF_THRESHOLD - 1,
        stamp_ago=2 * HOUR,
    )
    _patch_executor(monkeypatch)

    stats = await sweep_tenant_reports(db, tenant.id)

    assert stats["due"] == 1 and stats["refreshed"] == 1
    assert (await _reload(db, tenant.id, backed_off.id)).version == 1  # waits for the daily window
    assert (await _reload(db, tenant.id, still_hourly.id)).version == 2
    row = await _reload(db, tenant.id, backed_off.id)
    assert row.auto_refresh == "hourly"  # choice preserved


async def test_backed_off_hourly_refreshes_on_the_daily_window(db, monkeypatch):
    tenant, user = await _tenant(db)
    report = await _seed(
        db, tenant, user, auto_refresh="hourly", refresh_failure_count=HOURLY_BACKOFF_THRESHOLD, stamp_ago=25 * HOUR
    )
    _patch_executor(monkeypatch)
    stats = await sweep_tenant_reports(db, tenant.id)
    assert stats["refreshed"] == 1
    assert (await _reload(db, tenant.id, report.id)).version == 2


# --- failure ladder --------------------------------------------------------------------


async def test_failure_increments_count_and_keeps_last_good_version(db, monkeypatch):
    tenant, user = await _tenant(db)
    report = await _seed(db, tenant, user, query="SELECT dead")
    tid, rid = tenant.id, report.id  # the failure path's rollback expires ORM instances
    _patch_executor(monkeypatch, fail_queries=("SELECT dead",))

    stats = await sweep_tenant_reports(db, tid)

    assert stats["failed"] == 1 and stats["refreshed"] == 0
    row = await _reload(db, tid, rid)
    assert row.refresh_failure_count == 1
    assert row.auto_refresh_paused_at is None
    assert row.version == 1 and row.rendered_html == "<html></html>"  # last good version intact


async def test_seventh_consecutive_failure_pauses_and_audits(db, monkeypatch):
    tenant, user = await _tenant(db)
    report = await _seed(
        db, tenant, user, query="SELECT dead", refresh_failure_count=PAUSE_THRESHOLD - 1, stamp_ago=2 * DAY
    )
    tid, rid = tenant.id, report.id  # the failure path's rollback expires ORM instances
    _patch_executor(monkeypatch, fail_queries=("SELECT dead",))

    stats = await sweep_tenant_reports(db, tid)

    assert stats["failed"] == 1 and stats["paused"] == 1
    row = await _reload(db, tid, rid)
    assert row.refresh_failure_count == PAUSE_THRESHOLD
    assert row.auto_refresh_paused_at is not None
    audit = (
        await db.execute(
            text(
                "SELECT actor_id, actor_type FROM audit_events "
                "WHERE action='report.auto_refresh_paused' AND resource_id=:rid"
            ),
            {"rid": str(rid)},
        )
    ).first()
    assert audit is not None and audit[0] is None and audit[1] == "system"

    # paused → excluded from the next sweep entirely (no retry storm against dead OAuth)
    row.last_refreshed_at = datetime.now(timezone.utc) - timedelta(days=2)
    await db.flush()
    stats2 = await sweep_tenant_reports(db, tid)
    assert stats2["due"] == 0


async def test_success_resets_failure_count(db, monkeypatch):
    tenant, user = await _tenant(db)
    report = await _seed(db, tenant, user, refresh_failure_count=4, stamp_ago=2 * DAY)
    _patch_executor(monkeypatch)

    await sweep_tenant_reports(db, tenant.id)

    row = await _reload(db, tenant.id, report.id)
    assert row.version == 2 and row.refresh_failure_count == 0


async def test_debounce_and_supersede_skips_never_touch_the_ladder(db, monkeypatch):
    """429/supersede mean 'someone else refreshed' — not a dead connection. The ladder
    must not move (a manual-refresh race could otherwise walk a healthy report toward
    pause)."""
    tenant, user = await _tenant(db)
    report = await _seed(db, tenant, user, refresh_failure_count=2, stamp_ago=2 * DAY)

    async def debounced(*a, **kw):
        raise RefreshDebouncedError(120)

    monkeypatch.setattr("app.workers.tasks.report_auto_refresh.refresh_report", debounced)
    stats = await sweep_tenant_reports(db, tenant.id)
    assert stats["skipped"] == 1 and stats["failed"] == 0
    assert (await _reload(db, tenant.id, report.id)).refresh_failure_count == 2

    async def superseded(*a, **kw):
        raise RefreshSupersededError()

    monkeypatch.setattr("app.workers.tasks.report_auto_refresh.refresh_report", superseded)
    stats = await sweep_tenant_reports(db, tenant.id)
    assert stats["skipped"] == 1 and stats["failed"] == 0
    assert (await _reload(db, tenant.id, report.id)).refresh_failure_count == 2


# --- batch bound + isolation -----------------------------------------------------------


async def test_batch_bound_takes_most_stale_first(db, monkeypatch):
    tenant, user = await _tenant(db)
    never = await _seed(db, tenant, user, query="SELECT a")  # NULL stamp = most stale
    oldest = await _seed(db, tenant, user, query="SELECT b", stamp_ago=30 * HOUR)
    newest_due = await _seed(db, tenant, user, query="SELECT c", stamp_ago=25 * HOUR)
    _patch_executor(monkeypatch)

    stats = await sweep_tenant_reports(db, tenant.id, batch=2)

    assert stats["due"] == 3 and stats["refreshed"] == 2
    assert (await _reload(db, tenant.id, never.id)).version == 2
    assert (await _reload(db, tenant.id, oldest.id)).version == 2
    assert (await _reload(db, tenant.id, newest_due.id)).version == 1  # waits for the next tick


async def test_one_report_failure_does_not_abort_the_batch(db, monkeypatch):
    tenant, user = await _tenant(db)
    dead = await _seed(db, tenant, user, query="SELECT dead")  # NULL stamp → swept first
    alive = await _seed(db, tenant, user, query="SELECT alive", stamp_ago=25 * HOUR)
    tid, dead_id, alive_id = tenant.id, dead.id, alive.id  # rollback expires instances
    _patch_executor(monkeypatch, fail_queries=("SELECT dead",))

    stats = await sweep_tenant_reports(db, tid)

    assert stats["failed"] == 1 and stats["refreshed"] == 1
    assert (await _reload(db, tid, dead_id)).version == 1
    assert (await _reload(db, tid, alive_id)).version == 2


async def test_sweep_only_touches_the_given_tenant(db, monkeypatch):
    tenant_a, user_a = await _tenant(db, name="Sweep A")
    tenant_b, user_b = await _tenant(db, name="Sweep B")
    theirs = await _seed(db, tenant_b, user_b)  # due (NULL stamp) but belongs to B
    await set_tenant_context(db, str(tenant_a.id))
    _patch_executor(monkeypatch)

    stats = await sweep_tenant_reports(db, tenant_a.id)

    assert stats["due"] == 0 and stats["refreshed"] == 0
    assert (await _reload(db, tenant_b.id, theirs.id)).version == 1


async def test_stats_shape_for_job_instrumentation(db, monkeypatch):
    tenant, user = await _tenant(db)
    await _seed(db, tenant, user)
    _patch_executor(monkeypatch)
    stats = await sweep_tenant_reports(db, tenant.id)
    assert set(stats) >= {"tenant_id", "due", "refreshed", "failed", "skipped", "paused"}
    assert stats["tenant_id"] == str(tenant.id)
    assert uuid.UUID(stats["tenant_id"])  # JSON-serializable id, not a UUID object


# --- Celery glue (fan-out + per-tenant task + Beat entry + env gate) --------------------


from pathlib import Path  # noqa: E402  (glue-test helpers, house pattern)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


def _task_source() -> str:
    with open(_BACKEND_ROOT / "app/workers/tasks/report_auto_refresh.py") as f:
        return f.read()


class TestPreforkEventLoopSafety:
    """Prefork workers must NOT use the module-level async_session_factory (bound to
    the parent's event loop) and must use the SESSION-scoped tenant context —
    refresh_report commits repeatedly mid-run, which clears SET LOCAL. Source
    inspection, same style as test_recon_scheduled_run_all.py."""

    def test_uses_worker_async_session(self):
        src = _task_source()
        assert "worker_async_session" in src
        assert "async_session_factory" not in src

    def test_per_tenant_task_sets_session_scoped_tenant_context(self):
        assert "set_tenant_context_session" in _task_source()

    def test_has_no_self_retry(self):
        """A failed sweep must NOT retry in-task (the ladder + next tick are the
        retry); InstrumentedTask records the failure."""
        assert "self.retry" not in _task_source()


def test_tasks_registered_on_sync_queue():
    from app.workers.tasks.report_auto_refresh import report_auto_refresh_all, report_auto_refresh_tenant

    assert hasattr(report_auto_refresh_all, "delay")
    assert report_auto_refresh_all.name == "tasks.report_auto_refresh_all"
    assert report_auto_refresh_all.queue == "sync"
    assert hasattr(report_auto_refresh_tenant, "delay")
    assert report_auto_refresh_tenant.name == "tasks.report_auto_refresh"
    assert report_auto_refresh_tenant.queue == "sync"


def test_beat_entry_and_module_registration():
    from app.workers.celery_app import celery_app

    assert "app.workers.tasks.report_auto_refresh" in celery_app.conf.include
    entry = celery_app.conf.beat_schedule.get("report-auto-refresh-hourly")
    assert entry is not None, "Beat must tick the fan-out hourly"
    assert entry["task"] == "tasks.report_auto_refresh_all"


async def test_fanout_dispatches_one_per_active_tenant(db, monkeypatch):
    from app.core.config import settings
    from app.workers.tasks import report_auto_refresh as mod

    monkeypatch.setattr(settings, "REPORT_AUTO_REFRESH_ENABLED", True)
    tenant_a, _ = await _tenant(db, name="Fanout A")
    tenant_b, _ = await _tenant(db, name="Fanout B")
    inactive = await create_test_tenant(db, name="Fanout inactive")
    inactive.is_active = False
    await db.flush()

    sent: list[dict] = []
    monkeypatch.setattr(
        mod.celery_app,
        "send_task",
        lambda name, kwargs=None, queue=None, **_: sent.append({"name": name, "kwargs": kwargs, "queue": queue}),
    )
    stats = await mod.collect_and_dispatch(db)

    dispatched_ids = {s["kwargs"]["tenant_id"] for s in sent}
    assert {str(tenant_a.id), str(tenant_b.id)} <= dispatched_ids
    assert str(inactive.id) not in dispatched_ids  # deactivated tenants excluded (house rule)
    assert all(s["name"] == "tasks.report_auto_refresh" and s["queue"] == "sync" for s in sent)
    assert stats["dispatched"] == len(sent) and stats["enabled"] is True


async def test_fanout_env_gate_off_is_a_noop(db, monkeypatch):
    """REPORT_AUTO_REFRESH_ENABLED defaults False — the Beat entry is always
    registered, the body no-ops (the AGENT_BENCHMARK_VS_MCP pattern)."""
    from app.core.config import settings
    from app.workers.tasks import report_auto_refresh as mod

    assert settings.REPORT_AUTO_REFRESH_ENABLED is False  # default OFF until staging flip
    await _tenant(db, name="Gated")
    sent: list = []
    monkeypatch.setattr(mod.celery_app, "send_task", lambda *a, **kw: sent.append(a))

    stats = await mod.collect_and_dispatch(db)

    assert stats == {"enabled": False, "dispatched": 0}
    assert sent == []
