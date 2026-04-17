# Runbook: NetSuite Golden Dataset RAG Ingest

One-off operation to populate `domain_knowledge_chunks` with the 11
NetSuite files from `knowledge/golden_dataset/`. Run this **before merging
Phase 2 PR A** so the first staging deploy already has the chunks
available for retrieval.

## Preconditions

- Worktree or checkout of the Phase 2 PR branch (has the `partition_id` frontmatter
  on the 11 files).
- `backend/.venv` or equivalent Python environment with backend deps installed.
- `.env` with `DATABASE_URL` pointing at the target database (staging Supabase or
  local Docker Postgres).
- `OPENAI_EMBEDDING_API_KEY` set (required — each chunk gets embedded at ingest time).

## Commands

```bash
cd backend
.venv/bin/python -m scripts.ingest_domain_knowledge --dir ../knowledge/golden_dataset
```

The script:
1. Walks every `*.md` file under `../knowledge/golden_dataset/`.
2. Parses frontmatter (`partition_id`, `topic_tags`, `source_type`).
3. Splits the body at H2/H3 boundaries.
4. Embeds each chunk via `embed_domain_texts` (OpenAI embedding API).
5. Upserts into `domain_knowledge_chunks` via
   `pg_insert.on_conflict_do_update` on the `(source_uri, chunk_index)` key.

Expected runtime: ~1-2 minutes for 14 files.

## Verify

### 1. Chunk counts by partition

```sql
-- Run against the target DB
SELECT partition_id, COUNT(*) AS chunks, SUM(token_count) AS total_tokens
FROM domain_knowledge_chunks
WHERE partition_id IS NOT NULL
GROUP BY partition_id
ORDER BY partition_id;
```

Expected rows:

| partition_id | chunks (approx) | total_tokens (approx) |
|---|---|---|
| `netsuite/joins` | 6-12 | 2000-4000 |
| `netsuite/multi-currency` | 3-6 | 1000-2000 |
| `netsuite/record-types` | 6-12 | 2000-4000 |
| `netsuite/suiteql-rules` | 12-25 | 4000-8000 |
| `netsuite/transactions` | 6-12 | 2000-4000 |

Actual chunk counts depend on how many H2 sections each file has. If any
partition shows 0 chunks, the ingest didn't see that partition's files
(check frontmatter).

### 2. Retrieval similarity for canonical queries

Run this diagnostic inside the backend venv (from the worktree):

```bash
.venv/bin/python -c "
import asyncio
from app.core.database import async_session_factory
from app.services.chat.domain_knowledge import retrieve_domain_knowledge

async def main():
    async with async_session_factory() as db:
        queries = [
            'show me sales by shipping country since jan 1',
            'compare net revenue Q1 vs Q2 by subsidiary',
            'pivot order counts by month and country',
        ]
        for q in queries:
            chunks = await retrieve_domain_knowledge(
                db=db,
                query_text=q,
                top_k=3,
                partition_ids=[
                    'netsuite/suiteql-rules',
                    'netsuite/joins',
                    'netsuite/transactions',
                    'netsuite/multi-currency',
                    'netsuite/record-types',
                ],
            )
            print(f'{q!r}: {len(chunks)} chunks')
            for c in chunks:
                print(f'  sim={c[\"similarity\"]:.3f} partition={c.get(\"partition_id\")} source={c[\"source_uri\"]}')

asyncio.run(main())
"
```

Expected: each query returns >= 1 chunk with similarity >= 0.50. If the
top similarity is < 0.50, the chunk is unlikely to be retrieved at chat
time (default threshold is 0.50 per CLAUDE.md). If this happens, inspect
the chunk content — it may need to be reorganized into tighter sections.

## Rollback

`domain_knowledge_chunks` is idempotent on re-ingest. To wipe and start
over against the target DB:

```sql
DELETE FROM domain_knowledge_chunks
WHERE partition_id LIKE 'netsuite/%';
```

Then re-run the ingest command.

## When to re-run

Any time a NetSuite golden_dataset file's content changes. The ingest
upserts on `(source_uri, chunk_index)` so existing chunks get updated
in place; new chunks get inserted. Append-at-end edits are preferred
over inserting in the middle — inserting shifts subsequent chunks'
chunk_index values, which creates stale embeddings until re-ingested.

## Related

- Spec: `docs/superpowers/specs/2026-04-16-netsuite-knowledge-phase-2-design.md`
- Plan: `docs/superpowers/plans/2026-04-16-netsuite-knowledge-phase-2-pr-a.md`
- Profile: `backend/app/services/chat/knowledge_profiles/netsuite.yaml`
- Ingest script: `backend/scripts/ingest_domain_knowledge.py`
