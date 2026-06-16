# Tenant Memory Graph (①) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-serve, plain-English graph of per-tenant business concepts (overlay over the existing `TenantLearnedRule` + `TenantQueryPattern`), with a trust spine and a read-loop so confirmed concepts inform the agent and rejected ones stop driving answers.

**Architecture:** Three new tenant-scoped, RLS-isolated tables (`tenant_memory_concept`, `tenant_memory_edge`, `tenant_memory_link`) layered over the existing learning tables (never modified). A backfill extracts concepts from existing rows; a self-serve reactflow UI lets the customer confirm/edit/reject; a read-loop injects only `review_state='confirmed'` concepts into the prompt, mirroring the learned-rules subsystem end-to-end.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + Alembic + pgvector, Celery (InstrumentedTask), Next.js 14 + @tanstack/react-query + reactflow, anthropic fast-model extraction + OpenAI embeddings.

**Tier:** T2 (new migration + RLS/tenant-scoping + customer write-path + prompt-injection surface). Gates: seeded-tenant e2e + blocking `code-review-multiangle` pre-merge.

**Spec:** `docs/superpowers/specs/2026-06-15-tenant-memory-graph-design.md`

---

## LOCKED canonical schema (reconciles the research drafts)

The research agents diverged on column names. **This schema is authoritative for every task below** — do not reintroduce `label`/`body`/`source_kind`.

**`tenant_memory_concept`** — id, tenant_id(FK CASCADE, idx), `name` String(255), `summary` Text (plain-English), `concept_type` String(50) null, `embedding` Vector(1536) null, `review_state` String(20) default `'pending'` (`pending|confirmed|rejected|merged`), `confidence` Numeric(4,3) null, `origin_session_id` UUID null, `origin_message_id` UUID null, `confirmed_by` UUID FK users null, `merged_into_id` UUID FK self null, `use_count` Integer default 0, `last_used_at` DateTime null, created_at/updated_at (TimestampMixin).

**`tenant_memory_edge`** — id, tenant_id(FK CASCADE, idx), `source_concept_id` FK concept CASCADE, `target_concept_id` FK concept CASCADE, `relation` String(100), `review_state` String(20) default `'pending'`, created_at/updated_at.

**`tenant_memory_link`** (evidence) — id, tenant_id(FK CASCADE, idx), `concept_id` FK concept CASCADE, `source_table` String(50) (`tenant_learned_rules|tenant_query_patterns`), `source_id` UUID (no cross-table FK; integrity in service), created_at. **UNIQUE `uq_tenant_memory_link_source` on (tenant_id, source_table, source_id)** — this is the backfill idempotency key.

**Dedup (v1):** concepts dedup by **normalized exact name** within a backfill run (`embedding` stored for future fuzzy dedup, not used as the dedup key in v1). Evidence idempotency lives on the link table's unique constraint.

**Render-site note:** two candidate injection sites surfaced — `base_agent.py:_build_learned_rules_block` (lines 32-42, called 620/925) and the `UnifiedAgent.system_prompt` @property (unified_agent.py:523-532). **Task 5, Step 1 is to grep for the LIVE learned-rules render site and mirror exactly that one.** Do not assume; verify.

---

## File ownership (prevents subagent conflicts)

| Task | Owns (create/modify) |
|---|---|
| 1 | `backend/app/models/tenant_memory_*.py`, `models/__init__.py`, `alembic/versions/085_*.py`, `tests/test_tenant_memory_models.py` |
| 2 | `backend/app/schemas/tenant_memory.py`, `services/tenant_memory_service.py`, `api/v1/tenant_memory.py`, `api/v1/router.py`, `tests/test_tenant_memory_service.py`, `tests/api/test_tenant_memory_api.py` |
| 3 | `backend/app/services/chat/tenant_memory_extractor.py`, `config.py`, `tests/test_tenant_memory_extractor.py` |
| 4 | `backend/app/workers/tasks/tenant_memory_extract_backfill.py`, `workers/celery_app.py`, append to `api/v1/tenant_memory.py`, `tests/test_tenant_memory_backfill.py` |
| 5 | `backend/app/services/memory_graph_service.py`, `services/chat/orchestrator.py`, render-site file (base_agent.py or unified_agent.py), `prompt_cache.py`, `tests/test_memory_graph_injection.py` |
| 6 | `frontend/src/hooks/use-memory-graph.ts`, `app/(dashboard)/memory/page.tsx`, `components/memory/*`, `lib/constants.ts`, `components/sidebar.tsx`, `package.json` |
| 7 | `frontend/e2e/memory.spec.ts`, seeded-tenant e2e, the T2 review gate |

