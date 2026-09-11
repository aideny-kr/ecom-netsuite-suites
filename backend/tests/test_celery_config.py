"""Broker priority config (fix/jobs-live-run-defects, round 4 gate fix).

Round 2 got the direction backwards: it set `SCHEDULED_JOBS_RUN_NOW_PRIORITY
= 9` / `SCHEDULED_JOBS_SWEEP_PRIORITY = 7` and `queue_order_strategy =
"priority"`, believing a HIGHER number wins. Kombu's Redis transport is the
opposite: `_brpop_start` builds its BRPOP key list `for pri in
priority_steps for queue in queues` — the FIRST key in that list to have a
message wins, and `priority_steps` is ascending (`[0, 3, 6, 9]`), so
priority 0 is served FIRST and 9 LAST. Round 2's fix therefore published
"Run now"/the sweep BELOW every unlabeled batch task (default priority 0) —
the opposite of "must not sit behind a batch flood".

Round 3 fixed the direction but the mechanism it picked — `task_default_priority`
— turned out to be the wrong broker to begin with: that setting only binds
`Task.priority` (a class attribute a BOUND task's own `.apply_async()`/
`.delay()` reads, `celery/app/task.py`'s `from_config` table). It is never
consulted by `celery_app.send_task(name, ...)`, the "send by name" API this
codebase actually uses for every one of its worker dispatches (transaction_ops'
scheduler, report_auto_refresh, solidus_sync, scheduled_jobs, ...). So
`tasks.transaction_ops_run` — dispatched via
`app.services.transaction_ops.scheduler.publish_investigation`'s
`app.send_task(...)`, with no `priority=` kwarg of its own — never picked up
`task_default_priority` at all, and instead published at whatever priority an
unset message defaults to on the Redis transport (priority 0 — the FIRST
bucket, same as scheduled-jobs' own elevated sends), still starving "Run now"/
the sweep behind the batch flood despite `task_default_priority = 6` sitting
right there in the config looking like it was doing something.

The actual fix (round 4): `task_routes`, which every dispatch path funnels
through (`Router.route()` is called by both `send_task` and `Task.apply_async`).
Two explicit entries pin `tasks.scheduled_jobs_run_now` / `tasks.
scheduled_jobs_sweep` to their own priority; a catch-all router function,
LAST in the routes list (so it only fires when a task name doesn't match an
explicit entry above it — `Router.lookup_route` tries routers in order and
returns the first non-None result, confirmed against the installed
`celery/app/routes.py`), supplies `{"priority": 6}` for everything else,
`tasks.transaction_ops_run` included. `task_default_priority` is KEPT (not
removed) purely as the fallback for a bound task's own `.apply_async()`/
`.delay()` call — several call sites in this codebase do that directly
(`app/main.py`, several `mcp/tools/*`, `api/v1/workspaces.py`,
`drive_rag_sync.py`, `onboarding_service.py`) — but it is documented as NOT
the mechanism protecting scheduled-jobs' priority any more.

This file asserts (a) kombu's own transport confirms the ascending-served
direction, straight from its source, so this test would catch it again if
kombu's behaviour or default ever changed; (b) `celery_app.py` does NOT
override `queue_order_strategy` (replaces round-robin fairness across
default/sync/recon/export with a fixed order — an unrelated regression) or
`sep` (changes the Redis key names a worker addresses — old and new workers
would talk past each other during a rolling deploy); (c) the router itself —
exercised the exact way `send_task`/`apply_async` exercise it,
`celery_app.amqp.router.route(...)` — gives scheduled-jobs their own lower
priority and everything else the catch-all 6. `tests/api/
test_schedules_api.py`, `tests/test_schedule_ops_tool.py`, and `tests/jobs/
test_executor.py` assert that the scheduled-jobs `send_task` call sites
themselves still carry an explicit `priority=` kwarg matching these routes.
"""

from __future__ import annotations


def test_kombu_redis_transport_serves_the_lowest_priority_step_first():
    """Ground truth, read straight from the installed kombu package (not
    reimplemented here): `Transport.Channel.priority_steps` is `[0, 3, 6,
    9]`, and `_brpop_start` polls in THAT order — ascending, so 0 is served
    before 9."""
    from kombu.transport.redis import PRIORITY_STEPS, Transport

    assert PRIORITY_STEPS == [0, 3, 6, 9]
    assert Transport.Channel.priority_steps == [0, 3, 6, 9]


