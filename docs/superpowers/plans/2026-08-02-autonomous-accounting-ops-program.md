# Autonomous Accounting Ops — program plan

**Goal.** Fully automate end-to-end daily and monthly accounting operations (reconciliation, close, reporting) via scheduled jobs and agentic flows, with memory + reporting tools and read **and write** access to NetSuite and future MCP servers.

**Created** 2026-08-02 · **Basis** 『그래프 엔지니어링』(leaf meta, 2026-08-01) Agent-Graph track, distilled and gap-analysed against the codebase at `7885b15`; adversarially verified (~25 file:line claims re-checked, 1 error found and corrected).
**Doctrine** `.claude/rules/agent-graph.md` — binding for every task below.

---

## 1. Where we actually are

**Rung 1 of 4.** The *read* half is real and well-built. The *write* half does not exist.

Verified present and correct — do not rebuild:

| Capability | Where |
|---|---|
| Deterministic matching + four-bucket classification (no LLM in the number path) | `order_matching_engine.py`, `four_bucket_classifier.py` |
| Advisory confidence explicitly decoupled from status/auto-lock | `confidence_engine.py` |
| Materiality routing ($50 / 1% → needs_review, per-tenant) | `materiality.py`, migration 078 |
| Close lock blocks only what is irreversible; material unreviewed stays open | `close_scope.py:58-70` |
| Pure, side-effect-free autonomy envelope + nightly dry-run | `autonomy_envelope.py`, `recon_envelope_dry_run.py` |
| Two-step HMAC-confirmed mutation (the template to copy) | `mcp/governance.py:200-206`, `registry.py` |
| **Registry-derived allow-list on the one scheduled dispatch path** | `report/recipe.py:49 is_recipe_eligible()` |
| Report auto-refresh failure ladder (degrade at 3, pause at 7, human-only resume) | `report/refresh_service.py` |
| Idempotent redo via `dedupe_key` upsert + cursor watermarks | `ingestion/base.py`, `stripe_sync.py` |
| Independent-model review gate (real four-eyes) | `.claude/workflows/code-review-multiangle.js` |

Verified missing:

| Gap | Evidence |
|---|---|
| **No posting code at all** | zero `createRecord`/journal refs in recon services; trust-model spec: "no posting scaffolding at all" |
| **No durable execution** | zero `acks_late` / `max_retries` / `autoretry_for` in `backend/app/workers/` — a worker death drops the task with no redelivery. `reconciliation_run.py:100-102`: *"the nightly Beat schedule is the retry"* |
| **No compensation / reversal** | trust-model spec D4: *"Nothing exists today"* |
| **Mutation guard is not at the choke point** | `classify_mutation` is called only from `base_agent.py` (:734, :1235). The dispatcher `execute_tool_call` (`chat/tools.py:247`) has **7 callers**; `orchestrator.py:1747`, `orchestrator.py:3513` and `baseline_runner.py:313` reach it without classifying |
| **`recon.approve_match` HITL is prose** | `recon_approve.py:22-23` is a docstring, not a check |
| **Workers are invisible** | `sentry_sdk.init` exists only at `main.py:54` inside the FastAPI lifespan; the Celery entrypoint never imports it. 15 of 16 Beat jobs notify no one |
| **No negative labels** | `rejected` / `investigating` are declared statuses that no code path ever sets |

Corrections to earlier analysis, verified on `main`:
- `read_only_mode` is **not** a dead field — enforced at `unified_agent.py:707`, `suiteql_agent.py:403`, `prompt_template_service.py:284`. It is simply absent from `evaluate_tool_call`.
- A scheduled path **does** dispatch tools today (`report-auto-refresh-hourly` → `refresh_service.py:208`), and it is **correctly guarded** by `is_recipe_eligible`. It is the model to generalise, not a hole.

---

## 2. What we are deliberately not building

The book's own verdict is that graphs are overkill in most cases. Rejected with reasons:

- **Graph DB for recon/ledger** — tables win on constraints + transactions; our matching is 1-hop on `order_ref`; graph advantage starts at ≥3 hops.
- **GraphRAG for the chat agent** — the book's own case study: fact-lookup traffic *degraded*, satisfaction 4.1→3.6. Our mix is overwhelmingly fact-lookup and our north star is the vs-MCP benchmark on exactly those.
- **Multi-hop traversal over `TenantMemoryEdge`** — edges are write-only display data today; the book's threshold (~1,000 facts + measured token-cost crossover) is nowhere near met. Instrument first; the migration is ~2 days when actually needed.
- **State-graph framework (LangGraph-style) for the close pipeline** — breakeven is ~7 conditions or expensive late-step failure; ours is linear and cheap to re-run. The one place irreversibility moves the crossover — posting — gets durability primitives directly, without the framework.
- **Critical-path orchestrator** — the author built one twice and threw it away twice (8% measured gain vs his own 30% bar). Our Beat clock layering already *is* layer scheduling.
- **Full event sourcing + hash-chained audit** — author's own honesty: 6 years, zero tampering attempts, "I cannot claim it worked." Bitemporal dispositions cover the real need far cheaper.
- **Entity-resolution machinery** — matching is deterministic on a tenant-configurable `order_ref`; the documented failure was date-windowing, not alias fragmentation.

