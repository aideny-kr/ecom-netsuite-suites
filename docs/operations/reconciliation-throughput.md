# Reconciliation collection throughput

The runner now stores bounded `timing_ms` counters (`calls`, `total`, `max`) and
`active_ms` in its existing progress checkpoint. They reset for each continuation
slice. Times measure collection execution, excluding time spent queued. Read-stage
times include authorization and retries; a retry's reservation also appears in the
state counters, so do not treat all counters as a disjoint sum. The latest progress
save omits its own duration until the next checkpoint. No payloads or references
are included in the counters.

Collection reuses HTTP connections within an exact tenant, connection, account and
credential partition. Every reader still checks active connection scope and obtains
its current token. Rotation starts a separate client, including a separate cookie
jar. The pool closes at slice termination. This is transport reuse, not financial
evidence caching. Guards and execution preflights retain their independent reads.

Matching orders without a configured Celigo error step skip no-op proposal
preparation and its duplicate finding persistence. They still receive a final
finding, dependency inventory, case observation and existing audit behavior.
Feature enablement remains a fresh database check before every reservation.

Collection tasks also retain one task-owned database connection across commits.
This removes repeated pool checkout/ping overhead without opening an enclosing
transaction. Commits and SET LOCAL transaction boundaries are unchanged. The
connection and disposable engine close on success, failure and cancellation.
Other workers keep their existing session behavior.

## Daily execution lane

Publishing defaults to `recon` for compatibility with existing deployments. To
reserve one of the existing two scan slots for scheduled runs:

1. Warm-drain running collection workers and pause Beat during the change.
2. Enable compose profile `recon-daily`, set `RECON_BULK_CONCURRENCY=1` in the
   compose interpolation environment, and set `TRANSACTION_OPS_DAILY_QUEUE=recon-daily`
   in the API/worker/collector environment. The first setting is a compose setting;
   placing it only in a container's `env_file` does not interpolate the compose command.
3. Start the bulk worker at concurrency 1 and `worker-daily` at concurrency 1,
   then restart producers and Beat on the same reviewed image. Verify queue
   subscriptions and concurrency before resuming publication. Other worker groups
   retain their existing capacities.
4. Keep that compose profile/environment active for subsequent deploys and rollbacks.
   The release guard includes `worker-daily` whenever the resolved compose defines it.

New scheduled runs, republished pending runs and scheduled continuations use the
daily lane. Manual/custom scans remain bulk work. Single-order recovery retains the
actions queue. The bulk worker may help the daily queue when capacity is available;
historical work cannot consume the dedicated daily worker. Old broker deliveries
remain lease-fenced during transition; never purge unrelated queues.

Revert publication to `recon` before removing the daily consumer. Drain any pending
daily messages before stopping it, then restore bulk concurrency to 2. Daily
isolation is a fairness guarantee, not a promise of increased aggregate throughput.

## Remaining evidence reuse boundary

The existing Solidus conditional-response cache is separate from NetSuite.
NetSuite collection does not yet have a cross-run, change-validated financial
snapshot cache. A transaction's header timestamp alone cannot establish that
refund applications, custom requests, graph edges, deleted dependencies, currency
or period metadata are unchanged. Native change inventories nominate rechecks;
they do not themselves prove cached money is current.

Before enabling that cache, require account/role-specific revision coverage for
every monetary dependency, detection of removed links, explicit completeness,
credential/configuration/rule partitioning and original observation timestamps.
Preserve fresh execution preflights. Use per-stage timings to select batching work;
measure full slices before claiming an end-to-end multiplier.
