# Bounded refund-read resilience

The October4 reconnect fix resumed all four Framework schedules. AU/UK completed
October3 coverage. Inc advanced1,277 orders, then repeatedly timed out on one
refund lookup; BV also exhausted its finite refund-read retries. Both stopped
with `netsuite_read_timeout` at `netsuite_refunds`, not another auth failure.
Current production MCP is active; the daily engine uses its selected REST connection.

The native HTTP reader had a25-second read-idle deadline for every SuiteQL call.
Refund collections have existing overall160-second single /90-second bulk limits.
The candidate opts only refund collections into a60-second SuiteQL read-idle
allowance. Record reads and ordinary/order/dependency SuiteQL keep25 seconds;
record metadata keeps120. Connect/write/pool deadlines, response size, API-call
budgets, three inline/delayed retries, daily cutoffs, part/cycle caps, auth/tenant
scope, financial proof, cache freshness and financial-write authority are unchanged.
Timeout selection accepts only code-owned25/60 integer values before authorization.
Outer deadlines still cancel stalled calls and release the wire slot/client.

HTTP read timeouts carry only a validated operation enum, elapsed milliseconds
and configured idle timeout. The persisted failure and shared read-only Ops API
expose that context. SQL, URL, headers, tokens and record/customer identifiers are
excluded. Overall collection cancellation remains a phase-level diagnostic;
this change does not claim every timeout carries a per-query context.

## Evidence and decisions

Bounded read-only probes of the exact stopped references on October5 morning:
Inc's original path completed four calls in roughly6.2 seconds. BV's calls also
completed, then returned `processed_refund_unlinked`, which must remain unproven
for human review. These are latency diagnostics using historical target identity,
not fresh financial certificates. Failed-reference identifiers are hashed.
Three old versus split graph comparisons per entity preserved every relationship,
but split calls were slower for Inc (5.080s versus4.214s); no split-query code shipped.
A UNION trial returnedHTTP400 on this selected REST surface and was discarded.
Oracle recommends avoiding OR predicates, but that advice does not override
this customer's actual parity/performance evidence.

This candidate addresses premature per-request timeouts. It is not a measured
throughput gain or a claim that provider timeouts cannot recur. Do not reset
exhausted run state, remove refund proof or mark any missing date verified.
The next daily cutoff resumes budget-stopped runs through the existing checkpoint
path, retaining evidence root, pending references and cursor; no September restart.

## Acceptance

- Controlled transport demonstrates the refund query may complete when it needs
  more than25 but less than60 seconds; money/ownership proof is identical.
- Single160 /bulk90 overall deadlines cancel/drain the request; spend remains
  bounded and no sibling/orphan HTTP call survives.
- Ordinary query/record deadlines and auth/tenant/financial gates remain intact.
- Invalid timeout input is rejected before credential or wire access.
- Diagnostic context strips unexpected fields and rejects invalid types/enums.
- Focused319 tests including9 seeded lifecycle cases passed19.80s; full CI,
  independent T2 review, image rollout, guarded live UAT and real daily progress
  are separate pending gates until evidenced on the reviewed revision.
- Observe automatic scheduled execution and actual completed dated coverage;
  distinguish a queued retry, processed case and daily completion.

Implementer: GPT-6.1 Sol xhigh, standard processing, one lead. Exact revision,
actual reviewer model, CI, deployment and live evaluation are recorded under
`~/.codex/artifacts/recon-refund-read-resilience-20261005/` and workspaceSTATE.md.
