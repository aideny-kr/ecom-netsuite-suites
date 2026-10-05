# Reconciliation recovery after reconnection

Framework Inc, AU, UK and BV stopped at the native reader's connection-status
gate on October 3–4. Their stored failure was `netsuite_invalid_connection`,
not HTTP 401. Authentication recovered on October 4, but no daily continuation
followed: both the scheduler's candidate SQL and `auth_stop` only recognized
`netsuite_upstream_http_401`. Coverage remained October 1.

The fix admits the two pre-HTTP authentication stops (`invalid_connection` and
`authentication_failed`) to the existing bounded authentication recovery path.
One shared code list drives candidate discovery and checkpoint eligibility.
The existing minute collector is the wake-up mechanism; no callback task,
additional scheduler, provider probe, migration or infrastructure is added.

Eligibility still requires the latest scheduled read, an unresolved failure
owned by that invocation, unchanged connection/account/subsidiary, active
connection status, and a newer sufficiently valid credential. Recovery retains
the original window, pending references, cursors and evidence root. Config
locks, work keys, publication reservations and worker leases prevent duplicate
ownership. Only one authentication resume is allowed in a lineage; existing
part, elapsed-cycle and cost limits remain in force. A hard stop or expired
lineage stays blocked and is visible in Ops status; this change does not grant
unlimited retries or automatic financial writes.

## Acceptance

- While the connection is unhealthy, repeated collector ticks create no child
  and make no provider request to test recovery.
- After a newer credential is committed, an eligible checkpoint is discovered
  on the existing minute tick, before the next daily cutoff. It is published
  on the daily queue and retains its original scope and cursor.
- Repeated/concurrent recovery creates one child. New daily work, paused
  schedules, foreign tenants, changed scopes, expired credentials, rejected
  tokens, 403 responses and exhausted limits remain fenced.
- The resumed reader must validate evidence before resolving the diagnostic;
  credential recovery alone does not mark any date verified.
- Seeded lifecycle, financial approval/audit and tenant isolation checks pass.
- Required independent T2 review and CI pass on the actual release base/head.
- Staging's six backend services receive one immutable reviewed image;
  environment, queues, schema, frontend and Redis remain unchanged.
- Observe actual automatic continuations from the four stranded Framework
  parents, then advancing counters or completed windows with resolved read
  failures. Record coverage separately; a queued job is not completed catch-up.

## Development lesson

The earlier check stopped at authentication recovery rather than checking the
consumer outcome. The acceptance boundary for a dependency repair must include
the stopped consumer's discovery, dispatch, execution and evidence progression.
Executable coverage now follows real reader rejection → stopped runner →
reconnection → scheduler continuation → resumed runner completion. Include the
failure codes observed on the customer path in the review packet, rather than
testing only the presumed provider response. Release evidence must distinguish
healthy connection, dispatched job, advancing checkpoint and verified coverage.

Implementation model: GPT-6.1 Sol xhigh, standard processing, one lead.
Final tests/review/CI/deployment and live evidence are recorded outside the
candidate under `~/.codex/artifacts/recon-reconnect-recovery-20261004/` and in
the workspace `STATE.md`; this source document is not an assertion of rollout.