**The only knowledge-graph idea on the critical path is a bitemporal dispositions table — a plain Postgres table with two time columns.**

---

## 3. Tracks

Ordered only where a real data dependency exists (Arrow Test). O, G, A run in parallel with P.

### Track O — make failure visible *(no dependencies · effort S)*
Unattended means nobody is watching by definition.
1. `sentry_sdk.init` in the Celery `worker_init` signal.
2. One daily `ops_digest` Beat task: failed jobs + connections in `error` + DLQ backlog → single digest on the existing email plumbing. Hard page only past a backlog threshold.
3. Convert the bare `@celery_app.task` decorators to `base=InstrumentedTask` so every failure leaves a `jobs` row.

*Risk if skipped:* the documented failure mode (NetSuite refresh token dies → sync freezes → no alert) recurs during a real close.

### Track G — move the guard to the choke point *(no dependencies · effort M)*
1. Mutation classification into `execute_tool_call` so every present and future caller inherits it. **Preserve the existing asymmetry**: the non-streaming path today blocks mutations outright — centralising must not silently relax that to "confirmable."
2. Give `recon.approve_match` the two-step HMAC pattern that `workspace.deploy_sandbox` already uses.
3. Enforce `read_only_mode` inside `evaluate_tool_call`.
4. CI test asserting no dispatcher path reaches a mutation tool without a token.

### Track P — the posting ladder *(the only true chain)*
Each stage reads the previous stage's output.

**P1 · Bitemporal dispositions** *(M)* — `recon_dispositions` keyed on `(tenant_id, order_ref)`, **not** `run_id`; `valid_from/valid_to`, insert-only corrections, single-valued closing rule, half-open reads. Runs become observations; dispositions become facts. This is the trust-model spec's own blocking precondition for enforcement, and the one thing that cannot be retrofitted.

**P2 · Envelope enforcement** *(M)* — reads dispositions to dedupe candidates across runs; flips dry-run into real auto-approval of **DB status only**. Still zero NetSuite writes.

**P3 · Rung 2 — HITL posting** *(L)* — the first code that writes to NetSuite. Ships as one PR containing: `netsuite_posting_log` (work-derived idempotency key, `started→posted|failed`, kind/tries/first_failed_at/trace_id), external-ref stamping + stale-`started` sweeper, replayable DLQ, approval fingerprint check, reversing-JE generator registered before execute, `reconcile → draft → post → notify` ordering, and a staging `kill -9` mid-post drill as a required T2 gate step.

**P4 · Rung 3 — unattended posting** — gated on months of Rung 2 outcome labels (§4) and Track O running through a full close. Widen the envelope only by adding veto rules after incidents, never by loosening thresholds.

### Track A — scheduled agentic flows *(depends on G only)*
A standard run envelope for the first scheduled agent: budgets (tool calls, tokens, NetSuite API calls, query cost) persisted on the `Job` row and checked at spend granularity; termination reason enum into `jobs.result_summary`. Generalise `is_recipe_eligible`'s registry-derived allow-list into the reusable shape for any scheduled tool caller.

---

## 4. The hardest unknown — start now, not at Rung 3

Rung 3 rests on a claim nothing today can support: *"deterministic zero-variance matches are wrong less than 1-in-N times."*

The system collects **only positive labels**. There is no reject or dispute action anywhere, so a false positive leaves no trace, the advisory scorer is calibrated on a zero-rejection corpus, and no data stream exists that could ever produce the error rate. This is an evidence problem, not an architecture problem, and it has a months-long lead time.

Two actions must start before P3, not after:
1. **Ship a real reject/dispute action** so negative labels begin accumulating now.
2. **Get a controller's written answer on frozen-period reversals.** When a wrong auto-post is found, it will usually be after the period hard-freezes — and the compensating entry then collides with the close-lock invariant the system itself enforces. That is an accounting policy question the saga pattern does not answer.

---

## 5. Definition of done per PR

Tier by `CLAUDE.md`. Everything in Track P and Track G is **T2**. In addition, for any unattended-execution PR:

- [ ] Rules live in code on the call path, not in prose
- [ ] Allow-list, derived from a registry so it cannot drift
- [ ] Terminates with a reason enum; budgets cover cost, not just calls
- [ ] Irreversible step is last; compensation registered before execute
- [ ] Idempotency key derived from the work; side-effect log written before the call
- [ ] Failure reaches a human via the digest, and the DLQ entry is replayable from the record alone
- [ ] Recovery proven by a `kill -9` drill, not by a passing unit test
