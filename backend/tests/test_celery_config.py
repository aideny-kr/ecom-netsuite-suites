"""Broker priority config (fix/jobs-live-run-defects).

Celery's Redis transport supports per-message priority with no new queues
and no worker flags — just `broker_transport_options`. On staging the single
worker (`-Q default,sync,recon,export`, concurrency 2) was flooded by another
feature's `tasks.transaction_ops_run` batch (929 messages on `sync`, 4660 on
`recon`), so a user's "Run now" (`tasks.scheduled_jobs_run_now`) and the Beat
sweep fan-out (`tasks.scheduled_jobs_sweep`) sat behind hundreds of batch
messages. This asserts the transport config that makes priority ordering
take effect at all; `tests/api/test_schedules_api.py`,
`tests/test_schedule_ops_tool.py`, and `tests/jobs/test_executor.py` assert
that the scheduled-jobs sends actually carry a priority.
"""

from __future__ import annotations


def test_broker_transport_options_enable_priority_ordering():
    from app.workers.celery_app import celery_app

    opts = celery_app.conf.broker_transport_options
    assert opts is not None, "celery_app must configure broker_transport_options"
    assert opts.get("queue_order_strategy") == "priority"
    assert list(opts.get("priority_steps", [])) == list(range(10)), (
        "ten priority steps (0-9) so SCHEDULED_JOBS_RUN_NOW_PRIORITY=9 / "
        "SCHEDULED_JOBS_SWEEP_PRIORITY=7 are both valid, distinct steps"
    )