---

### Task 1: Models + RLS migration

**Files:**
- Create: `backend/app/models/tenant_memory_concept.py`, `tenant_memory_edge.py`, `tenant_memory_link.py`
- Modify: `backend/app/models/__init__.py` (import + `__all__` for all 3)
- Create: `backend/alembic/versions/085_tenant_memory_graph.py`
- Test: `backend/tests/test_tenant_memory_models.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_tenant_memory_models.py
def test_models_importable_and_registered():
    from app.models import TenantMemoryConcept, TenantMemoryEdge, TenantMemoryLink
    assert TenantMemoryConcept.__tablename__ == "tenant_memory_concept"
    assert TenantMemoryEdge.__tablename__ == "tenant_memory_edge"
    assert TenantMemoryLink.__tablename__ == "tenant_memory_link"
    # trust spine present
    cols = TenantMemoryConcept.__table__.columns.keys()
    for c in ("name", "summary", "concept_type", "embedding", "review_state",
              "confidence", "confirmed_by", "use_count"):
        assert c in cols
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /Users/aidenyi/projects/ecom-netsuite-suites/backend && .venv/bin/python -m pytest tests/test_tenant_memory_models.py -x -q`
Expected: FAIL (ImportError — models don't exist).

- [ ] **Step 3: Create the three models** (mirror `tenant_learned_rule.py` + `metric_definition.py` for the `Vector` column)

```python
# backend/app/models/tenant_memory_concept.py
import uuid
from datetime import datetime
from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

class TenantMemoryConcept(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tenant_memory_concept"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    concept_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    embedding = mapped_column(Vector(1536), nullable=True)  # bare — matches metric_definition.py
    review_state: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    origin_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    origin_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant_memory_concept.id", ondelete="SET NULL"), nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

```python
# backend/app/models/tenant_memory_edge.py
import uuid
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

class TenantMemoryEdge(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tenant_memory_edge"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    source_concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant_memory_concept.id", ondelete="CASCADE"), nullable=False)
    target_concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant_memory_concept.id", ondelete="CASCADE"), nullable=False)
    relation: Mapped[str] = mapped_column(String(100), nullable=False)
    review_state: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
```

```python
# backend/app/models/tenant_memory_link.py
import uuid
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