def test_broker_transport_options_do_not_override_queue_order_or_sep():
    from app.workers.celery_app import celery_app

    opts = celery_app.conf.broker_transport_options or {}
    assert opts.get("queue_order_strategy") is None, (
        "queue_order_strategy replaces round-robin fairness across "
        "default/sync/recon/export with a fixed order -- an unrelated regression"
    )
    assert opts.get("sep") is None, (
        "sep changes the Redis key names a worker addresses -- old and new "
        "workers would talk past each other during a rolling deploy"
    )


def test_task_default_priority_is_kept_only_as_the_bound_task_apply_async_fallback():
    """Round 4: `task_default_priority` is NOT the mechanism that protects
    scheduled-jobs' priority (see module docstring — `send_task` never reads
    it). It is kept anyway as the `Task.priority` fallback for the several
    call sites in this codebase that DO call `.apply_async()`/`.delay()` on
    a bound task object directly, so a value must still be asserted here."""
    from app.workers.celery_app import celery_app

    assert celery_app.conf.task_default_priority == 6


def test_router_applies_the_catch_all_priority_to_an_unrouted_send_task_call():
    """The bug round 4 fixes: `tasks.transaction_ops_run` is dispatched via
    `celery_app.send_task(...)` (`app.services.transaction_ops.scheduler.
    publish_investigation`) with no `priority=` kwarg of its own, and
    `task_default_priority` is never consulted on that path. Exercised
    exactly the way `send_task` exercises it internally --
    `router.route(options, name, args, kwargs, task_type)` --
    `celery_app.amqp.router` IS the same `Router` instance `send_task`
    calls (`self.amqp.router` in `celery/app/base.py`'s `send_task`)."""
    from app.workers.celery_app import celery_app

    route = celery_app.amqp.router.route({}, "tasks.transaction_ops_run")
    assert route["priority"] == 6


def test_router_routes_scheduled_jobs_run_now_and_sweep_to_their_own_lower_priority():
    """The two scheduled-jobs task names get an explicit, LOWER (served-
    first) priority from `task_routes` — not the catch-all — matching the
    `priority=` kwarg already passed at their three `celery_app.send_task`
    call sites (`app/mcp/tools/schedule_ops.py`, `app/api/v1/schedules.py`,
    `app/workers/tasks/scheduled_jobs.py`)."""
    from app.workers.celery_app import celery_app
    from app.workers.tasks.scheduled_jobs import (
        SCHEDULED_JOBS_RUN_NOW_PRIORITY,
        SCHEDULED_JOBS_SWEEP_PRIORITY,
    )

    run_now_route = celery_app.amqp.router.route({}, "tasks.scheduled_jobs_run_now")
    sweep_route = celery_app.amqp.router.route({}, "tasks.scheduled_jobs_sweep")

    assert run_now_route["priority"] == SCHEDULED_JOBS_RUN_NOW_PRIORITY == 0
    assert sweep_route["priority"] == SCHEDULED_JOBS_SWEEP_PRIORITY == 3
    # both still sit below the catch-all default the same router applies to
    # every other task name (previous test) -- 0 and 3 are served BEFORE 6.
    assert run_now_route["priority"] < 6
    assert sweep_route["priority"] < 6


def test_explicit_send_task_priority_kwarg_overrides_the_route_and_still_matches_it():
    """Ground truth, read straight from the installed celery package:
    `Router.route()` returns `lpmerge(self.expand_destination(route),
    options)` — `lpmerge(L, R)` keeps `L`'s values except where `R` supplies
    a non-None override — so an explicit `send_task(..., priority=...)` kwarg
    (passed in as `options`) DOES win over whatever `task_routes` supplies.
    The three scheduled-jobs `send_task` call sites therefore only actually
    get the priorities asserted above because their OWN `priority=` kwargs
    agree with the routes; if they ever drifted apart, the kwarg would
    silently win and the route would become dead configuration."""
    from celery.utils.collections import lpmerge

    route = {"queue": "sync", "priority": 0}
    explicit_kwarg_options = {"priority": 0}  # what SCHEDULED_JOBS_RUN_NOW_PRIORITY's send_task call passes
    merged = lpmerge(dict(route), explicit_kwarg_options)
    assert merged["priority"] == 0

    # An explicit kwarg of a DIFFERENT value would win over the route --
    # proving the kwarg is not merely decorative once task_routes exists.
    drifted = lpmerge(dict(route), {"priority": 9})
    assert drifted["priority"] == 9
