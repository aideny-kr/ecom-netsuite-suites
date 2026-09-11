# Reconciliation collector worker

Both compose files reserve `worker-collectors` for `recon-control`. It runs only
`transaction_ops_collect_due` and `transaction_ops_collect_actions`: durable DB
work discovery and publication, not investigation or financial execution. Long
scans and approved financial operations remain on the original worker queues.
The collector worker has two prefork slots, matching the two minute ticks, with
prefetch multiplier 1. The existing 50/55-second soft/hard limits and 120-second
tick expiry remain unchanged. Priority alone cannot bypass tasks already reserved
by a busy bulk worker.

## Deployment

The host compose file must include the versioned `worker-collectors` service before
starting the new API/Beat release. Existing customized host compose files are not
automatically replaced by GitHub Actions. Preserve their Redis dependencies, env
file and image pins; copy the existing worker settings and replace only the command
with the collector command in `docker-compose.prod.yml`. Validate `docker compose
-f docker-compose.prod.yml config` before release. Budget one additional worker
container (two small collector processes); check available memory first.

Stop Beat, allow active work to finish, and deploy the same reviewed backend image
to API, worker, worker-collectors and Beat. Start the collector worker before Beat.
Verify all four actual image IDs, health, and successful collector task completion
while investigations are active. The standard deploy workflow includes the new
service; it requires the host compose prerequisite above.

Old envelopes on `recon` remain consumable by the original worker and stateless
expired ticks can be discarded. Durable investigation/action records are unchanged.
Do not purge the broker, expire durable jobs, approve financial cards, or republish
financial writes as part of this rollout.

To roll back, stop Beat and the collector worker, restore the previous image pins
and backed-up compose, and restart the previous services. Any remaining control
queue envelopes are stateless and expire; the previous Beat rediscovers durable
work on its original queue. No migration or financial compensation is needed.

## Verification

`backend/tests/test_celery_config.py` uses unique local Redis queues and real
prefork workers started with production compose concurrency/subscription flags.
With both bulk slots occupied and four more scans queued, named, bound-task and
Beat publications for both collectors execute before any bulk slot is released.
Only isolated probe task bodies run. Existing scheduler, worker and publication
idempotency suites continue to cover durable work eligibility and authorization.
