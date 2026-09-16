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

import os
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest


@pytest.mark.parametrize("name", ["tasks.transaction_ops_collect_due", "tasks.transaction_ops_collect_actions"])
def test_reconciliation_collectors_keep_priority_across_actual_publication_paths(name):
    """Consume real Redis envelopes: bound Task defaults can override routes."""
    from app.workers.celery_app import RECON_COLLECTOR_PRIORITY, RECON_COLLECTOR_QUEUE, celery_app
    from app.workers.tasks import transaction_ops  # noqa: F401 - register the bound tasks

    assert urlparse(celery_app.conf.broker_url).hostname in {"localhost", "127.0.0.1", "redis"}
    assert not celery_app.conf.task_always_eager
    assert RECON_COLLECTOR_PRIORITY == 3
    assert celery_app.amqp.router.route({}, name)["queue"].name == RECON_COLLECTOR_QUEUE
    assert celery_app.tasks[name].queue == RECON_COLLECTOR_QUEUE
    entry = next(e for e in celery_app.conf.beat_schedule.values() if e["task"] == name)
    assert entry["options"]["expires"] == 120
    queue_name = f"recon-priority-test-{uuid4()}"
    with celery_app.connection_for_write() as connection:
        queue = connection.SimpleQueue(queue_name, no_ack=True)
        try:
            options = {"queue": queue_name, "connection": connection, "ignore_result": True}
            # All work stays in a unique unconsumed test queue. Nothing executes.
            celery_app.send_task("tasks.transaction_ops_run", **options)
            celery_app.send_task(name, **options)
            celery_app.tasks[name].apply_async(**options)
            celery_app.tasks[name].apply_async(**options, **entry["options"])
            celery_app.send_task("tasks.scheduled_jobs_run_now", **options)

            messages = [queue.get(block=False) for _ in range(5)]
            assert [m.properties["priority"] for m in messages] == [0, 3, 3, 3, 6]
            assert [m.headers["task"] for m in messages] == [
                "tasks.scheduled_jobs_run_now",
                name,
                name,
                name,
                "tasks.transaction_ops_run",
            ]
        finally:
            queue.queue.delete()
            queue.close()


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


