# Tenant Memory Graph — Design (Sub-project ①)

> Status: **DRAFT — design approved, pending spec review → implementation plan**
> Date: 2026-06-15
> Tier: **T2** (new tables + migration, multi-tenant + RLS, customer-facing write path, prompt-injection surface)
> Part of: [Self-Improving Memory & Skills program](../../strategy/2026-06-15-self-improving-memory-and-skills-roadmap.md) — Phase 1, sub-project ①

## One-line

A self-serve, plain-English **map of what the assistant has learned about a tenant's business** — concept nodes + inferred relationships, with every learned fact/skill attached as evidence, that the customer can **see, confirm, edit, or delete**, and where curation actually changes agent behavior.

## Why this, why now

The product **already** has a per-tenant, self-improving memory substrate: `TenantLearnedRule` (semantic memory, auto-extracted from chat corrections), `TenantQueryPattern` (procedural/query memory with pgvector retrieval), and a nightly `auto_query_improvement` promotion loop. It works — but it is **invisible** (lives in DB tables), and live auto-learning was deliberately **throttled** (2026-04-09 pattern-poisoning postmortem) precisely because unsupervised growth produced confident-wrong rules with no way to see or catch them.

**The throttle is downstream of the invisibility.** Making memory visible + curatable is therefore not a cosmetic "graph view" — it is the **trust/safety layer** that later lets passive auto-capture (②) be turned back up safely. Visibility before aggression.

① ships value on **existing** data with **zero new confident-wrong risk**: it does not turn up live learning. It makes what's already there visible, organized as a business-concept graph, and curatable — with the customer as the human-in-the-loop.

## Decisions locked during brainstorming

| Question | Decision |
|---|---|
| Audience | **Tenant end-users (self-serve)** — the customer sees & curates their assistant's memory |
| Readability | **Plain-English everything** — every memory item gets a business-language summary; users never see raw SuiteQL |
| Graph nodes | **Business concepts** ("failed orders", "net revenue", a key customer) — memories attach as evidence; edges = inferred concept relationships |
| Storage | **Overlay (Approach A)** — new concept/edge/link tables; existing stores untouched; concepts are a first-class, user-editable layer on top |
| Scope of ① | **Backfill-only** — build the graph from existing memory; live/passive capture deferred to ② |

## Scope boundary

**① includes:** the overlay data model; a plain-English summarization layer; a re-runnable **backfill** extraction that builds the concept graph from today's `TenantLearnedRule` + `TenantQueryPattern` rows; the self-serve graph UI (view + confirm/edit/delete/merge); and the **read-loop** so confirmed concepts inform the agent and rejected ones stop driving answers.

**① explicitly does NOT:** turn up live/passive auto-capture (→ ②); add per-tenant *skills* / knowledge-profile overrides (→ ③, separate program phase); expose or edit the **global** `DomainKnowledgeChunk` system expertise. The existing explicit "remember this" capture (`memory_updater.py`) keeps running unchanged.

## Architecture — overlay (Approach A)

Three new tables, all `tenant_id`-scoped under RLS. **Existing `tenant_learned_rules` and `tenant_query_patterns` are not modified** — all current prompt-injection/retrieval keeps working.

### `tenant_memory_concept` (nodes)
- `id`, `tenant_id` (FK + RLS)
- `name` (canonical concept label)
- `plain_english_summary` (what the assistant knows about this concept)
- `concept_type` — `term` | `metric` | `entity` | `segment`
- `embedding` (Vector(1536)) — for dedup + retrieval
- **trust spine** (see below)

### `tenant_memory_edge` (relationships)
- `id`, `tenant_id` (FK + RLS)
- `source_concept_id`, `target_concept_id` (FK → concept)
- `relation_label` (e.g. "excludes", "rolls up to", "supersedes")
- **trust spine**

### `tenant_memory_link` (evidence)
- `id`, `tenant_id` (FK + RLS)
- `concept_id` (FK → concept)
- `evidence_type` — `learned_rule` | `query_pattern`
- `evidence_id` — id of the row in the existing store (no FK constraint across the legacy table to keep coupling loose; integrity enforced in service layer)
- Unique on (`concept_id`, `evidence_type`, `evidence_id`) for idempotent backfill

### Trust spine (shared columns on concept + edge)
- `origin_session_id` / `origin_message_id` — provenance ("learned from a chat on Jun 3"); nullable, `"backfill"` marker when not derivable
- `confidence` — float 0–1, extractor-assigned
- `review_state` — `proposed` | `confirmed` | `rejected`
- `confirmed_by` (user FK, nullable)
- `use_count`, `last_used_at`
- `created_at`, `updated_at`