class TenantMemoryLink(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tenant_memory_link"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_table", "source_id", name="uq_tenant_memory_link_source"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    concept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant_memory_concept.id", ondelete="CASCADE"), nullable=False)
    source_table: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
```

Then add to `backend/app/models/__init__.py` (mirror line 36 import + line 93 `__all__` for `TenantLearnedRule`):
```python
from app.models.tenant_memory_concept import TenantMemoryConcept
from app.models.tenant_memory_edge import TenantMemoryEdge
from app.models.tenant_memory_link import TenantMemoryLink
# and add "TenantMemoryConcept", "TenantMemoryEdge", "TenantMemoryLink" to __all__
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/aidenyi/projects/ecom-netsuite-suites/backend && .venv/bin/python -m pytest tests/test_tenant_memory_models.py -x -q`
Expected: PASS.

- [ ] **Step 5: Confirm single alembic head**

Run: `cd /Users/aidenyi/projects/ecom-netsuite-suites/backend && .venv/bin/alembic heads`
Expected: exactly `084_reports (head)`. (If two heads print, STOP — re-parent/linearize; never add a merge migration — it fails the deploy `downgrade -1` gate.)

- [ ] **Step 6: Write the migration** (mirror `080_metric_definitions.py` create+HNSW + `080_learned_rules_rls.py` RLS; use the reports-style FORCE policy)

```python
# backend/alembic/versions/085_tenant_memory_graph.py
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "085_tenant_memory_graph"
down_revision = "084_reports"   # verified single head
branch_labels = None
depends_on = None

_TABLES = ("tenant_memory_link", "tenant_memory_edge", "tenant_memory_concept")  # drop order

def upgrade() -> None:
    op.create_table(
        "tenant_memory_concept",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("concept_type", sa.String(50), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("review_state", sa.String(20), server_default="pending", nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("origin_session_id", sa.UUID(), nullable=True),
        sa.Column("origin_message_id", sa.UUID(), nullable=True),
        sa.Column("confirmed_by", sa.UUID(), nullable=True),
        sa.Column("merged_into_id", sa.UUID(), nullable=True),
        sa.Column("use_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["merged_into_id"], ["tenant_memory_concept.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tenant_memory_concept_tenant_id", "tenant_memory_concept", ["tenant_id"])
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tmc_embedding ON tenant_memory_concept "
        "USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)"
    )
    op.create_table(
        "tenant_memory_edge",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("source_concept_id", sa.UUID(), nullable=False),
        sa.Column("target_concept_id", sa.UUID(), nullable=False),
        sa.Column("relation", sa.String(100), nullable=False),
        sa.Column("review_state", sa.String(20), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_concept_id"], ["tenant_memory_concept.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_concept_id"], ["tenant_memory_concept.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tenant_memory_edge_tenant_id", "tenant_memory_edge", ["tenant_id"])
    op.create_table(
        "tenant_memory_link",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("concept_id", sa.UUID(), nullable=False),
        sa.Column("source_table", sa.String(50), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["concept_id"], ["tenant_memory_concept.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "source_table", "source_id", name="uq_tenant_memory_link_source"),
    )
    op.create_index("ix_tenant_memory_link_tenant_id", "tenant_memory_link", ["tenant_id"])

    # RLS — reports-style: USING + WITH CHECK + FORCE (app role is NOT BYPASSRLS on Supabase)
    for tbl in ("tenant_memory_concept", "tenant_memory_edge", "tenant_memory_link"):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {tbl}_tenant_isolation ON {tbl} "
            f"USING (tenant_id = get_current_tenant_id()) "
            f"WITH CHECK (tenant_id = get_current_tenant_id())"
        )

    # Seed the memory.manage permission + grant to admin (mirror 080_metric_definitions.py:74-86)
    op.execute("""
        INSERT INTO permissions (id, codename, description)
        VALUES (gen_random_uuid(), 'memory.manage', 'Manage tenant memory graph')
        ON CONFLICT (codename) DO NOTHING
    """)
    op.execute("""
        INSERT INTO role_permissions (role, permission_id)
        SELECT 'admin', id FROM permissions WHERE codename = 'memory.manage'
        ON CONFLICT DO NOTHING
    """)

def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE codename='memory.manage')")
    op.execute("DELETE FROM permissions WHERE codename='memory.manage'")
    for tbl in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_tenant_isolation ON {tbl}")
        op.drop_table(tbl)
```

> NOTE: verify the exact `permissions`/`role_permissions` column names against `080_metric_definitions.py:74-86` before running; copy that migration's INSERT verbatim if they differ.

- [ ] **Step 7: Apply to BOTH databases + verify reversibility**

Run (Supabase — from backend/, source root .env so it doesn't hit localhost):
```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/backend && set -a && source ../.env && set +a && .venv/bin/alembic upgrade head
```
Run (local docker): `docker exec ecom-netsuite-suites-backend-1 alembic upgrade head`
Run (reversibility — the deploy gate): `cd backend && .venv/bin/alembic downgrade -1 && .venv/bin/alembic upgrade head`
Expected: clean up/down/up, no "Ambiguous walk".
**Do NOT apply to shared staging Supabase pre-merge** (orphan-migration deploy block) — use a Supabase branch DB.

- [ ] **Step 8: Commit**

```bash
git add backend/app/models/tenant_memory_*.py backend/app/models/__init__.py \
  backend/alembic/versions/085_tenant_memory_graph.py backend/tests/test_tenant_memory_models.py
git commit -m "feat(memory): tenant memory graph models + RLS migration (085)"
```

---

### Task 2: CRUD service + schemas + router

**Files:**
- Create: `backend/app/schemas/tenant_memory.py`, `backend/app/services/tenant_memory_service.py`, `backend/app/api/v1/tenant_memory.py`
- Modify: `backend/app/api/v1/router.py` (register)
- Test: `backend/tests/test_tenant_memory_service.py`, `backend/tests/api/test_tenant_memory_api.py`

- [ ] **Step 1: Write failing service + API tests** (mirror `tests/api/test_metrics_api.py` fixtures: client/admin_user/member_user/admin_user_b/db). Cover: list graph; get-one→404 for cross-tenant id; PATCH confirm requires `memory.manage` (member→403); DELETE is soft (row remains, `review_state='rejected'`); cross-tenant isolation.

```python
# backend/tests/api/test_tenant_memory_api.py (excerpt)
async def test_patch_confirm_requires_permission(client, member_user_headers):
    r = await client.patch("/api/v1/tenant-memory/concepts/" + str(uuid4()),
                           json={"review_state": "confirmed"}, headers=member_user_headers)
    assert r.status_code == 403

async def test_delete_is_soft(client, admin_user_headers, seeded_concept_id):
    r = await client.delete(f"/api/v1/tenant-memory/concepts/{seeded_concept_id}", headers=admin_user_headers)
    assert r.status_code == 204
    # row still exists, review_state flipped
    g = await client.get("/api/v1/tenant-memory?review_state=rejected", headers=admin_user_headers)
    assert any(c["id"] == seeded_concept_id for c in g.json()["concepts"])

async def test_cross_tenant_concept_is_404(client, admin_user_headers, other_tenant_concept_id):
    r = await client.get(f"/api/v1/tenant-memory/concepts/{other_tenant_concept_id}", headers=admin_user_headers)
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_tenant_memory_api.py tests/test_tenant_memory_service.py -x -q`
Expected: FAIL (router/service missing).

- [ ] **Step 3: Create schemas** — `backend/app/schemas/tenant_memory.py` (id/tenant_id as `str`, `model_config={"from_attributes": True}`; `MemoryConceptResponse`, `MemoryEdgeResponse`, `MemoryGraphResponse{concepts,edges}`, `MemoryConceptDetail(+links)`, `MemoryLinkResponse`, `MemoryConceptUpdate{name?,summary?,concept_type?,review_state: Literal['pending','confirmed','rejected']?}`, `MemoryMergeRequest{survivor_id, merged_ids: list[str]}`). Use the field names from the LOCKED schema (`name`/`summary`).

- [ ] **Step 4: Create service** — `backend/app/services/tenant_memory_service.py` with `list_concepts(db, tenant_id, review_state=None)`, `list_edges`, `get_concept` (scoped to id AND tenant_id → None on cross-tenant), `get_concept_links`, `update_concept` (patch fields, set `confirmed_by` when review_state→confirmed, `await db.flush()`), `soft_reject_concept` (set `review_state='rejected'`, flush — NOT db.delete), `merge_concepts` (repoint links + edges to survivor, set losers `review_state='merged'`, `merged_into_id`). **Flush, never commit — the endpoint commits.** Every query carries `.where(Model.tenant_id == tenant_id)` (defense-in-depth on top of RLS).

- [ ] **Step 5: Create router** — `backend/app/api/v1/tenant_memory.py`. Deps: `_Reader = Annotated[User, Depends(get_current_user)]`, `_Manager = Annotated[User, Depends(require_permission("memory.manage"))]`, `_Db = Annotated[AsyncSession, Depends(get_db)]`. Endpoints: `GET /tenant-memory` (graph), `GET /tenant-memory/concepts/{id}` (detail+links, 404 via `_parse_uuid`), `PATCH /tenant-memory/concepts/{id}`, `DELETE /tenant-memory/concepts/{id}` (204), `POST /tenant-memory/concepts/merge`. **Each mutation: `audit_service.log_event(...)` BEFORE `await db.commit()`.** Use explicit `_concept_to_response()` helpers that `str()` every UUID (`from_attributes` does NOT coerce UUID→str). Then register in `backend/app/api/v1/router.py`: add `tenant_memory` to the import tuple and `api_router.include_router(tenant_memory.router)`.

> The full router/service/schema code is in the spec-research output; lift it, renaming `label`→`name`, `body`→`summary` per the LOCKED schema.

- [ ] **Step 6: Run tests, verify pass + full suite + lint**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_tenant_memory_api.py tests/test_tenant_memory_service.py -x -q`
Run: `cd backend && .venv/bin/ruff check app/ && .venv/bin/ruff format --check app/`
Expected: PASS / clean.

- [ ] **Step 7: Commit** — `git commit -m "feat(memory): tenant memory CRUD service + router + schemas"`

---

### Task 3: LLM concept extractor

**Files:**
- Create: `backend/app/services/chat/tenant_memory_extractor.py`
- Modify: `backend/app/core/config.py` (add `MEMORY_CONCEPT_MIN_SIMILARITY: float = 0.85`)
- Test: `backend/tests/test_tenant_memory_extractor.py`

- [ ] **Step 1: Write failing test** (mirror `tests/test_memory_updater.py`: mock adapter via `AsyncMock` returning an `LLMResponse(text_blocks=[json])`; assert structured parse; assert `[]` on malformed JSON; assert prompt contains NO hardcoded tenant column names).

```python
# backend/tests/test_tenant_memory_extractor.py (excerpt)
async def test_extract_concepts_parses_json(monkeypatch):
    from app.services.chat import tenant_memory_extractor as ex
    adapter = AsyncMock()
    adapter.create_message.return_value = SimpleNamespace(text_blocks=[
        '{"concepts":[{"name":"Net Revenue","concept_type":"definition",'
        '"plain_english_summary":"Revenue excluding refunds","edges":[],"confidence":0.9}]}'])
    out = await ex.extract_concepts([{"kind":"learned_rule","text":"net revenue excludes refunds"}], adapter, "m")
    assert out[0]["name"] == "Net Revenue" and out[0]["confidence"] == 0.9

async def test_extract_returns_empty_on_garbage():
    from app.services.chat import tenant_memory_extractor as ex
    adapter = AsyncMock(); adapter.create_message.return_value = SimpleNamespace(text_blocks=["no json here"])
    assert await ex.extract_concepts([{"x":1}], adapter, "m") == []
```

- [ ] **Step 2: Run, verify fail** — `cd backend && .venv/bin/python -m pytest tests/test_tenant_memory_extractor.py -x -q`

- [ ] **Step 3: Implement** `tenant_memory_extractor.py` — `extract_concepts(rows, adapter, model)` and `embed_concept(text)` exactly per the research skeleton (string-replace `{{ROWS}}` prompt, `re.search(r"\{.*\}", text, re.DOTALL)` + `json.loads`, try/except→`[]`). Map extractor output `plain_english_summary`→ the concept's `summary` column at insert time. **Obey: no-prompt-pollution (behavioral guidance only), no-LLM-numbers (instruct model to never restate/compute numbers).** `embed_concept` = `openai.AsyncOpenAI(...).embeddings.create(model="text-embedding-3-small", dimensions=1536)` (returns None when no key). Add `MEMORY_CONCEPT_MIN_SIMILARITY` to config.

- [ ] **Step 4: Run, verify pass** — same command. Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(memory): LLM concept extractor + embedding"`

---

### Task 4: Backfill Celery task + admin trigger

**Files:**
- Create: `backend/app/workers/tasks/tenant_memory_extract_backfill.py`
- Modify: `backend/app/workers/celery_app.py` (`conf.include` only — NO beat entry), `backend/app/api/v1/tenant_memory.py` (add `POST /tenant-memory/backfill`)
- Test: `backend/tests/test_tenant_memory_backfill.py`

- [ ] **Step 1: Write failing tests** — (a) `_extract(db, tenant_id, job_id)` reads source rows, upserts concepts + links, and is **idempotent** (second call → same row count, no duplicates); (b) the trigger endpoint requires `tenant.manage` and calls `send_task` with `kwargs={"tenant_id": ...}`.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement task** per research skeleton — `@celery_app.task(base=InstrumentedTask, name="tasks.tenant_memory_extract_backfill", bind=True, queue="sync")`, body `asyncio.run(_run())`, `_run` uses `worker_async_session()` + `await set_tenant_context_session(db, tenant_id)` (session-scoped SET, NOT SET LOCAL — the upsert loop batch-commits every 10 rows). `_extract`: read `TenantLearnedRule` (is_active) + `TenantQueryPattern`; per row, mint/dedup a concept (by normalized name) and upsert a `tenant_memory_link` row via `pg_insert(...).on_conflict_do_update(constraint="uq_tenant_memory_link_source", ...)`; set concept `review_state="pending"`; return stats dict. **`tenant_id` MUST be a kwarg** (InstrumentedTask reads `kwargs['tenant_id']`). Add the module to `celery_app.conf.include` (no beat entry). Add `POST /tenant-memory/backfill` (require `tenant.manage`, `send_task(..., kwargs={"tenant_id": str(user.tenant_id)}, queue="sync")`, audit, commit).

- [ ] **Step 4: Run tests + registration smoke**

Run tests: `cd backend && .venv/bin/python -m pytest tests/test_tenant_memory_backfill.py -x -q`
Run reg check: `docker exec ecom-netsuite-suites-backend-1 python -c "from app.workers.celery_app import celery_app; print('tasks.tenant_memory_extract_backfill' in celery_app.tasks)"` → `True`

- [ ] **Step 5: Commit** — `git commit -m "feat(memory): backfill extractor task + admin trigger endpoint"`

---

### Task 5: The read-loop (inject confirmed concepts)

**Files:**
- Create: `backend/app/services/memory_graph_service.py`
- Modify: `backend/app/services/chat/orchestrator.py`, the LIVE render-site file, `backend/app/services/chat/prompt_cache.py`
- Test: `backend/tests/test_memory_graph_injection.py`

- [ ] **Step 1: Identify the LIVE learned-rules render site**

Run: `cd backend && grep -rn "learned_rules" app/services/chat/agents/ | grep -iE "build|<learned_rules>|parts.append|_system_prompt"`
Decide: mirror `base_agent._build_learned_rules_block` (if that's what runs) OR `unified_agent.system_prompt` property — whichever actually emits the live `<learned_rules>` block. Note which in the commit.

- [ ] **Step 2: Write failing injection test**

```python
# backend/tests/test_memory_graph_injection.py
def test_confirmed_concepts_render_in_prompt():
    from app.services.chat.agents.unified_agent import UnifiedAgent  # or base_agent block builder
    import uuid
    agent = UnifiedAgent(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="t")
    agent._context = {"memory_concepts": [{"name": "Net Revenue", "summary": "excludes refunds"}]}
    p = agent.system_prompt
    assert "<tenant_memory>" in p and "Net Revenue" in p

def test_no_concepts_no_block():
    from app.services.chat.agents.unified_agent import UnifiedAgent
    import uuid
    agent = UnifiedAgent(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), correlation_id="t")
    agent._context = {}
    assert "<tenant_memory>" not in agent.system_prompt
```
Also add a service test asserting `retrieve_confirmed_concepts` SQL filters `review_state == 'confirmed'` (a pending concept is excluded).

- [ ] **Step 3: Run, verify fail.**

- [ ] **Step 4: Implement retrieval service** — `memory_graph_service.retrieve_confirmed_concepts(db, tenant_id, query_text=None, max_concepts=10)`: `select(TenantMemoryConcept).where(tenant_id==X, review_state=="confirmed")` (the gate), return `[{"name":..., "summary":...}]`. Mirror `learned_rules_service.retrieve_learned_rules`.

- [ ] **Step 5: Wire the gather** (orchestrator.py): import next to `retrieve_learned_rules` (~:2287); append `retrieve_confirmed_concepts(db, tenant_id, query_text=sanitized_input)` to `_gather_tasks` + `_gather_keys.append("memory_concepts")` right after the learned_rules append (~:2334), **unconditionally** (do NOT add to the context_need matrix); read back `memory_concepts_result = _results.get("memory_concepts")` (~:2348); fold into `context["memory_concepts"]` with the same `isinstance(..., Exception)` fail-open guard (~:2446).

- [ ] **Step 6: Wire the render** (the site from Step 1): after the learned-rules block, append a `<tenant_memory>` block reading `self._context.get("memory_concepts", [])`, XML-escaping `name`+`summary` via the already-imported `_xml_escape`. Also add `memory_concepts` to the `prompt_cache.py:29` non-cacheable regex.

- [ ] **Step 7: Run injection tests + zero-regression on learned rules + prompt sync**

Run: `cd backend && .venv/bin/python -m pytest tests/test_memory_graph_injection.py tests/test_learned_rules_injection.py tests/test_prompt_tool_sync.py -q`
Expected: all PASS (the new block must not perturb learned_rules; section tags don't trip the tool-sync CI invariant).

- [ ] **Step 8: Commit** — `git commit -m "feat(memory): read-loop — inject confirmed concepts into agent prompt"`

---

### Task 6: Frontend — hooks + page + reactflow graph + nav

**Files:**
- Modify: `frontend/package.json` (add `reactflow`)
- Create: `frontend/src/hooks/use-memory-graph.ts`, `frontend/src/app/(dashboard)/memory/page.tsx`, `frontend/src/components/memory/memory-graph-canvas.tsx`, `frontend/src/components/memory/__tests__/memory-graph-canvas.test.tsx`
- Modify: `frontend/src/lib/constants.ts` (NAV_ITEMS), `frontend/src/components/sidebar.tsx` (icon import + iconMap)

- [ ] **Step 1: Install lib** — `cd frontend && npm i reactflow`

- [ ] **Step 2: Write failing component test** (mirror `learned-rules-section.test.tsx`): `vi.mock("@/hooks/use-memory-graph")`, render `MemoryGraphCanvas` with one `pending` concept, assert its name renders + clicking **Confirm** calls the update mutation with `{id, review_state:"confirmed"}`. Add `global.ResizeObserver` stub in `vitest.setup.ts` (reactflow needs it in jsdom). Map the hook's `MemoryConcept` interface to the LOCKED schema (`name`, `summary`, `concept_type`, `review_state`).

- [ ] **Step 3: Run, verify fail** — `cd frontend && npx vitest run src/components/memory`

- [ ] **Step 4: Implement** the hook (`useMemoryGraph`, `useUpdateConceptReview`, `useCreateConcept`, `useCreateEdge`, `useDeleteConcept` — all via `apiClient`, invalidate `["memory-graph"]`), the page (`app/(dashboard)/memory/page.tsx` → serves at `/memory`; filter chips All/Confirmed/Pending; Skeleton/empty states), and `MemoryGraphCanvas` (reactflow custom `ConceptNode` colored by `review_state`, Confirm button on pending). Per research skeleton. Register nav: add `{ label:"Memory", href:"/memory", icon:"Network", featureFlag:"memory_graph" }` to `NAV_ITEMS` (constants.ts) AND add `Network` to the lucide import + `iconMap` in `sidebar.tsx`.

- [ ] **Step 5: Run component tests + lint** — `cd frontend && npx vitest run && npm run lint`. Expected: PASS.

- [ ] **Step 6: Commit** — `git commit -m "feat(memory): self-serve memory graph UI (reactflow) + nav"`

---

### Task 7: e2e + T2 gates

**Files:**
- Create: `frontend/e2e/memory.spec.ts`
- Create/extend: seeded-tenant backend e2e for the memory read-loop

- [ ] **Step 1: Playwright golden path** (mirror `e2e/settings-learned-rules.spec.ts`): `registerAndLogin(page)` → `page.goto("/memory")` → assert heading + `[data-testid="memory-graph-canvas"]` → trigger backfill (or seed a pending concept via API) → click **Confirm** → assert it moves to the Confirmed filter. Run: `cd frontend && BASE_URL=http://localhost:3000 npx playwright test e2e/memory.spec.ts`.

- [ ] **Step 2: Seeded-tenant backend e2e** — a pytest e2e that: backfills a seeded tenant → asserts pending concepts created → confirms one via the service → asserts `retrieve_confirmed_concepts` returns it AND a chat turn's assembled prompt contains the `<tenant_memory>` block → asserts a rejected concept is absent. (Mirror the recon seeded-tenant e2e pattern.)

- [ ] **Step 3: Full suites green** — `cd backend && .venv/bin/python -m pytest -q` and `cd frontend && npx vitest run`. Expected: green.

- [ ] **Step 4: Blocking T2 review gate** — open the PR, then run `Workflow({name:"code-review-multiangle", args:{target:"<PR#>"}})`. Fails CLOSED: non-empty `failed_angles` ⇒ re-run; `UNVERIFIED` ⇒ human. Confirm `codex_used:true`.

- [ ] **Step 5: Commit + PR** — push the branch; open PR with the spec + plan linked; run the gate.

---

## Self-review checklist (run before handing off)

1. **Spec coverage:** overlay tables ✅(T1) · plain-English summarization ✅(T3) · backfill ✅(T4) · self-serve graph + confirm/edit/delete/merge ✅(T2,T6) · read-loop with review_state gate ✅(T5) · RLS + soft-delete + global-DK-untouched guardrails ✅(T1,T2) · tests ✅(every task) · T2 tier ✅(T7).
2. **Type consistency:** LOCKED schema (`name`/`summary`/`concept_type`/`review_state`) used in every task — no `label`/`body` leakage. `review_state` values `pending|confirmed|rejected|merged` consistent across model, service, read-loop, UI.
3. **No placeholders:** every code step has real code or a real command; the two router/service/extractor bodies reference the research skeletons to lift verbatim (with the rename), which are complete.
4. **Critical gotchas embedded:** two-DB migration, single-head 084_reports, session-scoped RLS in the worker, kwargs tenant_id, fail-open gather, XML-escape, platform-key (not BYOK) for background extraction, prompt_cache regex, reactflow ResizeObserver stub, route-group URL.