def test_collectors_execute_while_bulk_worker_is_saturated(tmp_path):
    """Real prefork workers: short ticks must run without a free bulk slot.

    Only probe implementations execute, on unique local Redis queues. Worker
    subscription/concurrency flags come from the actual production compose.
    Application publishers retain their actual routes and task metadata.
    """
    import redis
    import yaml

    from app.workers.celery_app import RECON_COLLECTOR_QUEUE, celery_app
    from app.workers.tasks import transaction_ops  # noqa: F401

    broker = celery_app.conf.broker_url
    assert urlparse(broker).hostname in {"localhost", "127.0.0.1", "redis"}
    root = Path(__file__).resolve().parents[2]
    production = yaml.safe_load((root / "docker-compose.prod.yml").read_text())["services"]
    development = yaml.safe_load((root / "docker-compose.yml").read_text())["services"]
    prefix = f"collector-worker-test-{uuid4()}"
    clients = redis.Redis.from_url(broker, decode_responses=True)
    queue_names = {name: f"{prefix}-{name}" for name in celery_app.amqp.queues}
    workers = []
    handles = []
    # Stub just the provider-independent bodies. The probes cannot access DBs
    # or financial providers, even when given real application task names.
    (tmp_path / "collector_probe.py").write_text(
        "import os\n"
        "from celery import Celery, signals\n"
        "import redis\n"
        "app = Celery('collector_probe', broker=os.environ['PROBE_BROKER'])\n"
        "app.conf.update(task_ignore_result=True, task_serializer='json', accept_content=['json'])\n"
        "r = redis.Redis.from_url(os.environ['PROBE_BROKER'], decode_responses=True)\n"
        "p = os.environ['PROBE_PREFIX']\n"
        "@signals.worker_ready.connect\n"
        "def ready(**kw): r.rpush(p + ':ready', 'ready')\n"
        "@app.task(name='tasks.transaction_ops_run')\n"
        "def bulk(marker):\n"
        "    r.rpush(p + ':started', marker)\n"
        "    r.blpop(p + ':release', timeout=30)\n"
        "    r.rpush(p + ':finished', marker)\n"
        "def collect(self): r.rpush(p + ':collected', self.request.headers['probe_marker'])\n"
        "app.task(name='tasks.transaction_ops_collect_due', bind=True)(collect)\n"
        "app.task(name='tasks.transaction_ops_collect_actions', bind=True)(collect)\n"
    )
    try:
        for service in ("worker", "worker-collectors"):
            flags = shlex.split(production[service]["command"])
            original_queues = flags[flags.index("-Q") + 1].split(",")
            if service == "worker":
                assert RECON_COLLECTOR_QUEUE not in original_queues
            else:
                assert original_queues == [RECON_COLLECTOR_QUEUE]
                assert RECON_COLLECTOR_QUEUE in shlex.split(development[service]["command"])
            flags[flags.index("-A") + 1] = "collector_probe:app"
            flags[flags.index("-Q") + 1] = ",".join(queue_names[q] for q in original_queues)
            handle = (tmp_path / f"{service}.log").open("w")
            handles.append(handle)
            workers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        *flags,
                        "--without-gossip",
                        "--without-mingle",
                        "--without-heartbeat",
                        f"--hostname={prefix}-{service}@%h",
                    ],
                    cwd=tmp_path,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "PROBE_BROKER": broker, "PROBE_PREFIX": prefix},
                )
            )
        for _ in workers:
            assert clients.blpop(prefix + ":ready", timeout=20), "probe worker failed to start"
        with celery_app.connection_for_write() as connection:
            options = {"connection": connection, "ignore_result": True}
            # Both production bulk slots are occupied before any collector is
            # published; four further bulks may already be prefetched.
            for index in range(6):
                celery_app.send_task(
                    "tasks.transaction_ops_run", args=[str(index)], queue=queue_names["recon"], **options
                )
            for _ in range(2):
                assert clients.blpop(prefix + ":started", timeout=10)
            expected = []
            for name in ("tasks.transaction_ops_collect_due", "tasks.transaction_ops_collect_actions"):
                route = celery_app.amqp.router.route({}, name)
                assert route["queue"].name == RECON_COLLECTOR_QUEUE
                assert celery_app.tasks[name].queue == RECON_COLLECTOR_QUEUE
                target = queue_names[route["queue"].name]
                beat = next(e["options"] for e in celery_app.conf.beat_schedule.values() if e["task"] == name)
                for path in ("named", "bound", "beat"):
                    marker = name + ":" + path
                    expected.append(marker)
                    call = celery_app.send_task if path == "named" else celery_app.tasks[name].apply_async
                    call_args = [name] if path == "named" else []
                    call(
                        *call_args,
                        headers={"probe_marker": marker},
                        queue=target,
                        **options,
                        **(beat if path == "beat" else {"expires": 120}),
                    )
            actual = [clients.blpop(prefix + ":collected", timeout=10) for _ in expected]
            assert all(actual), "collector starved while bulk worker was busy"
            assert sorted(value[1] for value in actual) == sorted(expected)
            assert clients.llen(prefix + ":finished") == 0, "bulk gate timed out before collector proof"
            clients.rpush(prefix + ":release", *["release"] * 6)
            for _ in range(6):
                assert clients.blpop(prefix + ":finished", timeout=10)
    finally:
        clients.rpush(prefix + ":release", *["release"] * 6)
        for worker in workers:
            worker.terminate()
        for worker in workers:
            try:
                worker.wait(timeout=35)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        for handle in handles:
            handle.close()
        with celery_app.connection_for_write() as connection:
            for name in queue_names.values():
                queue = connection.SimpleQueue(name, no_ack=True)
                queue.queue.delete()
                queue.close()
        keys = list(clients.scan_iter(match=prefix + ":*"))
        if keys:
            clients.delete(*keys)
        clients.close()
