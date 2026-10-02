# Reconciliation scope-first speed specification

Status: implementation verified locally; final release gates and live speed measurement pending.
Owner: Codex, GPT-6.1 Sol xhigh, standard processing. Date: 2026-10-02.
Base: c37d96a2fd51f4a7ffd3fce094c24a4ee72175c0. Infrastructure unchanged.

## Problem and baseline

Framework AU is active but spends most work on other entities. An October 2
158.503-second checkpoint sample advanced100 source candidates and3 AU orders:
37.85 candidates/minute and1.14 completed AU orders/minute. A nearby checkpoint
had687 of720 candidates outside AU (95.4%). Per-candidate source reads,
authorization/snapshot/checkpoint round trips and speculative NetSuite preparation
amplify the cost. Current AU scope contains only its own entity key.

Private evidence: ~/.codex/artifacts/au-stall-20261002/ and
~/.codex/artifacts/au-scope-speed-20261002/. No credentials or provider bodies
belong in this spec or public progress.

## Connector research and measured contract

The authenticated Framework `framework_sync` endpoint supports bounded exact
`sync/orders?q[number_in][]=...` reads. Positive+absent-reference probe: exactly
one requested order,0.427s. Ten-reference probe: exactly ten orders,0.263s;
ten serial details2.489s,10/10 entity/identity/timestamp parity.

A separate mixed sample includes two orders each from subsidiaries1,2,4,5:
8/8 list/detail entity, identity, completion and update timestamps match.
Headers0.429s; details1.997s. A final read-only execution of the actual scope
validator qualifies all8 headers and finds6 outside live AU scope; all8 contain
the entity field. Raw null count0: no live-null serializer parity claim is made.
Null handling preserves the existing detail rule and is regression-tested.

List headers omit lines, payments, adjustments and tax geography. They MUST NOT
become financial snapshots, case observations, write preflights or refreshed
financial evidence. In-scope orders still need complete detail.

[Ransack's search matcher contract](https://activerecord-hackery.github.io/ransack/getting-started/search-matches/)
documents array `in` parameters. Its [predicate documentation](https://activerecord-hackery.github.io/ransack/getting-started/using-predicates/)
warns unsupported attributes may silently remove filters, so every response must
prove exact requested identities and complete pagination. This customer's live
contract determines support; generic Rails behavior is insufficient.
[SQLAlchemy concurrency guidance](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-asyncsession-with-concurrent-tasks)
requires one session per concurrent task. Existing isolated source workers and
the coordinator's exclusive ownership of budgets/cursors are preserved.

## Final architecture

1. Metabase discovers candidates through existing keyset/window contracts.
   Missing replica entity remains unknown; address/currency never infer ownership.
2. Choose only the read strategy from observed scope mix. Once10 candidates have
   been processed/excluded/rejected, use scope-first when foreign work exceeds
   in-scope work. Cold profiles get one bounded header probe; an owned majority
   retains existing overlap and avoids further header overhead. Legacy-owning
   profiles retain their existing path. Current Inc uses an explicit entity ID,
   so the selector does not assume Inc owns the legacy key.
3. For up to10 unknown-entity candidates on a foreign-dominated feed, fetch one
   authenticated exact header batch. Check requested/returned identities,
   positive unique IDs, complete pagination, explicit valid entity and aware
   not-future source versions/completion. Known newer discovery versions require
   detail fallback, except verified fresh same-millisecond serialization.
4. Reject only a qualified authoritative entity outside this config's exact
   allowed keys, using the identical full-detail scope rule. Foreign mappings
   need not exist. Missing/stale/malformed/ambiguous headers retain full detail.
5. Persist one bounded aggregate scope/cursor checkpoint. Preserve conservative
   order budgets. An interrupted uncommitted checkpoint re-reads and re-pays;
   it cannot lose a candidate or manufacture a finding.
6. Prepare remaining full details through existing up-to4 isolated workers,
   ETag/credential/version invalidation and durable snapshots. For ambiguous
   foreign-dominated cohorts, NetSuite prefetch includes only full-detail-confirmed
   in-scope references. Mostly owned feeds preserve existing provider overlap.
7. List-endpoint non-authorization SourceReadError disables this optional read
   for the invocation and falls back to normal paid detail recovery. Authentication,
   scope/connection authorization, lease and database errors remain blocking.
   Optional list failures do not consume the detail retry budget.
8. Counters separate header reads/candidates/rejections/fallbacks, full details,
   source validations, target batches and completed financial orders. Header
   hits never refresh financial timestamps. Jev remains downstream classification.

No migration, infrastructure, environment, queue, credential, financial authority
or accounting-profile activation changes. Current frontend is preserved.

## Acceptance targets (fixed before implementation)

- Same in-scope references and monetary reports as detail-only execution;
  no excluded reference creates a financial finding/observation.
- At least90% fewer full-detail reads for a cohort with at least90% foreign
  candidates; zero NetSuite reads for header-rejected references. Max10 per read.
- Real AU orders-phase throughput at least3× baseline:114 candidates/minute
  across at least5 minutes of comparable work. Report completed AU orders/minute,
  calls and scope ratio separately. No full-day ETA inferred from HTTP timings.
- Tenant/connection/credential/version/disablement/lease/cancellation and durable
  continuation tests cannot produce false exclusions or lost candidates.
- Full required CI and seeded lifecycle; independent exact-base/head T2 review;
  six-service immutable backend rollout preserving frontend/schema/Env/Cmd/queues;
  guarded dedicated-tenant live smoke and actual AU speed measurement.

## Verified iterations and release evaluation

Iteration0: baseline diagnosis and bounded endpoint research; no customer writes.
Iteration1: initial gate and118-related-path foundation (first wider236 checks).
Actual-config inspection caught the incorrect assumption of foreign mappings
before deployment. Initial candidate3c695f43 was never merged/deployed.
Iteration2: exact allowed-scope rule with own-entity-only fixtures;97 checks pass;
mixed live parity above. Candidateeb2d0547 received actual Opus5.5High eight-angle
review with no blocker/major, but is obsolete after subsequent changes.
Iteration3: preserve mostly-owned overlap using actual scope mix; optional-list
failure fallback and preparation restriction reset address review findingsF1/F2.
Final118 checks PASS include monetary100.00 parity, missing refund proof remaining
incomplete,95%foreign cohort100→5 full details, zero foreign target prefetch,
config disablement, rotation, cancellation, budget continuation, list transport/
HTTP/invalid-response fallback, blocking auth failure, cold/warm explicit Inc
and legacy overlap, header validators and seeded lifecycle. Test fixtures use
production PostgreSQL claim time to avoid host-clock future-queue drift.

Current source hashes, test selections and actual completed results are recorded
in the private iteration3 test receipt; final exact-head CI remains authoritative.
Post-deploy measurement and final evaluation will be linked from STATE/handoff.
Targets will not be weakened to manufacture a pass.
