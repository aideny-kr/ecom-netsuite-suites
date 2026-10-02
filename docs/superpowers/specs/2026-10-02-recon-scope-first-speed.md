# Reconciliation scope-first speed specification

Status: implementation in progress. Owner: Codex, GPT-6.1 Sol xhigh. Date: 2026-10-02.
Base: c37d96a2fd51f4a7ffd3fce094c24a4ee72175c0. Infrastructure unchanged.

## Problem and measured baseline

Framework AU is running, but most work does not belong to AU. An October 2
158.503-second checkpoint sample advanced 100 source candidates and 3 AU orders:
37.85 candidates/minute and 1.14 AU orders/minute. At a nearby checkpoint, 687 of
720 candidates (95.4%) were outside AU. Source preparation, repeated source
snapshot/auth reads, checkpoints and speculative NetSuite batches dominate time.
Concurrency alone cannot solve work amplification.

Private measurement artifacts: ~/.codex/artifacts/au-stall-20261002/ and
~/.codex/artifacts/au-scope-speed-20261002/. No provider bodies or credentials
belong in this document or public progress.

## Verified connector facts and research

The Framework authenticated `framework_sync` Solidus endpoint supports bounded
`sync/orders?q[number_in][]=...` reads. A positive + absent-reference probe
returned exactly one requested order in 0.427s. A ten-reference probe returned
exactly ten requested orders in 0.228s with complete pagination metadata.
One corresponding detail read took 0.266s. List/detail projections differ in
lines, payments, adjustments and tax geography: list headers MUST NOT become
financial snapshots, case observations, write preflights or fresh daily proofs.
A second bounded probe returned ten headers in 0.263s and ten serial details in
2.489s; all ten matched on entity, identity, completion and update timestamps.

[Ransack's official search matcher contract](https://activerecord-hackery.github.io/ransack/getting-started/search-matches/)
documents array `in` parameters. Its [predicate documentation](https://activerecord-hackery.github.io/ransack/getting-started/using-predicates/)
warns that unsupported attributes can silently remove filters. Thus every
response must prove exact requested identities and complete bounded metadata;
this customer's live contract, not generic Rails behavior, determines support.
[SQLAlchemy concurrency guidance](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-asyncsession-with-concurrent-tasks)
requires one session per concurrent task. Preserve the existing isolated source
workers; keep run budgets, cursors and finding commits coordinator-owned.

## Architecture

1. Metabase discovers candidates with its existing verified keyset/window filters.
   An omitted replica entity remains unknown; currency and address never infer it.
2. For up to ten unknown-entity order candidates, fetch one authenticated exact
   Solidus header batch. Validate every requested/returned identity, pagination,
   explicit entity field and source version against the discovered candidate.
3. Reject only candidates whose authoritative header proves an entity outside this config’s exact
   allowed entity keys. This is the existing full-detail scope decision; the
   live AU mapping contains only AU’s key, not a catalog of foreign entities.
   Persist a bounded aggregate scope checkpoint. Missing, stale, ambiguous or
   incomplete headers fall back to the existing authoritative detail path.
4. Fetch full details for remaining candidates with the existing bounded source
   concurrency, ETag validation, tenant/credential partitioning and durable snapshots.
   Compare only after the existing full-detail identity/entity checks.
5. Restrict speculative NetSuite batches to the prepared, in-scope references in
   an ambiguous-entity batch. Preserve overlap for other established pipelines.
6. Persist counters distinguishing scope reads, rejected candidates, fallbacks,
   full details, source validations, target batches and completed financial orders.
   Never claim scan candidates/minute as financial reconciliations/minute.

No new infrastructure, schema migration, AI scope classification, expanded
financial authority, credential change or automatic profile activation.
Jev classification remains downstream of deterministic evidence collection.

## Acceptance targets (fixed before implementation)

- Correctness: same in-scope references and financial reports as the detail-only
  baseline; no excluded reference creates a finding or financial observation.
- Provider amplification: at least 90% fewer full-detail reads for a cohort with
  at least 90% foreign-entity candidates; zero NetSuite reads for header-rejected
  references. One scope HTTP read for at most ten candidates.
- Real throughput: at least 3x the measured AU baseline (114 candidates/minute)
  across at least five minutes of comparable orders-phase work. Also report AU
  completed orders/minute, phase, calls and scope ratio without projecting an
  unsupported full-window completion time.
- Recovery: bounded prepaid reads; checkpoint resumes without missing candidates;
  cancellation, disablement, wrong tenant/connection, credential rotation,
  missing/malformed entity, ignored filter, duplicate/extra rows, stale version
  and incomplete pagination cannot create false foreign exclusions.
- Release: focused regression checks, seeded lifecycle CI, full required CI,
  independent exact-base/head T2 review, pinned backend rollout preserving current
  frontend/queues/config, guarded live smoke and post-deploy AU measurements.

## Iteration and evaluation record

Iteration 0: diagnosis and bounded authenticated endpoint probes above. No writes.
Iteration 1: implement scope gate and reference-restricted target batches; compare
reads, reports and resume behavior on seeded mixed-entity fixtures. The first
99 focused checks pass: 100-candidate/95%-foreign cohort uses 5 full details
instead of 100, produces the same five in-scope financial balances, and never
prefetches a foreign reference. Malformed/missing headers preserve full fallback.
Credential rotation during HTTP and config disablement before checkpoint were
found by tests and fixed; the actual request credential fingerprint is captured
before sending. The wider first acceptance run passed236 checks including seeded lifecycle.
Final changed-path checks passed169 plus9 routing/recovery checks after the
serial-path restriction and added continuation/cancellation tests. Fixtures use
the production database clock to avoid future-queued host-clock drift. Current
full CI, independent review and deployed throughput measurement remain pending.
Iteration 2+: respond to measured failures or unmet targets, repeating the affected
checks. Do not weaken evidence/freshness or move targets to manufacture a pass.
Final evaluation and observed limitations will be appended before completion.

Iteration 2, actual-configuration correction: the first fixture included foreign
entity mappings absent from live AU. Before release, the live config check
exposed that valid foreign identities would not be rejected by that version.
The gate now uses the identical full-detail allowed-scope rule, and the fixture
contains only its own entity mapping. Valid explicit different IDs (including
explicit legacy null) are outside scope; missing/malformed ownership remains
unproven and falls back. Numeric targets are unchanged. Iteration 1 candidate
3c695f43 was built for review preparation but never merged/deployed.

Iteration 2 checks:97 routing/runner/concurrency/seeded-lifecycle checks pass
with own-entity-only mappings. An additional live eight-order sample includes
two each from subsidiaries1,2,4,5: all8 list/detail scope identities and versions
match. Headers0.429s vs details1.997s. No customer observations were written.
