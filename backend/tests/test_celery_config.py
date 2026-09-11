"""Broker priority config (fix/jobs-live-run-defects, round 3 gate fix).

Round 2 got the direction backwards: it set `SCHEDULED_JOBS_RUN_NOW_PRIORITY
= 9` / `SCHEDULED_JOBS_SWEEP_PRIORITY = 7` and `queue_order_strategy =
"priority"`, believing a HIGHER number wins. Kombu's Redis transport is the
opposite: `_brpop_start` builds its BRPOP key list `for pri in
priority_steps for queue in queues` — the FIRST key in that list to have a
message wins, and `priority_steps` is ascending (`[0, 3, 6, 9]`), so
priority 0 is served FIRST and 9 LAST. Round 2's fix therefore published
"Run now"/the sweep BELOW every unlabeled batch task (default priority 0) —
the opposite of "must not sit behind a batch flood".

This file asserts (a) kombu's own transport confirms that ascending-served
direction, straight from its source, so this test would catch it again if
kombu's behaviour or default ever changed; (b) `celery_app.py` sets
`task_default_priority` for unlabeled batch work and does NOT override
`queue_order_strategy` (replaces round-robin fairness across
default/sync/recon/export with a fixed order — an unrelated regression) or
`sep` (changes the Redis key names a worker addresses — old and new workers
would talk past each other during a rolling deploy). `tests/api/
test_schedules_api.py`, `tests/test_schedule_ops_tool.py`, and `tests/jobs/
test_executor.py` assert that the scheduled-jobs sends actually carry a
priority BELOW that default.
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


def test_task_default_priority_leaves_room_below_it_for_scheduled_jobs():
    """Unlabeled batch work (e.g. `tasks.transaction_ops_run`) lands in the
    middle of kombu's four ascending-served steps (6), leaving 0 and 3 free
    for scheduled-jobs' own elevated (LOWER-numbered, served-FIRST) sends."""
    from app.workers.celery_app import celery_app
    from app.workers.tasks.scheduled_jobs import (
        SCHEDULED_JOBS_RUN_NOW_PRIORITY,
        SCHEDULED_JOBS_SWEEP_PRIORITY,
    )

    assert celery_app.conf.task_default_priority == 6
    assert SCHEDULED_JOBS_RUN_NOW_PRIORITY < celery_app.conf.task_default_priority
    assert SCHEDULED_JOBS_SWEEP_PRIORITY < celery_app.conf.task_default_priority
    assert SCHEDULED_JOBS_RUN_NOW_PRIORITY == 0  # a person is waiting right now
    assert SCHEDULED_JOBS_SWEEP_PRIORITY == 3  # a due occurrence fired by the sweep