Backfilled items start `proposed` so the user curates them into `confirmed`.

## Backfill extraction

A `tenant_memory_extractor` service + a **re-runnable, idempotent** Celery task, run **per tenant** (RLS context set via `set_tenant_context`), **admin-triggered first** — not auto-fanned to all tenants.

For a tenant, it reads the existing `TenantLearnedRule` + `TenantQueryPattern` rows and, per row, makes **one cheap-model call** to:
1. identify the business **concept(s)** the row is about,
2. write the **plain-English summary** (for query patterns: describe what the query *does*, never surface SQL),
3. infer **edges** to other concepts,
4. **link** the source row as evidence.

Concepts are **deduped by embedding similarity** (cosine threshold, tunable) so the same concept extracted from multiple rows collapses into one node. Everything lands `review_state=proposed`, confidence from the extractor, provenance from the source row's origin (or `"backfill"`). **Idempotent** on re-run (unique evidence link + concept embedding match → update, not duplicate). Cost-bounded by batching.

## Self-serve graph UI

A new dashboard nav item (**"Memory"** / "What your assistant knows"), under the `(dashboard)` route group, tenant-scoped via existing auth/RLS.

- **Two panes:** force-directed graph (concept nodes colored by `concept_type`, labeled edges) + detail panel.
- **Node detail:** plain-English summary, type, provenance, attached evidence (each plain-English — **never raw SQL**), confidence, review-state.
- **Actions:** Confirm · Edit (name/summary) · Delete (soft) · Merge duplicates. Edges: confirm/delete.
- **Review queue:** a filter surfacing `proposed` items so the user triages what's new.
- Plain CRUD over the existing `api-client` patterns (no SSE).
- **The graph's exact visual treatment gets a dedicated mockup/design pass before FE build** — not pinned in this architecture spec.

## The read-loop (why curation matters)

This is the payoff that makes ① more than a viewer.

- Confirmed concepts (+ confirmed, high-confidence edges) are injected into the agent prompt **alongside the existing learned-rules injection** (the per-turn, tenant-scoped gather in `orchestrator.py`).
- **`review_state` becomes a filter on both injection and query-pattern retrieval** — a memory the user **deletes/rejects stops driving answers**; a confirmed one is trusted.
- Additive filter on existing paths → low blast radius.

This closes the loop: *curate in the graph → the agent's behavior changes.*

## Guardrails

- RLS tenant isolation on all three tables (CREATE POLICY ... USING `tenant_id = get_current_tenant_id()`), following the `080_learned_rules_rls.py` precedent.
- A user edits only their own tenant's memory.
- **Deletes are soft** (`review_state=rejected`) — preserve audit/provenance.
- The **global** `DomainKnowledgeChunk` (system NetSuite expertise) is never exposed or editable here.
- No raw SQL surfaced to the user anywhere.
- ① does **not** enable live auto-capture (that's ②) — zero new confident-wrong risk.

## Testing (TDD)

- Model + **RLS isolation** tests (cross-tenant read/write denied).
- Extractor unit tests (mocked LLM): concept extraction, dedup by embedding, **idempotent re-run**, plain-English summary never contains SQL.
- Tenant-scoped API CRUD tests (confirm/edit/delete/merge; edge ops).
- **Read-loop tests:** confirmed concept injected; rejected concept excluded from **both** prompt injection **and** query-pattern retrieval.
- FE component tests + **Playwright e2e golden path:** view graph → confirm a node → edit → delete → assert retrieval no longer reflects it.

## Migration / two-DB note

New tables require an Alembic migration applied to **both** databases (Supabase via `.venv/bin/alembic` **and** the local Docker Postgres). Single head; verify `downgrade -1` reversibility (the merge-migration pitfall — re-parent, never merge-head).

## Open items for the implementation plan

- Concept-type taxonomy: start with `term | metric | entity | segment`; confirm during planning.
- Dedup cosine threshold + summarization model choice (cost vs quality).
- Whether merge is v1 or fast-follow.
- Exact injection format in the read-loop (flat list vs concept-organized) — start simple (inject confirmed concept summaries), iterate.

## Tier & gates (T2)

New tables + migration, multi-tenant + RLS, customer-facing write path, prompt-injection surface ⇒ **T2**. Gates: existing CI **+ seeded-tenant e2e** + **blocking multi-angle review** (`code-review-multiangle`) pre-merge.
