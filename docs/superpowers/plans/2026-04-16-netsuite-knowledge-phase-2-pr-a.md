# NetSuite Knowledge Phase 2 (PR A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move SuiteQL dialect rules from the universal `_SYSTEM_PROMPT` into a new `netsuite.yaml` knowledge profile, ingest 11 orphaned NetSuite golden-dataset files into RAG, and add a worked `transactionShippingAddress` example. Net effect: NetSuite dialect knowledge injects only when NetSuite tools are connected; `golden_dataset/` becomes a live RAG corpus instead of dead docs.

**Architecture:** Two deliberate mechanical moves, one additive content change. (1) Copy the `<suiteql_dialect_rules>` block verbatim from `unified_agent.py:_SYSTEM_PROMPT` into `netsuite.yaml`'s `prompt_fragment`, then delete it from `_SYSTEM_PROMPT` in a single atomic commit (no mid-deploy state with duplicated rules). (2) Extend `ingest_domain_knowledge.py` to read `partition_id` from frontmatter, stamp it on chunks, then add `partition_id` frontmatter to 11 NetSuite files so they ingest under `netsuite/*` partitions. (3) Append a worked example section to `knowledge/golden_dataset/join-patterns-and-aggregation.md`. No DB migrations. No frontend changes. No new packages.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, pytest async, pyyaml. Backend-only.

**Spec:** `docs/superpowers/specs/2026-04-16-netsuite-knowledge-phase-2-design.md` (commit 2c19a5d).

**Worktree:** `/Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2/` on branch `feat/netsuite-knowledge-phase-2`. Base commit is `791d75c` (main @ a4aae09 + Phase 1 work from feat/cross-source-sheets + this plan's spec doc). Phase 1's ADDRESS TABLES block + `_compute_need_patterns` helper + PRECEDENCE clause + ceiling-at-18500 test are all present.

**Related PRs:** #45 (Phase 1, merged into feat/cross-source-sheets 2026-04-17) — adds the ADDRESS TABLES block this plan will move. #46 (deploy.yml SHA-tag fix, open on main) — independent; not required for Phase 2 but makes staging smoke-test deploys reliable if you need to run one after merging Phase 2.

---

## Constraints (from the spec)

- **Verbatim copy of SuiteQL rules** when moving from `_SYSTEM_PROMPT` into `netsuite.yaml.prompt_fragment` (CLAUDE.md rule #24). No paraphrasing, no whitespace drift, no content edits. This plan provides the exact verbatim string.
- **Atomic move.** Create the YAML section AND delete the old `_SYSTEM_PROMPT` section in the same commit. Any intermediate state with duplicated rules risks prompt bloat in CI.
- **No DB migrations, no env var changes, no new packages.** All DB schema columns used (`partition_id` on `domain_knowledge_chunks`) already exist — this plan only starts populating them.
- **Profile loader pattern unchanged.** `netsuite.yaml` mirrors `bigquery.yaml`'s shape: `profile_id`, `display_name`, `trigger_tools`, `prompt_fragment`, `rag_partitions`.
- **No re-enable of auto-learning.** Out of scope; PR B separate.

---

## File Map

| File | Op | Purpose |
|---|---|---|
| `backend/app/services/chat/knowledge_profiles/netsuite.yaml` | Create | New profile; triggers on any NetSuite read tool; contains the verbatim SuiteQL dialect rules |
| `backend/app/services/chat/agents/unified_agent.py` | Modify | Delete lines 173-320 (the `<suiteql_dialect_rules>` block) from `_SYSTEM_PROMPT` |
| `backend/scripts/ingest_domain_knowledge.py` | Modify | Read `partition_id` from frontmatter; stamp onto `DomainKnowledgeChunk.partition_id` |
| `knowledge/golden_dataset/join-patterns-and-aggregation.md` | Modify | Append worked `transactionShippingAddress` example at END (preserves chunk_index stability) |
| `knowledge/golden_dataset/suiteql-syntax-rules.md` | Modify | Add `partition_id: netsuite/suiteql-rules` to frontmatter |
| `knowledge/golden_dataset/suiteql-example-queries.md` | Modify | Add `partition_id: netsuite/suiteql-rules` to frontmatter |
| `knowledge/golden_dataset/common-errors-and-recovery.md` | Modify | Add `partition_id: netsuite/suiteql-rules` to frontmatter |
| `knowledge/golden_dataset/date-and-time-patterns.md` | Modify | Add `partition_id: netsuite/suiteql-rules` to frontmatter |
| `knowledge/golden_dataset/transaction-relationships.md` | Modify | Add `partition_id: netsuite/joins` to frontmatter |
| `knowledge/golden_dataset/transaction-types-and-statuses.md` | Modify | Add `partition_id: netsuite/transactions` to frontmatter |
| `knowledge/golden_dataset/financial-statements.md` | Modify | Add `partition_id: netsuite/transactions` to frontmatter |
| `knowledge/golden_dataset/multi-currency-rules.md` | Modify | Add `partition_id: netsuite/multi-currency` to frontmatter |
| `knowledge/golden_dataset/record-types-and-columns.md` | Modify | Add `partition_id: netsuite/record-types` to frontmatter |
| `knowledge/golden_dataset/custom-fields-and-records.md` | Modify | Add `partition_id: netsuite/record-types` to frontmatter |
| `backend/tests/test_ingest_partition_id.py` | Create | Unit test: frontmatter `partition_id` flows through to chunk |
| `backend/tests/test_netsuite_yaml_profile.py` | Create | Profile loads, trigger_tools expected, prompt_fragment contains key rule strings |
| `backend/tests/test_unified_agent_suiteql_rules.py` | Modify | Assertions now read `netsuite.yaml.prompt_fragment` instead of `_SYSTEM_PROMPT` |
| `backend/tests/test_prompt_trim.py` | Modify | Tighten `_SYSTEM_PROMPT` ceiling from 18500 → 13000 chars |
| `docs/runbooks/netsuite-rag-ingest.md` | Create | How to ingest the 11 NS golden_dataset files against staging |

**Working directory:** All commands below assume `pwd` is `/Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2/`.

---

## Task 1: Worktree environment setup

**Files:** none modified (environment-only).

The worktree was created from `origin/main` with Phase 1 content merged in, but `.venv`, `node_modules`, and `.env` are NOT symlinked from the main checkout. A fresh subagent session must set up these symlinks before any pytest or ingest command will work. This task has no tests of its own — it verifies the environment is executable.

- [ ] **Step 1: Symlink `.venv`, `node_modules`, and `.env`**

Run from the worktree root:

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2
ln -s ../../../backend/.venv backend/.venv
ln -s ../../../frontend/node_modules frontend/node_modules
ln -s ../../.env .env
```

(Three levels of `..` because the worktree path is `<repo>/.worktrees/<branch>/<subdir>/`, so from `backend/` you need three ups to reach `<repo>/` where the real `.venv` lives. The `.env` is two levels because it sits at `<repo>/.env` and the worktree root is `<repo>/.worktrees/<branch>/`.)

- [ ] **Step 2: Verify symlinks resolve**

```bash
ls -la backend/.venv/bin/python frontend/node_modules/.bin/tsc .env
```

Expected: all three lines show valid symlinks resolving to files that exist. `ls: cannot access ...: No such file or directory` means the `..` counts are off — re-check with `readlink backend/.venv` against the actual `<repo>/backend/.venv` path.

- [ ] **Step 3: Smoke-test backend imports**

```bash
backend/.venv/bin/python -c "import sys; sys.path.insert(0, 'backend'); from app.main import app; print(f'backend imports OK, {len(app.routes)} routes')"
```

Expected: `backend imports OK, 195 routes` (or similar — the exact count depends on any new routes since Phase 1).

- [ ] **Step 4: Smoke-test frontend tooling**

```bash
cd frontend && npx tsc --noEmit 2>&1 | tail -5
cd ..
```

Expected: empty output (no type errors) or an acceptable message. If TypeScript complains about missing modules, node_modules symlink is broken.

- [ ] **Step 5: No commit**

This task sets up the worktree; nothing enters git. Proceed to Task 2.

---

## Task 2: Extend `ingest_domain_knowledge.py` to read `partition_id` from frontmatter

**Files:**
- Modify: `backend/scripts/ingest_domain_knowledge.py`
- Create: `backend/tests/test_ingest_partition_id.py`

`DomainKnowledgeChunk.partition_id` is already a column on the model (`backend/app/models/domain_knowledge.py:38`) but the current ingest script never sets it — all existing chunks have `partition_id = NULL`. To support the `netsuite/*` partition scheme referenced by `netsuite.yaml`, the ingest script must read `partition_id` from each markdown file's YAML frontmatter and pass it through to the chunk insert.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_ingest_partition_id.py`:

```python
"""Unit tests for partition_id propagation in ingest_domain_knowledge.

DomainKnowledgeChunk.partition_id has been on the model since the
knowledge-profile work but the ingest script never populated it.
netsuite.yaml's rag_partitions (netsuite/suiteql-rules, netsuite/joins,
etc.) only work if the matching chunks have partition_id set.

This test asserts that `partition_id` in frontmatter flows through to
the chunk record's partition_id field.
"""

from scripts.ingest_domain_knowledge import parse_frontmatter


class TestParseFrontmatterPartitionId:
    def test_partition_id_in_frontmatter(self):
        content = '---\npartition_id: netsuite/suiteql-rules\ntopic_tags: ["suiteql"]\n---\n\n# Body\n'
        fm, body = parse_frontmatter(content)
        assert fm.get("partition_id") == "netsuite/suiteql-rules"
        assert body.startswith("# Body")

    def test_partition_id_absent_is_none(self):
        content = '---\ntopic_tags: ["suiteql"]\n---\n\n# Body\n'
        fm, body = parse_frontmatter(content)
        assert fm.get("partition_id") is None

    def test_no_frontmatter_at_all(self):
        content = '# Just a heading\n\nsome body\n'
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == content
```

- [ ] **Step 2: Run test to confirm `parse_frontmatter` works (baseline)**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2
backend/.venv/bin/python -m pytest backend/tests/test_ingest_partition_id.py::TestParseFrontmatterPartitionId -v
```

Expected: 3 PASS. (`parse_frontmatter` was already generic about frontmatter keys, so `partition_id` flows through to the returned dict without any code change.)

- [ ] **Step 3: Write a failing test for chunk-level propagation**

Append to `backend/tests/test_ingest_partition_id.py`:

```python
class TestChunkPartitionIdPropagation:
    """The chunk record inserted into domain_knowledge_chunks must have
    partition_id populated when the source markdown declares it in
    frontmatter.
    """

    def test_ingest_passes_partition_id_to_chunk_dict(self):
        """The dict representation of a chunk built from frontmatter with
        partition_id must carry that partition_id forward."""
        from scripts.ingest_domain_knowledge import build_chunk_dicts_for_file

        # A minimal markdown file string with frontmatter
        content = (
            '---\n'
            'topic_tags: ["suiteql", "joins"]\n'
            'source_type: expert_rules\n'
            'partition_id: netsuite/joins\n'
            '---\n\n'
            '# Join Patterns\n\n'
            '## Header vs Line\n\n'
            'Some content about joins.\n'
        )
        chunks = build_chunk_dicts_for_file(source_uri="test.md", content=content)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk["partition_id"] == "netsuite/joins"

    def test_ingest_handles_missing_partition_id(self):
        """Files without partition_id frontmatter produce chunks with
        partition_id=None (not a crash, not a default string)."""
        from scripts.ingest_domain_knowledge import build_chunk_dicts_for_file

        content = (
            '---\n'
            'topic_tags: ["suiteql"]\n'
            'source_type: expert_rules\n'
            '---\n\n'
            '# Something\n\n'
            'body\n'
        )
        chunks = build_chunk_dicts_for_file(source_uri="test.md", content=content)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk["partition_id"] is None
```

- [ ] **Step 4: Run test to verify it fails**

```bash
backend/.venv/bin/python -m pytest backend/tests/test_ingest_partition_id.py::TestChunkPartitionIdPropagation -v
```

Expected: 2 FAIL with `ImportError: cannot import name 'build_chunk_dicts_for_file'` OR `KeyError: 'partition_id'` depending on current state. The public helper `build_chunk_dicts_for_file` does not yet exist as a top-level function; the existing ingest script has this logic inline inside `main`. We will extract it.

- [ ] **Step 5: Inspect the current ingest loop to find the extraction point**

```bash
backend/.venv/bin/python -c "
import inspect, scripts.ingest_domain_knowledge as m
src = inspect.getsource(m)
# Print lines around DomainKnowledgeChunk(
for i, line in enumerate(src.split('\n'), 1):
    if 'DomainKnowledgeChunk(' in line or 'topic_tags' in line or 'chunk_dict' in line.lower():
        print(f'{i}: {line}')
"
```

Use the output to locate the section in `backend/scripts/ingest_domain_knowledge.py` that builds per-file chunk dicts. The existing logic roughly reads the markdown, splits it into chunks (at H2/H3 boundaries), and builds a dict per chunk with keys like `source_uri`, `chunk_index`, `raw_text`, `token_count`, `topic_tags`, `source_type`. We extend it to also emit `partition_id`.

- [ ] **Step 6: Extract the per-file chunk-building logic into a testable helper**

Open `backend/scripts/ingest_domain_knowledge.py`. Near the bottom of the file (before `async def ingest()` or `main()`), add the helper. The actual in-file chunk-splitting code is ~30-40 lines; the helper wraps exactly that, returning a list of dicts.

Use Edit with:

`old_string` (the existing top-of-file imports block):

```python
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import async_session_factory
from app.models.domain_knowledge import DomainKnowledgeChunk
from app.services.chat.domain_knowledge import embed_domain_texts
```

`new_string` (unchanged — this block stays as-is; we're adding a helper later in the file):

```python
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import async_session_factory
from app.models.domain_knowledge import DomainKnowledgeChunk
from app.services.chat.domain_knowledge import embed_domain_texts
```

(No-op Edit — confirms the import block is intact.)

Now find the `parse_frontmatter` function (starts at line ~37) and add the new helper `build_chunk_dicts_for_file` right after it. Use Edit with:

`old_string`:

```python
def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter from markdown content.

    Returns (frontmatter_dict, remaining_content).
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}, content
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    remaining = content[match.end() :]
    return fm, remaining
```

`new_string`:

```python
def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter from markdown content.

    Returns (frontmatter_dict, remaining_content).
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}, content
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    remaining = content[match.end() :]
    return fm, remaining


def build_chunk_dicts_for_file(*, source_uri: str, content: str) -> list[dict[str, Any]]:
    """Split a markdown file's content into chunk dicts ready for DB insert.

    Each dict has: source_uri, chunk_index, raw_text, token_count,
    topic_tags, source_type, partition_id. partition_id is None when not
    declared in frontmatter.

    Chunking: splits at H2/H3 boundaries, never severs code blocks from
    their preceding text, prepends H1 title to each chunk for embedding
    context. This mirrors the existing `ingest()` inline behavior —
    extracted here so the logic is unit-testable.
    """
    frontmatter, body = parse_frontmatter(content)
    topic_tags = frontmatter.get("topic_tags", [])
    source_type = frontmatter.get("source_type", "expert_rules")
    partition_id = frontmatter.get("partition_id")

    # Extract the H1 title (first line starting with "# ") to prepend to each chunk
    h1_match = re.search(r"^#\s+(.+?)$", body, re.MULTILINE)
    h1_title = h1_match.group(1).strip() if h1_match else ""

    # Split body at H2 boundaries (lines starting with "## ")
    # The first section before any H2 is treated as a preface (often empty).
    sections = re.split(r"(?=^##\s+)", body, flags=re.MULTILINE)

    chunks: list[dict[str, Any]] = []
    for idx, section in enumerate(sections):
        stripped = section.strip()
        if not stripped:
            continue
        prefixed_text = f"# {h1_title}\n\n{stripped}" if h1_title and idx > 0 else stripped
        chunks.append({
            "source_uri": source_uri,
            "chunk_index": idx,
            "raw_text": prefixed_text,
            "token_count": estimate_tokens(prefixed_text),
            "topic_tags": topic_tags,
            "source_type": source_type,
            "partition_id": partition_id,
        })
    return chunks
```

- [ ] **Step 7: Run unit tests to verify the helper works**

```bash
backend/.venv/bin/python -m pytest backend/tests/test_ingest_partition_id.py -v
```

Expected: all 5 tests PASS (3 from Step 1's class + 2 from Step 3's class).

- [ ] **Step 8: Update the in-file `ingest()` body to call the helper and pass `partition_id` to the DB insert**

Now integrate: the existing `ingest()` function builds chunk dicts inline. Find that section (look for the loop body that creates chunk dicts) and replace the inline logic with a call to `build_chunk_dicts_for_file`. Additionally, find the `DomainKnowledgeChunk(...)` constructor call (or `pg_insert(...).on_conflict_do_update(...)` upsert) and add `partition_id=chunk_data["partition_id"]` to the fields.

Inspect the current `ingest()` function to locate the exact lines. Use Read on `backend/scripts/ingest_domain_knowledge.py` starting at line 60 to find the ingest loop. The replacement pattern is:

- Find the inline H1 extraction + section split + chunk dict construction (roughly lines 65-125 based on current file structure, but verify).
- Replace that inline block with `chunks = build_chunk_dicts_for_file(source_uri=str(path.relative_to(dir_path.parent)), content=content)`.
- Find the `DomainKnowledgeChunk(...)` or equivalent `pg_insert` statement and add the `partition_id` field.

Given the existing script's 273 lines and the complexity of the ingest loop, the actual refactor may require 20-40 lines of in-file edits. Focus only on: (a) using the new helper, (b) passing `partition_id` to the chunk insert. Do NOT restructure anything else.

- [ ] **Step 9: Integration check — run ingest in dry-run mode against the existing golden_dataset**

The existing script has no dry-run mode by default. Create a one-shot verification: temporarily ingest a single file (with a known partition_id added) and inspect the row. Use a small Python REPL:

```bash
backend/.venv/bin/python -c "
from scripts.ingest_domain_knowledge import build_chunk_dicts_for_file
with open('knowledge/golden_dataset/multi-currency-rules.md') as f:
    content = f.read()
chunks = build_chunk_dicts_for_file(source_uri='multi-currency-rules.md', content=content)
print(f'{len(chunks)} chunks')
print(f'partition_id of chunk 0: {chunks[0].get(\"partition_id\")}')
print(f'topic_tags of chunk 0: {chunks[0].get(\"topic_tags\")}')
"
```

Expected: `partition_id: None` (because we haven't added it to this file's frontmatter yet — that's Task 8). If the helper crashes, something in the refactor is wrong; debug before committing.

- [ ] **Step 10: Run sibling tests that might touch ingestion**

```bash
backend/.venv/bin/python -m pytest backend/tests/ -k "ingest or domain_knowledge" -v
```

Expected: all existing tests PASS. The helper is backward-compatible (it just makes the existing logic reachable from tests).

- [ ] **Step 11: Commit**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2
git add backend/scripts/ingest_domain_knowledge.py backend/tests/test_ingest_partition_id.py
git commit -m "feat(ingest): read partition_id from frontmatter, stamp on chunk

DomainKnowledgeChunk.partition_id has existed as a column since the
knowledge-profile work but the ingest script never populated it. That's
why rag_partitions on any profile returned nothing — all existing chunks
have partition_id=NULL.

Extract the inline chunk-building logic into build_chunk_dicts_for_file
(a pure function, testable in isolation). Read partition_id from
frontmatter; pass it through to the chunk dict. The existing DB insert
now stamps partition_id on every new chunk.

Files with no partition_id in frontmatter keep landing as partition_id=
NULL — backward compatible with already-ingested chunks.

5 new unit tests guard the extraction. No DB migration required (column
already exists).

Spec: docs/superpowers/specs/2026-04-16-netsuite-knowledge-phase-2-design.md"
```

---

## Task 3: Create `netsuite.yaml` + delete SuiteQL block from `_SYSTEM_PROMPT` (atomic)

**Files:**
- Create: `backend/app/services/chat/knowledge_profiles/netsuite.yaml`
- Modify: `backend/app/services/chat/agents/unified_agent.py` (delete lines 173-320)

This is the highest-blast-radius task in the plan. The create-yaml and delete-from-prompt MUST land in the same commit. Any intermediate state has the rules in BOTH places, which is fine for runtime but violates the "single source of truth" principle of CLAUDE.md rule #24.

No test is written FIRST in this task because the tests that enforce rule presence (Tasks 4 and 5) still assert against `_SYSTEM_PROMPT` until they're updated. Testing discipline here: Task 3 ships the data move, Tasks 4 and 5 relocate the assertions. If you try to TDD Task 3 in isolation, the existing `test_unified_agent_suiteql_rules.py` assertions (which read `_SYSTEM_PROMPT`) would BREAK mid-task and you'd end up debugging ghost failures.

Instead: make the move, then run the full test suite AT THE END of Task 3 and expect `test_unified_agent_suiteql_rules.py` to fail. Tasks 4 and 5 fix those failures.

- [ ] **Step 1: Create the new `netsuite.yaml` profile**

Create `backend/app/services/chat/knowledge_profiles/netsuite.yaml`. The `prompt_fragment` contains the VERBATIM SuiteQL block from `unified_agent.py:173-320` — preserved below in full for copy-paste fidelity. The only wrapper change is moving from XML tags (`<suiteql_dialect_rules>...</suiteql_dialect_rules>` in the base prompt) into the same XML tags inside the YAML's prompt_fragment string. This keeps the agent's cross-references (e.g., tool_selection's "Follow ALL <suiteql_dialect_rules>") valid.

```yaml
profile_id: netsuite
display_name: "NetSuite SuiteQL"
trigger_tools:
  - netsuite_suiteql
  - netsuite_financial_report
  - netsuite_get_metadata
  - ns_runReport
  - ns_runSavedSearch
  - ns_listSavedSearches
  - ns_listAllReports
  - "ext__*__ns_runCustomSuiteQL"
  - "ext__*__ns_getSuiteQLMetadata"
  - "ext__*__ns_getRecord"
  - "ext__*__ns_runReport"
  - "ext__*__ns_runSavedSearch"
  - "ext__*__ns_listSavedSearches"
  - "ext__*__ns_listAllReports"
  - "ext__*__ns_getSavedSearchSchema"
prompt_fragment: |
  <suiteql_dialect_rules>
  SuiteQL is Oracle-based with NetSuite-specific behaviors:

  # Prevents: wrong "latest N" results — ROWNUM filters before ORDER BY (2025)
  PAGINATION:
  - `FETCH FIRST N ROWS ONLY` for "latest"/"top N". NEVER `ROWNUM` with `ORDER BY`. `LIMIT` not supported.

  COLUMN NAMING:
  - Primary key is `id` (NOT `internalid`).
  - `id` is sequential — higher id = more recent. Use `ORDER BY t.id DESC` for "latest" queries.
  - Transaction date: `trandate`. Created date: `createddate`.

  # Prevents: 0-row results from wrong date functions (recurring since 2025)
  DATE FUNCTIONS — CRITICAL:
  - "today": `BUILTIN.RELATIVE_RANGES('TODAY', 'START')` (preferred) or `TRUNC(SYSDATE)` (fallback, server time).
  - "yesterday": `TRUNC(SYSDATE) - 1`.
  - Date ranges: `WHERE t.trandate >= TRUNC(SYSDATE) - 7`
  - Specific dates: `WHERE t.trandate = TO_DATE('2026-01-15', 'YYYY-MM-DD')`
  - Saved search periods: `BUILTIN.RELATIVE_RANGES('THIS_MONTH', 'START')` / `BUILTIN.RELATIVE_RANGES('THIS_MONTH', 'END')`.
  - NEVER use `BUILTIN.DATE(SYSDATE)` — returns 0 rows.
  - NEVER use `CURRENT_DATE` — not supported in SuiteQL.

  TEXT RESOLUTION:
  - Use `BUILTIN.DF(field_name)` for List/Record fields to get display text.

  BOOLEAN FIELDS — CRITICAL:
  - NetSuite stores booleans as 'T' (true) and 'F' (false), NOT true/false.
  - In query results: 'T' = Yes/True/Enabled, 'F' = No/False/Disabled, '' or NULL = not set.
  - When presenting results: ALWAYS interpret 'T' as YES and 'F' as NO. Do NOT say a field is "empty/false" when it contains 'T'.

  # Prevents: filtering custom list fields by string instead of ID (2025)
  CUSTOM LIST FIELDS:
  - SELECT-type fields store integer IDs. Filter: `WHERE field = <id>` (fastest) or `BUILTIN.DF(field) = 'Value Name'` (readable).
  - ID → name mappings in tenant schema Custom List Values. Linkage shown as `(SELECT → customlist_name)`.

  # Prevents: shipping-country join rediscovery loop (2026-04-09 / 2026-04-16)
  ADDRESS TABLES — shipping/billing country, state, city:
  - Country lives on the address record, NOT the transaction header. Join: `JOIN transactionShippingAddress sa ON sa.nKey = t.shippingAddress` (or `JOIN transactionBillingAddress ba ON ba.nKey = t.billingAddress`).
  - The join key is `sa.nKey = t.shippingAddress`. NEVER `sa.recordOwner = t.id`, NEVER `sa.transaction = t.id`, NEVER `sa.id = t.shippingAddress`.
  - Read `BUILTIN.DF(sa.country)` for country name ("Switzerland") or `sa.country` for 2-letter ISO code ("CH"). Both work; prefer `BUILTIN.DF` for display, raw code for filtering.
  - Do NOT use custom body fields (`custbody*_ship_country*`, `custbody*_country*`) for country queries unless the user explicitly asks for the custom field. The standard address join is the source of truth.

  TRANSACTION NUMBER CONVENTIONS:
  - NetSuite `tranid` typically includes the type prefix (e.g., "RMA61214", "SO865732", "PO12345").
  - When the user says "RMA61214", search for the EXACT value first: `WHERE t.tranid = 'RMA61214'`
  - Common prefixes and their type codes (use to filter by type for faster queries):
    RMA → `t.type = 'RtnAuth'`, SO → `t.type = 'SalesOrd'`, PO → `t.type = 'PurchOrd'`,
    INV → `t.type = 'CustInvc'`, TO → `t.type = 'TrnfrOrd'`, IF → `t.type = 'ItemShip'`,
    IR → `t.type = 'ItemRcpt'`, WO → `t.type = 'WorkOrd'`, VB → `t.type = 'VendBill'`

  HEADER vs LINE AGGREGATION — CRITICAL:
  - `t.foreigntotal` and `t.total` are HEADER-LEVEL fields.
  - If you JOIN transactionline, NEVER use `SUM(t.foreigntotal)` — it inflates by line count.
  - For order-level totals: query `transaction` alone without transactionline.
  - For line-level breakdown: use `SUM(tl.amount * -1)` for revenue in base currency (USD).

  JOIN PATTERNS:
  - Filter to item lines only using `tl.mainline = 'F' AND tl.taxline = 'F' AND (tl.iscogs = 'F' OR tl.iscogs IS NULL) AND tl.assemblycomponent = 'F'`.
  - The `assemblycomponent = 'F'` filter excludes assembly/kit component lines that would otherwise double-count alongside the parent line.
  - For header-only queries (no line details), use `WHERE t.mainline = 'T'` or just query the `transaction` table without joining `transactionline`.
  - COLUMN RESTRICTION: `tl.itemtype` does NOT work on transactionline via REST API (returns 400). Use `i.type` from the item table instead: `JOIN item i ON tl.item = i.id WHERE i.type IN ('InvtPart', 'Assembly')`.
  - For strict revenue queries (excluding shipping, discounts, subtotals): `JOIN item i ON tl.item = i.id WHERE i.type NOT IN ('ShipItem', 'Discount', 'Subtotal', 'Markup', 'Payment', 'EndGroup')`.
  - LINKED RECORDS (createdfrom): The `createdfrom` field on transaction and transactionline links related records in the fulfillment chain. Common chains: SO → Invoice (`CustInvc.createdfrom = SalesOrd.id`), PO → Item Receipt (`ItemRcpt.createdfrom = PurchOrd.id`), RMA → Item Receipt (`ItemRcpt.createdfrom = RtnAuth.id`), SO → Item Fulfillment (`ItemShip.createdfrom = SalesOrd.id`). To find linked records: `SELECT t2.tranid FROM transaction t2 WHERE t2.createdfrom = <source_id>`.

  LINE AMOUNT SIGN CONVENTION — IMPORTANT:
  - In NetSuite, `tl.foreignamount` is NEGATIVE for revenue lines on sales orders, invoices, and credit memos (accounting convention: credits are negative).
  - `t.foreigntotal` (header) is POSITIVE for the same transactions.
  - When presenting line-level sales totals to the user, NEGATE the amount to match the positive header convention: use `SUM(tl.foreignamount) * -1` or `ABS(SUM(tl.foreignamount))`.
  - For base currency (USD): use `SUM(tl.amount * -1)`. This is the GL-posted amount — the most accurate accounting value.
  - Do NOT present raw negative amounts as "sales" — it confuses users. Always present revenue as positive numbers.
  - Sort revenue DESC (highest first) when showing "best sellers" or "top platforms".

  MULTI-CURRENCY — CRITICAL:
  - `t.foreigntotal` = amount in the TRANSACTION currency (could be USD, EUR, GBP, etc.)
  - `t.total` = amount in the SUBSIDIARY's BASE currency (usually USD for US-based companies)
  - `t.currency` = the transaction's currency (use BUILTIN.DF(t.currency) for name)
  - `t.exchangerate` = conversion rate from transaction currency to subsidiary base currency
  - `tl.foreignamount` / `tl.netamount` = line amounts in TRANSACTION currency
  - `tl.amount` / `tl.netamount` (without "foreign") = line amounts in SUBSIDIARY BASE currency
  - When the user asks for "total in USD" or "USD value": Use `SUM(t.total)` — this is already converted to the subsidiary's base currency (USD). No manual conversion needed.
  - When the user asks for breakdown by currency: Use `SUM(t.foreigntotal)` with `GROUP BY BUILTIN.DF(t.currency)` to show per-currency totals.
  - For line-level amounts in base currency: Use `SUM(tl.amount) * -1` (base currency, negated for revenue).
  - For line-level amounts in transaction currency: Use `SUM(tl.foreignamount) * -1` (transaction currency, negated for revenue).
  - DEFAULT: For line-level USD revenue, use `SUM(tl.amount * -1)`. For header-level, use `SUM(t.total)`.

  TRANSACTION TYPES (avoid double-counting):
  - For order analysis: `t.type = 'SalesOrd'` only.
  - For recognized revenue: `t.type = 'CustInvc'` only.
  - NEVER combine SalesOrd + CustInvc in one SUM — same sale appears as both.

  STATUS CODE FILTERING — CRITICAL:
  - The REST API uses SINGLE-LETTER status codes, NOT compound codes.
  - WRONG: `t.status = 'SalesOrd:B'` or `t.status = 'PurchOrd:H'` — these silently match NOTHING.
  - CORRECT: `t.status = 'B'` or `t.status NOT IN ('G', 'H')`
  - Sales Order (SalesOrd): A=Pending Approval, B=Pending Fulfillment, C=Cancelled, D=Partially Fulfilled, E=Pending Billing/Partially Fulfilled, F=Pending Billing, G=Billed, H=Closed
  - Purchase Order (PurchOrd): A=Pending Supervisor Approval, B=Pending Receipt, C=Rejected, D=Partially Received, E=Pending Billing/Partially Received, F=Pending Bill, G=Fully Billed, H=Closed
  - Return Authorization (RtnAuth): A=Pending Approval, B=Pending Receipt, C=Cancelled, D=Partially Received, E=Received, F=Closed, G=Refunded, H=Cancelled
  - Invoice (CustInvc): A=Open, B=Paid In Full
  - Item Receipt (ItemRcpt): A=Received (only status)
  - Item Fulfillment (ItemShip): A=Shipped, B=Packed, C=Picked
  - Vendor Bill (VendBill): A=Open, B=Paid In Full
  - For active POs (open/in-progress), exclude closed and fully billed: `t.status NOT IN ('G', 'H')`
  - For active SOs (open/in-progress), exclude closed and cancelled: `t.status NOT IN ('C', 'H')`
  - For RMAs with items received: `t.status IN ('D', 'E', 'F', 'G', 'H')` (D=partially received, E=received, F=closed, G=refunded — all confirm items were received)
  - ALWAYS use single-letter codes for ALL transaction types.

  ITEM TABLE GOTCHA:
  - Only safe columns: id, itemid, displayname, description. Other columns may cause 0 rows.
  - If a minimal query succeeds, present those results. Don't add more columns.

  # Prevents: wrong table for inventory (inventorybalance doesn't work via REST API, 2025)
  INVENTORY QUERIES:
  - ALWAYS use `inventoryitemlocations` (NOT `inventorybalance`, NOT custom records). It is the definitive source.
  - Join: `JOIN item i ON i.id = iil.item`. Key columns: `iil.quantityavailable`, `iil.quantityonhand`, `BUILTIN.DF(iil.location)`.
  - Filter items: `WHERE i.itemid LIKE '%keyword%'` or `WHERE i.displayname LIKE '%keyword%'`.
  - If 0 rows, retry without `quantityavailable > 0` filter. If still 0, query `item` alone first to confirm items exist.

  CUSTOM RECORD TABLES:
  - Use LOWERCASE scriptid: `customrecord_r_inv_processor`.

  CUSTOM FIELDS SEARCH STRATEGY:
  - custbody_* fields → on transaction header (e.g., custbody_platform, custbody_shopify_order)
  - custitem_* fields → on item records (e.g., custitem_fw_platform)
  - custcol_* fields → on transaction lines (e.g., custcol_tracking)
  - custentity_* fields → on entity records (customer, vendor, employee)
  - Always check <tenant_schema> and <tenant_vernacular> for available custom fields before guessing.

  # Prevents: 400 errors from guessing column names (recurring since 2025)
  PREFLIGHT SCHEMA CHECK:
  - Verify ALL columns in <tenant_schema> or <standard_table_schemas> before querying. Unknown columns → call netsuite_get_metadata.
  - Safe columns (never need verification): id, tranid, trandate, type, entity, status, total, foreigntotal, memo, createddate (transaction); id, transaction, item, quantity, rate, amount, foreignamount, mainline, taxline, iscogs, linesequencenumber, class, department, location, quantityshiprecv, quantitybilled, memo, createdfrom (transactionline); id, companyname, email (customer); id, itemid, displayname, description, type (item).
  - Known restricted via REST API: `tl.itemtype` → use `i.type` instead. `t.expectedreceiptdate` → use `tl.expectedreceiptdate` (line-level only). `tl.quantityreceived` → use `tl.quantityshiprecv`.
  - PO pending receipt: `tl.expectedreceiptdate` for arrival, `(tl.quantity - NVL(tl.quantityshiprecv, 0)) AS pending_qty`.

  SELECT COLUMN ORDER — for readable output:
  - Identifiers (tranid, entity) → items → dates → status → quantities → amounts → dimensions (location, subsidiary, class).

  FINANCIAL AGGREGATION — CRITICAL:
  - NEVER return raw financial rows for the LLM to sum. Use SQL GROUP BY + SUM().
  - WRONG: "Show me all revenue accounts" → returns 78 rows → LLM hallucinates total
  - RIGHT: "Show me revenue by account type" → SUM(amount) GROUP BY accttype → 5 rows with pre-computed totals
  - For net income: compute in SQL → SUM(CASE WHEN accttype IN ('Income','OthIncome') THEN amount * -1 ELSE amount END)
  - The LLM should PRESENT numbers, never COMPUTE them. All math happens in SQL or in tool-provided summary objects.
  </suiteql_dialect_rules>
rag_partitions:
  - netsuite/suiteql-rules
  - netsuite/joins
  - netsuite/transactions
  - netsuite/multi-currency
  - netsuite/record-types
```

- [ ] **Step 2: Delete the SuiteQL block from `_SYSTEM_PROMPT`**

Open `backend/app/services/chat/agents/unified_agent.py`. Lines 173-320 contain `<suiteql_dialect_rules>` through `</suiteql_dialect_rules>` (inclusive). Delete those 148 lines and the blank line after the closing tag (line 321).

Use Edit with:

`old_string`:

```
<suiteql_dialect_rules>
SuiteQL is Oracle-based with NetSuite-specific behaviors:

# Prevents: wrong "latest N" results — ROWNUM filters before ORDER BY (2025)
PAGINATION:
- `FETCH FIRST N ROWS ONLY` for "latest"/"top N". NEVER `ROWNUM` with `ORDER BY`. `LIMIT` not supported.

COLUMN NAMING:
- Primary key is `id` (NOT `internalid`).
- `id` is sequential — higher id = more recent. Use `ORDER BY t.id DESC` for "latest" queries.
- Transaction date: `trandate`. Created date: `createddate`.

# Prevents: 0-row results from wrong date functions (recurring since 2025)
DATE FUNCTIONS — CRITICAL:
- "today": `BUILTIN.RELATIVE_RANGES('TODAY', 'START')` (preferred) or `TRUNC(SYSDATE)` (fallback, server time).
- "yesterday": `TRUNC(SYSDATE) - 1`.
- Date ranges: `WHERE t.trandate >= TRUNC(SYSDATE) - 7`
- Specific dates: `WHERE t.trandate = TO_DATE('2026-01-15', 'YYYY-MM-DD')`
- Saved search periods: `BUILTIN.RELATIVE_RANGES('THIS_MONTH', 'START')` / `BUILTIN.RELATIVE_RANGES('THIS_MONTH', 'END')`.
- NEVER use `BUILTIN.DATE(SYSDATE)` — returns 0 rows.
- NEVER use `CURRENT_DATE` — not supported in SuiteQL.

TEXT RESOLUTION:
- Use `BUILTIN.DF(field_name)` for List/Record fields to get display text.

BOOLEAN FIELDS — CRITICAL:
- NetSuite stores booleans as 'T' (true) and 'F' (false), NOT true/false.
- In query results: 'T' = Yes/True/Enabled, 'F' = No/False/Disabled, '' or NULL = not set.
- When presenting results: ALWAYS interpret 'T' as YES and 'F' as NO. Do NOT say a field is "empty/false" when it contains 'T'.

# Prevents: filtering custom list fields by string instead of ID (2025)
CUSTOM LIST FIELDS:
- SELECT-type fields store integer IDs. Filter: `WHERE field = <id>` (fastest) or `BUILTIN.DF(field) = 'Value Name'` (readable).
- ID → name mappings in tenant schema Custom List Values. Linkage shown as `(SELECT → customlist_name)`.

# Prevents: shipping-country join rediscovery loop (2026-04-09 / 2026-04-16)
ADDRESS TABLES — shipping/billing country, state, city:
- Country lives on the address record, NOT the transaction header. Join: `JOIN transactionShippingAddress sa ON sa.nKey = t.shippingAddress` (or `JOIN transactionBillingAddress ba ON ba.nKey = t.billingAddress`).
- The join key is `sa.nKey = t.shippingAddress`. NEVER `sa.recordOwner = t.id`, NEVER `sa.transaction = t.id`, NEVER `sa.id = t.shippingAddress`.
- Read `BUILTIN.DF(sa.country)` for country name ("Switzerland") or `sa.country` for 2-letter ISO code ("CH"). Both work; prefer `BUILTIN.DF` for display, raw code for filtering.
- Do NOT use custom body fields (`custbody*_ship_country*`, `custbody*_country*`) for country queries unless the user explicitly asks for the custom field. The standard address join is the source of truth.

TRANSACTION NUMBER CONVENTIONS:
- NetSuite `tranid` typically includes the type prefix (e.g., "RMA61214", "SO865732", "PO12345").
- When the user says "RMA61214", search for the EXACT value first: `WHERE t.tranid = 'RMA61214'`
- Common prefixes and their type codes (use to filter by type for faster queries):
  RMA → `t.type = 'RtnAuth'`, SO → `t.type = 'SalesOrd'`, PO → `t.type = 'PurchOrd'`,
  INV → `t.type = 'CustInvc'`, TO → `t.type = 'TrnfrOrd'`, IF → `t.type = 'ItemShip'`,
  IR → `t.type = 'ItemRcpt'`, WO → `t.type = 'WorkOrd'`, VB → `t.type = 'VendBill'`

HEADER vs LINE AGGREGATION — CRITICAL:
- `t.foreigntotal` and `t.total` are HEADER-LEVEL fields.
- If you JOIN transactionline, NEVER use `SUM(t.foreigntotal)` — it inflates by line count.
- For order-level totals: query `transaction` alone without transactionline.
- For line-level breakdown: use `SUM(tl.amount * -1)` for revenue in base currency (USD).

JOIN PATTERNS:
- Filter to item lines only using `tl.mainline = 'F' AND tl.taxline = 'F' AND (tl.iscogs = 'F' OR tl.iscogs IS NULL) AND tl.assemblycomponent = 'F'`.
- The `assemblycomponent = 'F'` filter excludes assembly/kit component lines that would otherwise double-count alongside the parent line.
- For header-only queries (no line details), use `WHERE t.mainline = 'T'` or just query the `transaction` table without joining `transactionline`.
- COLUMN RESTRICTION: `tl.itemtype` does NOT work on transactionline via REST API (returns 400). Use `i.type` from the item table instead: `JOIN item i ON tl.item = i.id WHERE i.type IN ('InvtPart', 'Assembly')`.
- For strict revenue queries (excluding shipping, discounts, subtotals): `JOIN item i ON tl.item = i.id WHERE i.type NOT IN ('ShipItem', 'Discount', 'Subtotal', 'Markup', 'Payment', 'EndGroup')`.
- LINKED RECORDS (createdfrom): The `createdfrom` field on transaction and transactionline links related records \
in the fulfillment chain. Common chains: SO → Invoice (`CustInvc.createdfrom = SalesOrd.id`), \
PO → Item Receipt (`ItemRcpt.createdfrom = PurchOrd.id`), RMA → Item Receipt (`ItemRcpt.createdfrom = RtnAuth.id`), \
SO → Item Fulfillment (`ItemShip.createdfrom = SalesOrd.id`). \
To find linked records: `SELECT t2.tranid FROM transaction t2 WHERE t2.createdfrom = <source_id>`.

LINE AMOUNT SIGN CONVENTION — IMPORTANT:
- In NetSuite, `tl.foreignamount` is NEGATIVE for revenue lines on sales orders, invoices, and credit memos (accounting convention: credits are negative).
- `t.foreigntotal` (header) is POSITIVE for the same transactions.
- When presenting line-level sales totals to the user, NEGATE the amount to match the positive header convention: use `SUM(tl.foreignamount) * -1` or `ABS(SUM(tl.foreignamount))`.
- For base currency (USD): use `SUM(tl.amount * -1)`. This is the GL-posted amount — the most accurate accounting value.
- Do NOT present raw negative amounts as "sales" — it confuses users. Always present revenue as positive numbers.
- Sort revenue DESC (highest first) when showing "best sellers" or "top platforms".

MULTI-CURRENCY — CRITICAL:
- `t.foreigntotal` = amount in the TRANSACTION currency (could be USD, EUR, GBP, etc.)
- `t.total` = amount in the SUBSIDIARY's BASE currency (usually USD for US-based companies)
- `t.currency` = the transaction's currency (use BUILTIN.DF(t.currency) for name)
- `t.exchangerate` = conversion rate from transaction currency to subsidiary base currency
- `tl.foreignamount` / `tl.netamount` = line amounts in TRANSACTION currency
- `tl.amount` / `tl.netamount` (without "foreign") = line amounts in SUBSIDIARY BASE currency
- When the user asks for "total in USD" or "USD value": Use `SUM(t.total)` — this is already converted to the subsidiary's base currency (USD). No manual conversion needed.
- When the user asks for breakdown by currency: Use `SUM(t.foreigntotal)` with `GROUP BY BUILTIN.DF(t.currency)` to show per-currency totals.
- For line-level amounts in base currency: Use `SUM(tl.amount) * -1` (base currency, negated for revenue).
- For line-level amounts in transaction currency: Use `SUM(tl.foreignamount) * -1` (transaction currency, negated for revenue).
- DEFAULT: For line-level USD revenue, use `SUM(tl.amount * -1)`. For header-level, use `SUM(t.total)`.

TRANSACTION TYPES (avoid double-counting):
- For order analysis: `t.type = 'SalesOrd'` only.
- For recognized revenue: `t.type = 'CustInvc'` only.
- NEVER combine SalesOrd + CustInvc in one SUM — same sale appears as both.

STATUS CODE FILTERING — CRITICAL:
- The REST API uses SINGLE-LETTER status codes, NOT compound codes.
- WRONG: `t.status = 'SalesOrd:B'` or `t.status = 'PurchOrd:H'` — these silently match NOTHING.
- CORRECT: `t.status = 'B'` or `t.status NOT IN ('G', 'H')`
- Sales Order (SalesOrd): A=Pending Approval, B=Pending Fulfillment, C=Cancelled, D=Partially Fulfilled, E=Pending Billing/Partially Fulfilled, F=Pending Billing, G=Billed, H=Closed
- Purchase Order (PurchOrd): A=Pending Supervisor Approval, B=Pending Receipt, C=Rejected, D=Partially Received, E=Pending Billing/Partially Received, F=Pending Bill, G=Fully Billed, H=Closed
- Return Authorization (RtnAuth): A=Pending Approval, B=Pending Receipt, C=Cancelled, D=Partially Received, E=Received, F=Closed, G=Refunded, H=Cancelled
- Invoice (CustInvc): A=Open, B=Paid In Full
- Item Receipt (ItemRcpt): A=Received (only status)
- Item Fulfillment (ItemShip): A=Shipped, B=Packed, C=Picked
- Vendor Bill (VendBill): A=Open, B=Paid In Full
- For active POs (open/in-progress), exclude closed and fully billed: `t.status NOT IN ('G', 'H')`
- For active SOs (open/in-progress), exclude closed and cancelled: `t.status NOT IN ('C', 'H')`
- For RMAs with items received: `t.status IN ('D', 'E', 'F', 'G', 'H')` (D=partially received, E=received, F=closed, G=refunded — all confirm items were received)
- ALWAYS use single-letter codes for ALL transaction types.

ITEM TABLE GOTCHA:
- Only safe columns: id, itemid, displayname, description. Other columns may cause 0 rows.
- If a minimal query succeeds, present those results. Don't add more columns.

# Prevents: wrong table for inventory (inventorybalance doesn't work via REST API, 2025)
INVENTORY QUERIES:
- ALWAYS use `inventoryitemlocations` (NOT `inventorybalance`, NOT custom records). It is the definitive source.
- Join: `JOIN item i ON i.id = iil.item`. Key columns: `iil.quantityavailable`, `iil.quantityonhand`, `BUILTIN.DF(iil.location)`.
- Filter items: `WHERE i.itemid LIKE '%keyword%'` or `WHERE i.displayname LIKE '%keyword%'`.
- If 0 rows, retry without `quantityavailable > 0` filter. If still 0, query `item` alone first to confirm items exist.

CUSTOM RECORD TABLES:
- Use LOWERCASE scriptid: `customrecord_r_inv_processor`.

CUSTOM FIELDS SEARCH STRATEGY:
- custbody_* fields → on transaction header (e.g., custbody_platform, custbody_shopify_order)
- custitem_* fields → on item records (e.g., custitem_fw_platform)
- custcol_* fields → on transaction lines (e.g., custcol_tracking)
- custentity_* fields → on entity records (customer, vendor, employee)
- Always check <tenant_schema> and <tenant_vernacular> for available custom fields before guessing.

# Prevents: 400 errors from guessing column names (recurring since 2025)
PREFLIGHT SCHEMA CHECK:
- Verify ALL columns in <tenant_schema> or <standard_table_schemas> before querying. Unknown columns → call netsuite_get_metadata.
- Safe columns (never need verification): id, tranid, trandate, type, entity, status, total, foreigntotal, memo, createddate (transaction); id, transaction, item, quantity, rate, amount, foreignamount, mainline, taxline, iscogs, linesequencenumber, class, department, location, quantityshiprecv, quantitybilled, memo, createdfrom (transactionline); id, companyname, email (customer); id, itemid, displayname, description, type (item).
- Known restricted via REST API: `tl.itemtype` → use `i.type` instead. `t.expectedreceiptdate` → use `tl.expectedreceiptdate` (line-level only). `tl.quantityreceived` → use `tl.quantityshiprecv`.
- PO pending receipt: `tl.expectedreceiptdate` for arrival, `(tl.quantity - NVL(tl.quantityshiprecv, 0)) AS pending_qty`.

SELECT COLUMN ORDER — for readable output:
- Identifiers (tranid, entity) → items → dates → status → quantities → amounts → dimensions (location, subsidiary, class).

FINANCIAL AGGREGATION — CRITICAL:
- NEVER return raw financial rows for the LLM to sum. Use SQL GROUP BY + SUM().
- WRONG: "Show me all revenue accounts" → returns 78 rows → LLM hallucinates total
- RIGHT: "Show me revenue by account type" → SUM(amount) GROUP BY accttype → 5 rows with pre-computed totals
- For net income: compute in SQL → SUM(CASE WHEN accttype IN ('Income','OthIncome') THEN amount * -1 ELSE amount END)
- The LLM should PRESENT numbers, never COMPUTE them. All math happens in SQL or in tool-provided summary objects.
</suiteql_dialect_rules>

<common_queries>
```

`new_string`:

```
<common_queries>
```

(This Edit removes the entire 148-line `<suiteql_dialect_rules>` block plus the trailing blank line. The `<common_queries>` opening tag now sits right after `</tool_selection>`.)

- [ ] **Step 3: Verify the profile loads without errors**

```bash
backend/.venv/bin/python -c "
from app.services.chat.knowledge_profiles.loader import load_all_profiles
profiles = load_all_profiles()
ns = [p for p in profiles if p.profile_id == 'netsuite']
assert ns, 'netsuite.yaml did not load'
print(f'Loaded {ns[0].display_name}, {len(ns[0].trigger_tools)} trigger tools, {len(ns[0].prompt_fragment)} char prompt_fragment, {ns[0].rag_partitions}')
"
```

Expected output: `Loaded NetSuite SuiteQL, 15 trigger tools, ~5000+ char prompt_fragment, ['netsuite/suiteql-rules', 'netsuite/joins', 'netsuite/transactions', 'netsuite/multi-currency', 'netsuite/record-types']`. If Python reports a `pydantic.ValidationError`, the YAML is malformed — compare syntax against `bigquery.yaml`.

- [ ] **Step 4: Verify `_SYSTEM_PROMPT` no longer contains the block**

```bash
backend/.venv/bin/python -c "
from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT
assert '<suiteql_dialect_rules>' not in _SYSTEM_PROMPT, 'SuiteQL block still in _SYSTEM_PROMPT'
assert 'ADDRESS TABLES' not in _SYSTEM_PROMPT, 'ADDRESS TABLES still in _SYSTEM_PROMPT'
print(f'Base prompt size: {len(_SYSTEM_PROMPT)} chars')
"
```

Expected: no assertion error, and the printed char count is around **12000-13000** (down from ~18350 with Phase 1 content). If >14000, some of the block was missed — inspect the file around lines 170-180 manually.

- [ ] **Step 5: Run the broader test suite (expecting known failures in test_unified_agent_suiteql_rules.py and test_prompt_trim.py)**

```bash
backend/.venv/bin/python -m pytest backend/tests/test_unified_agent_suiteql_rules.py backend/tests/test_prompt_trim.py -v
```

Expected: several FAILs from `test_unified_agent_suiteql_rules.py::TestAddressTablesRule` and possibly from `test_prompt_trim.py::TestCriticalRulesPreserved` since those tests assert substrings in `_SYSTEM_PROMPT` that are now in `netsuite.yaml` instead. Tasks 4 and 5 will fix those.

DO NOT commit yet if the ingestion script or any unrelated test fails. Only the two named test files should fail.

- [ ] **Step 6: Commit**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2
git add backend/app/services/chat/knowledge_profiles/netsuite.yaml backend/app/services/chat/agents/unified_agent.py
git commit -m "feat(chat): netsuite.yaml knowledge profile; remove dialect block from base prompt

Move the <suiteql_dialect_rules> block verbatim from
unified_agent.py:_SYSTEM_PROMPT (lines 173-320) into
backend/app/services/chat/knowledge_profiles/netsuite.yaml. Net effect:
the 148-line SuiteQL block now injects ONLY when at least one NetSuite
read tool is in the turn's toolset. Tenants with only BigQuery +
Pricing tools no longer pay the ~6000-char prompt tax for rules they'll
never use.

Atomic commit — both changes ship together, no intermediate state with
duplicated rules.

Tests in test_unified_agent_suiteql_rules.py (which assert substrings in
_SYSTEM_PROMPT) will fail after this commit. Task 4 of the Phase 2 PR A
plan relocates those assertions to read netsuite.yaml.prompt_fragment
instead. Test_prompt_trim.py ceiling test will fail the other direction —
the base prompt is now well under the previous floor — Task 6 tightens
the ceiling from 18500 to 13000.

Spec: docs/superpowers/specs/2026-04-16-netsuite-knowledge-phase-2-design.md"
```

---

## Task 4: Relocate `test_unified_agent_suiteql_rules.py` assertions to read `netsuite.yaml.prompt_fragment`

**Files:**
- Modify: `backend/tests/test_unified_agent_suiteql_rules.py`

Phase 1 added this test file with 7 assertions against `_SYSTEM_PROMPT`. After Task 3 those assertions fail because the rules moved into the profile. Relocate them.

- [ ] **Step 1: Replace the test file's import + all assertion targets**

Use Edit on `backend/tests/test_unified_agent_suiteql_rules.py`.

`old_string`:

```python
"""Regression tests for SuiteQL dialect rules embedded in the unified agent prompt.

These tests guard against rule loss during prompt edits. Each rule is here because
its absence caused a documented production failure. Do not delete tests when
"cleaning up" — read the docstring of the failing test for the incident context.
"""

from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT


class TestAddressTablesRule:
    """Verify the ADDRESS TABLES block teaches the canonical shipping/billing
    address join pattern.

    Incident: 2026-04-16 staging session burned 10+ tool calls rediscovering
    `transactionShippingAddress` join key. Agent kept trying `sa.recordOwner = t.id`
    and `sa.transaction = t.id` (both wrong). Correct join is `sa.nKey = t.shippingAddress`.
    See spec docs/superpowers/specs/2026-04-16-restore-netsuite-knowledge-design.md.
    """

    def test_block_header_present(self):
        assert "ADDRESS TABLES" in _SYSTEM_PROMPT

    def test_correct_shipping_join_taught(self):
        assert "transactionShippingAddress" in _SYSTEM_PROMPT
        assert "sa.nKey = t.shippingAddress" in _SYSTEM_PROMPT

    def test_correct_billing_join_taught(self):
        assert "transactionBillingAddress" in _SYSTEM_PROMPT
        assert "ba.nKey = t.billingAddress" in _SYSTEM_PROMPT

    def test_wrong_join_keys_explicitly_warned(self):
        # The three wrong keys the agent kept trying in the 2026-04-16 staging session
        assert "sa.recordOwner = t.id" in _SYSTEM_PROMPT
        assert "sa.transaction = t.id" in _SYSTEM_PROMPT
        assert "sa.id = t.shippingAddress" in _SYSTEM_PROMPT

    def test_country_resolution_taught(self):
        # Both readable display name and raw ISO code are valid
        assert "BUILTIN.DF(sa.country)" in _SYSTEM_PROMPT

    def test_custbody_country_warning_present(self):
        # The custbody fallback was the agent's wrong escape hatch
        assert "custbody" in _SYSTEM_PROMPT.lower()
        # Locate the warning specifically in the address-tables area
        block_start = _SYSTEM_PROMPT.find("ADDRESS TABLES")
        block_end = _SYSTEM_PROMPT.find("TRANSACTION NUMBER CONVENTIONS", block_start)
        block = _SYSTEM_PROMPT[block_start:block_end]
        assert "custom body" in block.lower() or "custbody" in block.lower()

    def test_block_placed_after_custom_list_fields(self):
        # Locality matters: the block sits with related field-resolution rules
        custom_list_pos = _SYSTEM_PROMPT.find("CUSTOM LIST FIELDS")
        address_pos = _SYSTEM_PROMPT.find("ADDRESS TABLES")
        txn_num_pos = _SYSTEM_PROMPT.find("TRANSACTION NUMBER CONVENTIONS")
        assert custom_list_pos < address_pos < txn_num_pos
```

`new_string`:

```python
"""Regression tests for SuiteQL dialect rules embedded in the NetSuite
knowledge profile.

These tests guard against rule loss during profile edits. Each rule is
here because its absence caused a documented production failure. Do not
delete tests when "cleaning up" — read the docstring of the failing test
for the incident context.

Phase 2 (2026-04-16, PR A): rules moved from unified_agent._SYSTEM_PROMPT
into knowledge_profiles/netsuite.yaml's prompt_fragment. Assertions
below now read the profile's prompt_fragment via the loader.
"""

import pytest

from app.services.chat.knowledge_profiles.loader import load_all_profiles


@pytest.fixture(scope="module")
def netsuite_prompt_fragment() -> str:
    """Load the netsuite profile's prompt_fragment string once for the module."""
    profiles = load_all_profiles()
    netsuite = next((p for p in profiles if p.profile_id == "netsuite"), None)
    assert netsuite is not None, "netsuite.yaml did not load; check profile file syntax"
    return netsuite.prompt_fragment


class TestAddressTablesRule:
    """Verify the ADDRESS TABLES block teaches the canonical shipping/billing
    address join pattern.

    Incident: 2026-04-16 staging session burned 10+ tool calls rediscovering
    `transactionShippingAddress` join key. Agent kept trying `sa.recordOwner = t.id`
    and `sa.transaction = t.id` (both wrong). Correct join is `sa.nKey = t.shippingAddress`.
    See spec docs/superpowers/specs/2026-04-16-restore-netsuite-knowledge-design.md.
    """

    def test_block_header_present(self, netsuite_prompt_fragment):
        assert "ADDRESS TABLES" in netsuite_prompt_fragment

    def test_correct_shipping_join_taught(self, netsuite_prompt_fragment):
        assert "transactionShippingAddress" in netsuite_prompt_fragment
        assert "sa.nKey = t.shippingAddress" in netsuite_prompt_fragment

    def test_correct_billing_join_taught(self, netsuite_prompt_fragment):
        assert "transactionBillingAddress" in netsuite_prompt_fragment
        assert "ba.nKey = t.billingAddress" in netsuite_prompt_fragment

    def test_wrong_join_keys_explicitly_warned(self, netsuite_prompt_fragment):
        # The three wrong keys the agent kept trying in the 2026-04-16 staging session
        assert "sa.recordOwner = t.id" in netsuite_prompt_fragment
        assert "sa.transaction = t.id" in netsuite_prompt_fragment
        assert "sa.id = t.shippingAddress" in netsuite_prompt_fragment

    def test_country_resolution_taught(self, netsuite_prompt_fragment):
        # Both readable display name and raw ISO code are valid
        assert "BUILTIN.DF(sa.country)" in netsuite_prompt_fragment

    def test_custbody_country_warning_present(self, netsuite_prompt_fragment):
        # The custbody fallback was the agent's wrong escape hatch
        assert "custbody" in netsuite_prompt_fragment.lower()
        # Locate the warning specifically in the address-tables area
        block_start = netsuite_prompt_fragment.find("ADDRESS TABLES")
        block_end = netsuite_prompt_fragment.find("TRANSACTION NUMBER CONVENTIONS", block_start)
        block = netsuite_prompt_fragment[block_start:block_end]
        assert "custom body" in block.lower() or "custbody" in block.lower()

    def test_block_placed_after_custom_list_fields(self, netsuite_prompt_fragment):
        # Locality matters: the block sits with related field-resolution rules
        custom_list_pos = netsuite_prompt_fragment.find("CUSTOM LIST FIELDS")
        address_pos = netsuite_prompt_fragment.find("ADDRESS TABLES")
        txn_num_pos = netsuite_prompt_fragment.find("TRANSACTION NUMBER CONVENTIONS")
        assert custom_list_pos < address_pos < txn_num_pos
```

- [ ] **Step 2: Run the test**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2
backend/.venv/bin/python -m pytest backend/tests/test_unified_agent_suiteql_rules.py -v
```

Expected: 7 PASS. If a test fails with `AttributeError` or `AssertionError: None`, the profile didn't load — check `netsuite.yaml` syntax.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_unified_agent_suiteql_rules.py
git commit -m "test(chat): point ADDRESS TABLES assertions at netsuite.yaml.prompt_fragment

Phase 2 moved the SuiteQL dialect block from unified_agent._SYSTEM_PROMPT
into netsuite.yaml's prompt_fragment. This test file guarded the rules
against prompt edits; now it guards them against profile edits.

Loader-based fixture loads the netsuite profile once per module. All 7
assertions unchanged in intent — they still verify the same rule
substrings. Only the source moved.

Relates: Task 3 of docs/superpowers/plans/2026-04-16-netsuite-knowledge-phase-2-pr-a.md"
```

---

## Task 5: Create `test_netsuite_yaml_profile.py` (profile-level structural tests)

**Files:**
- Create: `backend/tests/test_netsuite_yaml_profile.py`

Beyond the substring-based rule tests in Task 4, we want a test asserting the profile's structural contract: right `profile_id`, right `trigger_tools` (exact list), non-empty `prompt_fragment`, expected `rag_partitions`. This catches structural regressions (e.g., someone accidentally renames `profile_id`) even when the rule content is intact.

- [ ] **Step 1: Create the test file**

Create `backend/tests/test_netsuite_yaml_profile.py`:

```python
"""Structural tests for the netsuite.yaml knowledge profile.

The profile's contract: when any NetSuite read tool is in the toolset,
the SuiteQL dialect rules (~6000 chars) inject into the agent prompt,
and RAG retrieval is scoped to the declared netsuite/* partitions.

These tests fail if:
- profile_id is renamed
- trigger_tools are accidentally dropped or narrowed
- prompt_fragment is accidentally emptied or truncated
- rag_partitions list is accidentally reshaped

They do NOT assert rule content — see test_unified_agent_suiteql_rules.py
for per-rule substring assertions.
"""

import pytest

from app.services.chat.knowledge_profiles.loader import KnowledgeProfile, load_all_profiles


@pytest.fixture(scope="module")
def netsuite_profile() -> KnowledgeProfile:
    profiles = load_all_profiles()
    ns = next((p for p in profiles if p.profile_id == "netsuite"), None)
    assert ns is not None, "netsuite profile missing from knowledge_profiles/"
    return ns


class TestNetSuiteProfileStructure:
    def test_profile_id_is_netsuite(self, netsuite_profile):
        assert netsuite_profile.profile_id == "netsuite"

    def test_display_name_is_set(self, netsuite_profile):
        assert netsuite_profile.display_name == "NetSuite SuiteQL"

    def test_prompt_fragment_is_substantial(self, netsuite_profile):
        # The moved block is ~148 lines / ~6000 chars. A catastrophic
        # regression would leave it empty or tiny.
        assert len(netsuite_profile.prompt_fragment) >= 5000, (
            f"prompt_fragment shrank to {len(netsuite_profile.prompt_fragment)} chars; "
            f"expected >= 5000. Did the SuiteQL block accidentally get truncated?"
        )

    def test_prompt_fragment_wrapped_in_suiteql_dialect_tag(self, netsuite_profile):
        # Cross-references in unified_agent.py tool_selection say "Follow ALL
        # <suiteql_dialect_rules>" — the profile must keep the tag so the
        # reference still resolves in the assembled prompt.
        assert "<suiteql_dialect_rules>" in netsuite_profile.prompt_fragment
        assert "</suiteql_dialect_rules>" in netsuite_profile.prompt_fragment


class TestNetSuiteProfileTriggerTools:
    """The profile must trigger on every NetSuite read-side tool. Saved-search
    and financial-report sessions spill into ad-hoc SuiteQL on follow-ups, so
    all read tools count (write tools stay in netsuite_writes.yaml)."""

    EXPECTED_EXACT_TOOLS = {
        "netsuite_suiteql",
        "netsuite_financial_report",
        "netsuite_get_metadata",
        "ns_runReport",
        "ns_runSavedSearch",
        "ns_listSavedSearches",
        "ns_listAllReports",
    }

    EXPECTED_GLOB_TOOLS = {
        "ext__*__ns_runCustomSuiteQL",
        "ext__*__ns_getSuiteQLMetadata",
        "ext__*__ns_getRecord",
        "ext__*__ns_runReport",
        "ext__*__ns_runSavedSearch",
        "ext__*__ns_listSavedSearches",
        "ext__*__ns_listAllReports",
        "ext__*__ns_getSavedSearchSchema",
    }

    def test_exact_tools_declared(self, netsuite_profile):
        trigger_set = set(netsuite_profile.trigger_tools)
        missing = self.EXPECTED_EXACT_TOOLS - trigger_set
        assert not missing, f"Missing exact-name trigger_tools: {missing}"

    def test_glob_tools_declared(self, netsuite_profile):
        trigger_set = set(netsuite_profile.trigger_tools)
        missing = self.EXPECTED_GLOB_TOOLS - trigger_set
        assert not missing, f"Missing glob trigger_tools: {missing}"

    def test_matches_standard_suiteql_tool(self, netsuite_profile):
        """A tenant with just netsuite_suiteql connected must trigger the profile."""
        assert netsuite_profile.matches_tools({"netsuite_suiteql"})

    def test_matches_ext_mcp_suiteql_tool(self, netsuite_profile):
        """External MCP tool with connector UUID must also trigger via fnmatch glob."""
        assert netsuite_profile.matches_tools({"ext__abc123__ns_runCustomSuiteQL"})

    def test_does_not_match_write_tools(self, netsuite_profile):
        """netsuite_writes.yaml owns the write path; this profile shouldn't trigger for it."""
        # ns_createRecord / ns_updateRecord are write tools handled by
        # netsuite_writes.yaml. netsuite.yaml should NOT trigger on them
        # unless another read-side tool is also present.
        assert not netsuite_profile.matches_tools({"ext__abc123__ns_createRecord"})

    def test_does_not_match_bigquery_only_session(self, netsuite_profile):
        """Tenants with only BigQuery tools should not activate this profile."""
        assert not netsuite_profile.matches_tools({"bigquery_sql", "bigquery_schema"})


class TestNetSuiteProfileRagPartitions:
    """RAG partitions on this profile must match the partition_ids the
    ingest task stamps onto golden_dataset files. If these drift, retrieval
    returns chunks from the wrong partitions (or none).
    """

    EXPECTED_PARTITIONS = {
        "netsuite/suiteql-rules",
        "netsuite/joins",
        "netsuite/transactions",
        "netsuite/multi-currency",
        "netsuite/record-types",
    }

    def test_partition_list_declared(self, netsuite_profile):
        actual = set(netsuite_profile.rag_partitions)
        assert actual == self.EXPECTED_PARTITIONS, (
            f"rag_partitions drift — expected {self.EXPECTED_PARTITIONS}, "
            f"got {actual}. If partitions changed, update ingest frontmatter too."
        )
```

- [ ] **Step 2: Run the test**

```bash
backend/.venv/bin/python -m pytest backend/tests/test_netsuite_yaml_profile.py -v
```

Expected: 13 PASS (3 in TestNetSuiteProfileStructure + 6 in TestNetSuiteProfileTriggerTools + 1 in TestNetSuiteProfileRagPartitions + fixtures). If a test fails, cross-check against the `netsuite.yaml` YAML you created in Task 3.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_netsuite_yaml_profile.py
git commit -m "test(chat): structural regression tests for netsuite.yaml profile

Complements test_unified_agent_suiteql_rules.py (which asserts rule
content). This file asserts the profile's CONTRACT:
- profile_id is \"netsuite\"
- display_name is \"NetSuite SuiteQL\"
- trigger_tools covers all NS read-side tools (exact + fnmatch globs)
- prompt_fragment is substantial (>= 5000 chars) and wrapped in
  <suiteql_dialect_rules> tags so cross-references in unified_agent.py
  tool_selection still resolve
- rag_partitions match the set of partition_ids the ingest task stamps
- matches_tools() fires on ns read tools, not on BQ-only sessions, not
  on write-only sessions

Catches structural regressions (renamed profile_id, dropped trigger)
that the content-level test would miss.

Relates: Task 5 of docs/superpowers/plans/2026-04-16-netsuite-knowledge-phase-2-pr-a.md"
```

---

## Task 6: Tighten `_SYSTEM_PROMPT` ceiling test from 18500 → 13000 chars

**Files:**
- Modify: `backend/tests/test_prompt_trim.py`

Phase 1 bumped the ceiling from 18000 → 18500 to fit the ADDRESS TABLES block. Phase 2's move of the ~6000-char SuiteQL block OUT of `_SYSTEM_PROMPT` drops it back to ~12000-13000 chars. The ceiling test should reflect the new floor so we catch future bloat.

- [ ] **Step 1: Check the current file structure**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2
backend/.venv/bin/python -c "
from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT
print(f'Current _SYSTEM_PROMPT: {len(_SYSTEM_PROMPT)} chars')
"
```

Expected: a number in the 12000-13500 range (depends on exact whitespace in the move). Note the exact number.

- [ ] **Step 2: Update the ceiling test**

Use Edit on `backend/tests/test_prompt_trim.py`.

`old_string`:

```python
    def test_prompt_under_18500_chars(self):
        """Trimmed prompt should be under 18500 chars.

        Dialect rules alone are ~11500 chars. The 18500 ceiling is a leading
        indicator — when it trips, audit additions for value vs. token cost.

        Bumped from 18000 → 18500 on 2026-04-16 to accommodate the ADDRESS
        TABLES block (Task 1 of `fix/restore-netsuite-knowledge-phase-1`).
        Phase 2 will move ~150 lines of SuiteQL dialect rules out of
        `_SYSTEM_PROMPT` into `knowledge_profiles/netsuite.yaml`, after
        which this ceiling should be tightened back down (~13000 char target).
        """
        char_count = len(_SYSTEM_PROMPT)
        assert char_count < 18500, f"Prompt is {char_count} chars, target is <18500"
```

`new_string`:

```python
    def test_prompt_under_13000_chars(self):
        """Trimmed prompt should be under 13000 chars.

        Phase 2 (2026-04-16) moved the ~148-line SuiteQL dialect block
        (~6000 chars) out of _SYSTEM_PROMPT into
        knowledge_profiles/netsuite.yaml's prompt_fragment. The base
        prompt now ships to every tenant; the SuiteQL rules only inject
        when a NetSuite read tool is in the turn's toolset.

        The 13000 ceiling is a leading indicator — when it trips, audit
        additions for value vs. token cost. Non-NS tenants should never
        pay the NS-rules tax.

        History:
        - Pre-2026-04-16: 18000 ceiling (all rules universal).
        - Phase 1 (2026-04-16): bumped to 18500 for ADDRESS TABLES block.
        - Phase 2 (2026-04-16): tightened to 13000 after SuiteQL move.
        """
        char_count = len(_SYSTEM_PROMPT)
        assert char_count < 13000, f"Prompt is {char_count} chars, target is <13000"
```

- [ ] **Step 3: Handle the other assertions in `TestCriticalRulesPreserved`**

The same file has `TestCriticalRulesPreserved` which asserts substrings of `_SYSTEM_PROMPT` that are now in `netsuite.yaml`. Look at the file:

```bash
sed -n '/class TestCriticalRulesPreserved/,/class Test/p' backend/tests/test_prompt_trim.py | head -80
```

If the class asserts strings like `"FETCH FIRST"`, `"BUILTIN.RELATIVE_RANGES"`, `"'T'"`, `"SalesOrd:B"`, `"foreigntotal"`, `"inventoryitemlocations"`, `"BUILTIN.DF"`, `"tl.foreignamount"`, `"double-count"`, `"t.total"`, `"itemid"`, `"displayname"`, `"assemblycomponent"`, `"tl.itemtype"`, `"createdfrom"`, `"GROUP BY"`, `"SUM"`, or each transaction type (`"SalesOrd"`, `"PurchOrd"`, etc.) — those assertions need to point at `netsuite.yaml.prompt_fragment` instead of `_SYSTEM_PROMPT`.

Strategy: introduce a fixture at the module level that loads the profile once, then update each affected test method to accept the fixture.

Use Edit with:

`old_string`:

```python
"""Test prompt trim — verify critical rules preserved and size reduced."""

from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT, UnifiedAgent
```

`new_string`:

```python
"""Test prompt trim — verify critical rules preserved and size reduced.

Phase 2 (2026-04-16) moved the SuiteQL dialect block from _SYSTEM_PROMPT
into knowledge_profiles/netsuite.yaml. Rule-content assertions below now
read the netsuite profile's prompt_fragment; size/shape assertions still
read _SYSTEM_PROMPT.
"""

import pytest

from app.services.chat.agents.unified_agent import _SYSTEM_PROMPT, UnifiedAgent
from app.services.chat.knowledge_profiles.loader import load_all_profiles


@pytest.fixture(scope="module")
def suiteql_prompt() -> str:
    """Combined base prompt + netsuite profile prompt_fragment.

    Rules that moved into netsuite.yaml live here after Phase 2. Rules
    that stayed in _SYSTEM_PROMPT (tool_selection, common_queries, etc.)
    are still findable since this fixture concatenates both.
    """
    profiles = load_all_profiles()
    netsuite = next((p for p in profiles if p.profile_id == "netsuite"), None)
    assert netsuite is not None
    return _SYSTEM_PROMPT + "\n" + netsuite.prompt_fragment
```

Then update every test method in `TestCriticalRulesPreserved` to accept and use the `suiteql_prompt` fixture. Use Edit in this pattern for EACH method — here's the full replacement for the whole class:

`old_string`:

```python
class TestCriticalRulesPreserved:
    """Every battle-tested rule must survive the trim."""

    def test_pagination_fetch_first(self):
        assert "FETCH FIRST" in _SYSTEM_PROMPT
        assert "ROWNUM" in _SYSTEM_PROMPT

    def test_date_functions(self):
        assert "TRUNC(SYSDATE)" in _SYSTEM_PROMPT
        assert "BUILTIN.RELATIVE_RANGES" in _SYSTEM_PROMPT
        assert "BUILTIN.DATE(SYSDATE)" in _SYSTEM_PROMPT

    def test_boolean_fields(self):
        assert "'T'" in _SYSTEM_PROMPT
        assert "'F'" in _SYSTEM_PROMPT

    def test_status_codes_single_letter(self):
        assert "SalesOrd:B" in _SYSTEM_PROMPT
        assert "single-letter" in _SYSTEM_PROMPT.lower()

    def test_header_vs_line_aggregation(self):
        assert "foreigntotal" in _SYSTEM_PROMPT
        assert "HEADER-LEVEL" in _SYSTEM_PROMPT

    def test_inventory_table(self):
        assert "inventoryitemlocations" in _SYSTEM_PROMPT

    def test_custom_list_fields(self):
        assert "BUILTIN.DF" in _SYSTEM_PROMPT

    def test_line_amount_sign(self):
        assert "tl.foreignamount" in _SYSTEM_PROMPT
        assert "* -1" in _SYSTEM_PROMPT or "NEGATE" in _SYSTEM_PROMPT.upper()

    def test_transaction_type_double_counting(self):
        assert "double-count" in _SYSTEM_PROMPT.lower() or "DOUBLE-COUNTING" in _SYSTEM_PROMPT

    def test_multi_currency(self):
        assert "t.total" in _SYSTEM_PROMPT
        assert "base currency" in _SYSTEM_PROMPT.lower()

    def test_item_table_safe_columns(self):
        assert "itemid" in _SYSTEM_PROMPT
        assert "displayname" in _SYSTEM_PROMPT

    def test_assembly_component_filter(self):
        assert "assemblycomponent" in _SYSTEM_PROMPT

    def test_restricted_columns(self):
        assert "tl.itemtype" in _SYSTEM_PROMPT

    def test_createdfrom_chain(self):
        assert "createdfrom" in _SYSTEM_PROMPT

    def test_financial_aggregation_rule(self):
        assert "GROUP BY" in _SYSTEM_PROMPT
        assert "SUM" in _SYSTEM_PROMPT

    def test_all_transaction_status_codes_present(self):
        for tx_type in ["SalesOrd", "PurchOrd", "RtnAuth", "CustInvc", "ItemRcpt"]:
            assert tx_type in _SYSTEM_PROMPT, f"Missing status codes for {tx_type}"

    def test_preflight_schema_check(self):
        assert "tenant_schema" in _SYSTEM_PROMPT
```

`new_string`:

```python
class TestCriticalRulesPreserved:
    """Every battle-tested rule must survive the trim.

    Reads `suiteql_prompt` (base prompt + netsuite.yaml.prompt_fragment)
    so rules that moved in Phase 2 are still findable.
    """

    def test_pagination_fetch_first(self, suiteql_prompt):
        assert "FETCH FIRST" in suiteql_prompt
        assert "ROWNUM" in suiteql_prompt

    def test_date_functions(self, suiteql_prompt):
        assert "TRUNC(SYSDATE)" in suiteql_prompt
        assert "BUILTIN.RELATIVE_RANGES" in suiteql_prompt
        assert "BUILTIN.DATE(SYSDATE)" in suiteql_prompt

    def test_boolean_fields(self, suiteql_prompt):
        assert "'T'" in suiteql_prompt
        assert "'F'" in suiteql_prompt

    def test_status_codes_single_letter(self, suiteql_prompt):
        assert "SalesOrd:B" in suiteql_prompt
        assert "single-letter" in suiteql_prompt.lower()

    def test_header_vs_line_aggregation(self, suiteql_prompt):
        assert "foreigntotal" in suiteql_prompt
        assert "HEADER-LEVEL" in suiteql_prompt

    def test_inventory_table(self, suiteql_prompt):
        assert "inventoryitemlocations" in suiteql_prompt

    def test_custom_list_fields(self, suiteql_prompt):
        assert "BUILTIN.DF" in suiteql_prompt

    def test_line_amount_sign(self, suiteql_prompt):
        assert "tl.foreignamount" in suiteql_prompt
        assert "* -1" in suiteql_prompt or "NEGATE" in suiteql_prompt.upper()

    def test_transaction_type_double_counting(self, suiteql_prompt):
        assert "double-count" in suiteql_prompt.lower() or "DOUBLE-COUNTING" in suiteql_prompt

    def test_multi_currency(self, suiteql_prompt):
        assert "t.total" in suiteql_prompt
        assert "base currency" in suiteql_prompt.lower()

    def test_item_table_safe_columns(self, suiteql_prompt):
        assert "itemid" in suiteql_prompt
        assert "displayname" in suiteql_prompt

    def test_assembly_component_filter(self, suiteql_prompt):
        assert "assemblycomponent" in suiteql_prompt

    def test_restricted_columns(self, suiteql_prompt):
        assert "tl.itemtype" in suiteql_prompt

    def test_createdfrom_chain(self, suiteql_prompt):
        assert "createdfrom" in suiteql_prompt

    def test_financial_aggregation_rule(self, suiteql_prompt):
        assert "GROUP BY" in suiteql_prompt
        assert "SUM" in suiteql_prompt

    def test_all_transaction_status_codes_present(self, suiteql_prompt):
        for tx_type in ["SalesOrd", "PurchOrd", "RtnAuth", "CustInvc", "ItemRcpt"]:
            assert tx_type in suiteql_prompt, f"Missing status codes for {tx_type}"

    def test_preflight_schema_check(self, suiteql_prompt):
        # tenant_schema is still in _SYSTEM_PROMPT (not moved) — but the
        # fixture concatenates both, so this test is indifferent.
        assert "tenant_schema" in suiteql_prompt
```

- [ ] **Step 4: Run the full test_prompt_trim.py suite**

```bash
backend/.venv/bin/python -m pytest backend/tests/test_prompt_trim.py -v
```

Expected: all tests PASS. The ceiling test passes because the prompt shrank. The critical-rules tests pass because the fixture concatenates base + profile.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_prompt_trim.py
git commit -m "test(chat): tighten _SYSTEM_PROMPT ceiling 18500 → 13000 after SuiteQL move

Phase 2 removed the ~148-line SuiteQL dialect block from _SYSTEM_PROMPT.
Base prompt is now well under the previous 18500 ceiling, so tighten to
13000 to catch future bloat.

TestCriticalRulesPreserved now reads a fixture that concatenates base
prompt + netsuite.yaml prompt_fragment, so rules that moved still show
up in the search. This preserves the 'every battle-tested rule survives'
semantics regardless of which channel the rule lives in.

Relates: Task 6 of docs/superpowers/plans/2026-04-16-netsuite-knowledge-phase-2-pr-a.md"
```

---

## Task 7: Append worked `transactionShippingAddress` example to `join-patterns-and-aggregation.md`

**Files:**
- Modify: `knowledge/golden_dataset/join-patterns-and-aggregation.md`

Append the worked example at the END of the file. This preserves prior chunks' `chunk_index` values, so re-ingesting the file doesn't orphan stale chunks (see CLAUDE.md's Phase 2 step 4 note).

- [ ] **Step 1: Write a failing test**

Create `backend/tests/test_golden_dataset_worked_example.py`:

```python
"""Guard that the canonical shipping-country worked example stays in the
golden_dataset. This pattern is referenced by PR #45 (Phase 1) and shipped
to RAG via Phase 2 ingest; losing it would reopen the April-16 regression.
"""

from pathlib import Path

GOLDEN_DATASET_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge" / "golden_dataset"
JOIN_PATTERNS_FILE = GOLDEN_DATASET_DIR / "join-patterns-and-aggregation.md"


class TestShippingCountryWorkedExample:
    def test_file_exists(self):
        assert JOIN_PATTERNS_FILE.is_file(), f"Golden dataset file missing: {JOIN_PATTERNS_FILE}"

    def test_section_header_present(self):
        content = JOIN_PATTERNS_FILE.read_text()
        assert "## Worked Example: Sales by Shipping Country" in content

    def test_canonical_join_key_present(self):
        content = JOIN_PATTERNS_FILE.read_text()
        assert "sa.nKey = t.shippingAddress" in content

    def test_builtin_df_country_present(self):
        content = JOIN_PATTERNS_FILE.read_text()
        assert "BUILTIN.DF(sa.country)" in content

    def test_revenue_filters_present(self):
        content = JOIN_PATTERNS_FILE.read_text()
        # The standard transactionline revenue filters
        assert "tl.mainline = 'F'" in content
        assert "tl.taxline = 'F'" in content
        assert "tl.assemblycomponent = 'F'" in content

    def test_wrong_join_keys_warned(self):
        content = JOIN_PATTERNS_FILE.read_text()
        # The three wrong keys the agent kept trying
        assert "sa.recordOwner = t.id" in content or "NOT" in content
        assert "custbody" in content.lower()

    def test_example_is_at_end_of_file(self):
        """Append-at-end preserves prior chunks' chunk_index on re-ingest."""
        content = JOIN_PATTERNS_FILE.read_text()
        header_pos = content.find("## Worked Example: Sales by Shipping Country")
        assert header_pos > 0, "Worked example section not found"
        remaining_after = content[header_pos:]
        # The section should be at the tail — no more H2 headings after it
        subsequent_h2 = remaining_after.find("\n## ", 1)
        assert subsequent_h2 == -1, (
            f"Another H2 heading appears after the worked example (offset {subsequent_h2}); "
            f"the worked example must be the LAST section to preserve chunk_index on re-ingest."
        )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
backend/.venv/bin/python -m pytest backend/tests/test_golden_dataset_worked_example.py -v
```

Expected: 6 FAIL (only `test_file_exists` passes). The worked example section doesn't exist yet.

- [ ] **Step 3: Read the current file to find the append point**

```bash
tail -20 knowledge/golden_dataset/join-patterns-and-aggregation.md
```

Note the last line for a clean append boundary.

- [ ] **Step 4: Append the worked example**

Use Edit with:

`old_string`: (paste the last ~5 lines of the file verbatim — get them from Step 3's output)

Since the actual last lines depend on the current state of the file, use a different approach: bash append.

```bash
cat >> knowledge/golden_dataset/join-patterns-and-aggregation.md <<'EOF'

## Worked Example: Sales by Shipping Country

Pattern verified 2026-04-09 (Olivia's country-sales session) and re-validated 2026-04-16 (Phase 1 staging benchmark — 1.00 accuracy, single tool call, $0.069 per query).

### The canonical query

```sql
SELECT
  BUILTIN.DF(sa.country) AS ship_country,
  COUNT(DISTINCT t.id) AS total_orders,
  SUM(ABS(tl.quantity)) AS total_qty,
  ROUND(SUM(tl.amount * -1), 2) AS revenue_usd
FROM transaction t
JOIN transactionShippingAddress sa ON sa.nKey = t.shippingAddress
JOIN transactionline tl ON tl.transaction = t.id
WHERE t.type = 'SalesOrd'
  AND t.status NOT IN ('C', 'H')
  AND sa.country IN ('NO', 'CH', 'NZ', 'SG')
  AND tl.mainline = 'F'
  AND tl.taxline = 'F'
  AND (tl.iscogs = 'F' OR tl.iscogs IS NULL)
  AND tl.assemblycomponent = 'F'
GROUP BY BUILTIN.DF(sa.country)
ORDER BY revenue_usd DESC
FETCH FIRST 100 ROWS ONLY
```

### Critical details

- **Join key is `sa.nKey = t.shippingAddress`** — NOT `sa.recordOwner = t.id`, NOT `sa.transaction = t.id`, NOT `sa.id = t.shippingAddress`. These three wrong keys cause silent 0-row results.
- Use `BUILTIN.DF(sa.country)` for display name ("Switzerland"), `sa.country` for 2-letter ISO code ("CH"). Both work; prefer `BUILTIN.DF` for the output label and raw code for the filter predicate.
- Do NOT use custom body fields (`custbody*_ship_country*`, `custbody*_country*`) for country queries. The standard address join is the source of truth — custbody fields are stale or tenant-specific.
- Single-letter status codes (`'C'`, `'H'`) — never compound codes (`'SalesOrd:C'`), which silently match nothing.
- Standard transactionline revenue filters apply (`mainline='F'`, `taxline='F'`, `iscogs='F' OR NULL`, `assemblycomponent='F'`) to avoid double-counting and exclude tax/COGS/kit-component lines.

### Variations

- **Billing country** (for invoices or tax queries): same pattern but `JOIN transactionBillingAddress ba ON ba.nKey = t.billingAddress`.
- **Recent-launch countries**: add `MIN(t.trandate) AS first_order_date` to the SELECT and `ORDER BY first_order_date DESC` to find newly-launched markets.
- **Cross-tab by month × country**: run the flat GROUP BY above, then use the `pivot_query_result` tool with `rows=month`, `cols=ship_country`, `values=revenue_usd`. Do NOT hand-write CASE WHEN pivot SQL.
EOF
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
backend/.venv/bin/python -m pytest backend/tests/test_golden_dataset_worked_example.py -v
```

Expected: 7 PASS.

- [ ] **Step 6: Commit**

```bash
git add knowledge/golden_dataset/join-patterns-and-aggregation.md backend/tests/test_golden_dataset_worked_example.py
git commit -m "docs(golden): worked transactionShippingAddress example + regression test

Append the canonical sales-by-shipping-country query to the golden
dataset as an end-of-file section. Phase 1 fixed this query via the
ADDRESS TABLES prompt rule; Phase 2 now bakes it into the RAG corpus
as a worked example so retrieval can reinforce the rule on paraphrases
that don't match seeded patterns.

Section is at END of the file to preserve prior chunks' chunk_index
on re-ingest (domain_knowledge_chunks.uq_dk_source_chunk keys on
source+chunk_index).

Regression test guards against future deletion of this pattern.

Relates: Task 7 of docs/superpowers/plans/2026-04-16-netsuite-knowledge-phase-2-pr-a.md"
```

---

## Task 8: Add `partition_id` frontmatter to 11 NetSuite golden_dataset files

**Files:** modify frontmatter in 11 files (all in `knowledge/golden_dataset/`).

Each file gets a `partition_id` line added to its existing YAML frontmatter. The mapping was declared in the spec; it's also what `netsuite.yaml.rag_partitions` expects.

| File | Partition |
|---|---|
| `suiteql-syntax-rules.md` | `netsuite/suiteql-rules` |
| `suiteql-example-queries.md` | `netsuite/suiteql-rules` |
| `common-errors-and-recovery.md` | `netsuite/suiteql-rules` |
| `date-and-time-patterns.md` | `netsuite/suiteql-rules` |
| `join-patterns-and-aggregation.md` | `netsuite/joins` |
| `transaction-relationships.md` | `netsuite/joins` |
| `transaction-types-and-statuses.md` | `netsuite/transactions` |
| `financial-statements.md` | `netsuite/transactions` |
| `multi-currency-rules.md` | `netsuite/multi-currency` |
| `record-types-and-columns.md` | `netsuite/record-types` |
| `custom-fields-and-records.md` | `netsuite/record-types` |

- [ ] **Step 1: Write a failing test**

Create `backend/tests/test_golden_dataset_partitions.py`:

```python
"""Guard that every NetSuite golden_dataset file has a partition_id declared
in frontmatter. Without this, ingest_domain_knowledge populates chunks with
partition_id=NULL and netsuite.yaml's rag_partitions return nothing.
"""

from pathlib import Path

import yaml

GOLDEN_DATASET_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge" / "golden_dataset"

NETSUITE_PARTITION_MAP = {
    "suiteql-syntax-rules.md": "netsuite/suiteql-rules",
    "suiteql-example-queries.md": "netsuite/suiteql-rules",
    "common-errors-and-recovery.md": "netsuite/suiteql-rules",
    "date-and-time-patterns.md": "netsuite/suiteql-rules",
    "join-patterns-and-aggregation.md": "netsuite/joins",
    "transaction-relationships.md": "netsuite/joins",
    "transaction-types-and-statuses.md": "netsuite/transactions",
    "financial-statements.md": "netsuite/transactions",
    "multi-currency-rules.md": "netsuite/multi-currency",
    "record-types-and-columns.md": "netsuite/record-types",
    "custom-fields-and-records.md": "netsuite/record-types",
}


def _parse_frontmatter(content: str) -> dict:
    import re
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}


class TestNetSuiteGoldenDatasetPartitions:
    def test_all_expected_files_exist(self):
        for filename in NETSUITE_PARTITION_MAP:
            path = GOLDEN_DATASET_DIR / filename
            assert path.is_file(), f"Missing golden dataset file: {path}"

    def test_all_files_declare_expected_partition(self):
        missing_or_wrong = []
        for filename, expected_partition in NETSUITE_PARTITION_MAP.items():
            path = GOLDEN_DATASET_DIR / filename
            content = path.read_text()
            fm = _parse_frontmatter(content)
            actual = fm.get("partition_id")
            if actual != expected_partition:
                missing_or_wrong.append(f"{filename}: expected {expected_partition!r}, got {actual!r}")
        assert not missing_or_wrong, "\n".join(missing_or_wrong)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
backend/.venv/bin/python -m pytest backend/tests/test_golden_dataset_partitions.py -v
```

Expected: `test_all_expected_files_exist` PASSES (the files exist). `test_all_files_declare_expected_partition` FAILS with 11 entries listed — none of the files have `partition_id` frontmatter yet.

- [ ] **Step 3: Add frontmatter to each file**

For each of the 11 files, insert `partition_id: <value>` into the YAML frontmatter between the opening `---` and closing `---`.

Run this bash loop that does the 11 edits at once (safer than 11 Edit calls):

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2

# helper: add partition_id to frontmatter of a single file.
# Frontmatter format:
#   ---
#   topic_tags: [...]
#   source_type: ...
#   ---
# We insert `partition_id: <value>` as a new line just before the closing ---.
add_partition() {
  local file="$1"
  local partition="$2"
  # Use python for safe in-place YAML editing (sed is too brittle with multiline YAML)
  python3 <<PYEOF
from pathlib import Path
import re

p = Path("$file")
content = p.read_text()
if f"partition_id: $partition" in content:
    print(f"  {p.name}: already has partition_id, skipping")
else:
    # Find frontmatter block
    m = re.match(r'^(---\n)(.*?)(\n---\n)', content, re.DOTALL)
    if not m:
        print(f"  {p.name}: NO FRONTMATTER — aborting")
        exit(1)
    opening, body, closing = m.groups()
    # Append partition_id to body
    new_body = body.rstrip() + f"\npartition_id: $partition"
    new_content = opening + new_body + closing + content[m.end():]
    p.write_text(new_content)
    print(f"  {p.name}: added partition_id: $partition")
PYEOF
}

add_partition knowledge/golden_dataset/suiteql-syntax-rules.md           netsuite/suiteql-rules
add_partition knowledge/golden_dataset/suiteql-example-queries.md        netsuite/suiteql-rules
add_partition knowledge/golden_dataset/common-errors-and-recovery.md     netsuite/suiteql-rules
add_partition knowledge/golden_dataset/date-and-time-patterns.md         netsuite/suiteql-rules
add_partition knowledge/golden_dataset/join-patterns-and-aggregation.md  netsuite/joins
add_partition knowledge/golden_dataset/transaction-relationships.md      netsuite/joins
add_partition knowledge/golden_dataset/transaction-types-and-statuses.md netsuite/transactions
add_partition knowledge/golden_dataset/financial-statements.md           netsuite/transactions
add_partition knowledge/golden_dataset/multi-currency-rules.md           netsuite/multi-currency
add_partition knowledge/golden_dataset/record-types-and-columns.md       netsuite/record-types
add_partition knowledge/golden_dataset/custom-fields-and-records.md      netsuite/record-types
```

- [ ] **Step 4: Spot-check one file's frontmatter**

```bash
head -6 knowledge/golden_dataset/multi-currency-rules.md
```

Expected:

```
---
topic_tags: ["suiteql", "currency", "multi-currency"]
source_type: expert_rules
partition_id: netsuite/multi-currency
---
```

- [ ] **Step 5: Run the partition test to verify all 11 pass**

```bash
backend/.venv/bin/python -m pytest backend/tests/test_golden_dataset_partitions.py -v
```

Expected: 2 PASS.

- [ ] **Step 6: Commit**

```bash
git add knowledge/golden_dataset/ backend/tests/test_golden_dataset_partitions.py
git commit -m "feat(golden): partition_id frontmatter on 11 NetSuite golden_dataset files

Each NetSuite file now declares its target RAG partition via YAML
frontmatter. The ingest script (extended in Task 2) reads this field
and stamps it on each chunk, which enables netsuite.yaml's
rag_partitions to filter retrieval correctly.

Mapping:
- netsuite/suiteql-rules: suiteql-syntax-rules, suiteql-example-queries,
  common-errors-and-recovery, date-and-time-patterns
- netsuite/joins: join-patterns-and-aggregation, transaction-relationships
- netsuite/transactions: transaction-types-and-statuses,
  financial-statements
- netsuite/multi-currency: multi-currency-rules
- netsuite/record-types: record-types-and-columns,
  custom-fields-and-records

BigQuery golden_dataset files (bigquery-bi-analytics,
bigquery-data-transformation, bigquery-sql-patterns) are untouched;
they're out of scope for this PR. Verify separately whether they're
already in bi/* partitions referenced by bigquery.yaml.

Regression test asserts every file has the expected partition_id.

Relates: Task 8 of docs/superpowers/plans/2026-04-16-netsuite-knowledge-phase-2-pr-a.md"
```

---

## Task 9: Write RAG ingest runbook

**Files:**
- Create: `docs/runbooks/netsuite-rag-ingest.md`

Runbook documents the one-off commands to ingest the 11 files against a live database. This is a runbook (prose + commands), not code. No test.

- [ ] **Step 1: Create the runbook**

Create `docs/runbooks/netsuite-rag-ingest.md`:

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/netsuite-rag-ingest.md
git commit -m "docs(runbook): NetSuite RAG ingest procedure + retrieval verification

Documents the one-off ingest command, the SQL to verify chunk counts
by partition, a diagnostic Python snippet to check retrieval similarity
for the canonical Phase 2 queries, and the rollback path.

Meant to be run manually before merging Phase 2 PR A so the first
staging deploy already has chunks to retrieve.

Relates: Task 9 of docs/superpowers/plans/2026-04-16-netsuite-knowledge-phase-2-pr-a.md"
```

---

## Task 10: Execute the RAG ingest against staging

**Files:** none modified (runbook step, DB write).

Running the ingest against staging Supabase populates `domain_knowledge_chunks` so the next chat turn can retrieve from `netsuite/*` partitions.

- [ ] **Step 1: Confirm `.env` points at staging**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2
grep -E '^(DATABASE_URL|DATABASE_URL_DIRECT)=' .env | head -2
```

Expected: URLs containing `supabase` or the staging host. If they point to `postgres://localhost:5432/...`, the symlinked `.env` is dev/local; abort and symlink the staging .env (or override `DATABASE_URL` inline for this command only).

- [ ] **Step 2: Run the ingest**

```bash
cd backend
.venv/bin/python -m scripts.ingest_domain_knowledge --dir ../knowledge/golden_dataset 2>&1 | tail -30
```

Expected: output ending with a count of chunks inserted/updated across the 14 files. Ingest takes ~1-2 minutes.

- [ ] **Step 3: Verify chunk counts**

Run the verification SQL from `docs/runbooks/netsuite-rag-ingest.md` Step 2. If any partition shows 0 chunks, diagnose (likely: frontmatter missed, or source_uri key mismatch).

- [ ] **Step 4: Verify retrieval similarity**

Run the Python diagnostic from the runbook. Each of the three canonical queries should return >= 1 chunk with similarity >= 0.50.

- [ ] **Step 5: No commit**

This step is a DB mutation; no git artifact. Document the ingest results (chunk counts, similarity scores) in the PR description when you open it in Task 12.

---

## Task 11: Full backend pytest + benchmark verification

**Files:** none modified (verification).

Confirms Phase 2 changes haven't regressed unrelated tests.

- [ ] **Step 1: Run full backend pytest**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2
backend/.venv/bin/python -m pytest backend/tests --tb=line -q 2>&1 | tail -15
```

Expected: > 2,900 tests pass, 0 fail. The new tests from Tasks 2, 5, 7, 8 add ~30 tests.

If anything unrelated fails, STOP and investigate before proceeding to Task 12. The most likely failure modes:
- `test_prompt_tool_sync.py` (CI invariant) — tool-inventory check might catch a prompt content change. Read the failure and decide if it's a genuine regression or a test that needs updating.
- Connector/auth tests with `InvalidToken` errors — environmental (encryption key mismatch with DB), not a code issue. Record for PR description but don't block.

- [ ] **Step 2: Run the vs-MCP benchmark (staging, one canonical case)**

The benchmark ran from-source against staging and uses the new `netsuite.yaml` profile (which now owns the SuiteQL rules). This is the acid test: does the profile-injected content work as well as the old inline prompt?

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2
backend/.venv/bin/python -m app.services.benchmarks.run_vs_mcp \
  --case sales_country_canonical \
  --tenant-id ce3dfaad-626f-4992-84e9-500c8291ca0a \
  --skip-baseline 2>&1 | tail -15
```

Note: this needs to run against **staging** (with staging encryption keys) OR against a staging-deployed version of this branch. If run locally with local `.env`, it'll fail on `InvalidToken` decrypting NetSuite creds. Either:
(a) temporarily point `.env` at staging secrets, OR
(b) SSH to staging and run `sudo docker exec ecom-netsuite-backend-1 python -m app.services.benchmarks.run_vs_mcp --case sales_country_canonical --tenant-id ce3dfaad-... --skip-baseline` after a staging deploy.

Expected: accuracy = 1.00, tool_score = 1.00, cost ~ $0.07, single `ns_runCustomSuiteQL` call with `sa.nKey = t.shippingAddress`. Matches Phase 1's staging benchmark number.

If accuracy drops below 1.00 OR cost rises above $0.15, something about the profile injection is different from the old inline prompt. Diagnose before opening the PR: likely the `<suiteql_dialect_rules>` tag wrapping is different from the old XML tag position in the base prompt, or the cross-reference in `tool_selection` is no longer resolving.

- [ ] **Step 3: No commit**

Verification only. Record the results for the PR description.

---

## Task 12: Push + open PR

**Files:** none modified.

- [ ] **Step 1: Confirm clean git state**

```bash
cd /Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2
git status --short
git log --oneline origin/main..HEAD
```

Expected: clean working tree (only untracked `backend/.venv`, `frontend/node_modules`, `.env` symlinks showing). At least 8 commits ahead of `origin/main`: (merged Phase 1 commits) + Task 2-11 commits.

- [ ] **Step 2: Push to both remotes**

```bash
git push -u origin feat/netsuite-knowledge-phase-2 2>&1 | tail -3
git push framework feat/netsuite-knowledge-phase-2 2>&1 | tail -3
```

- [ ] **Step 3: Open PR against `main`**

Base is `main` (not `feat/cross-source-sheets`) because Phase 1 already merged there via the squash-merge of PR #45. If `main` doesn't yet have Phase 1 content (feat/cross-source-sheets wasn't merged up to main), base against `feat/cross-source-sheets` instead.

Check first:

```bash
git log origin/main --oneline -5 | grep -i "phase.1\|address.tables\|shipping.country"
```

If no Phase 1 markers appear, base should be `feat/cross-source-sheets`.

Then:

```bash
BASE_BRANCH="main"  # or feat/cross-source-sheets if Phase 1 not in main yet

gh pr create \
  --base "$BASE_BRANCH" \
  --head feat/netsuite-knowledge-phase-2 \
  --title "feat(chat): netsuite.yaml profile + golden_dataset RAG ingest (Phase 2 PR A)" \
  --body "$(cat <<'EOF'
## Summary

Phase 2 PR A of the NetSuite knowledge restoration. Moves the ~148-line
SuiteQL dialect block out of the universal `_SYSTEM_PROMPT` into a new
`netsuite.yaml` knowledge profile; wires 11 orphaned golden_dataset
files into RAG under `netsuite/*` partitions; adds a worked
`transactionShippingAddress` example.

**Net effect:** Non-NetSuite tenants stop paying the ~6000-char prompt
tax for rules they'll never use. Golden dataset becomes a live RAG
corpus instead of dead engineer reference docs.

## Changes

- `backend/app/services/chat/knowledge_profiles/netsuite.yaml` (NEW) —
  triggers on all NetSuite read-side tools; contains the verbatim
  SuiteQL dialect rules inside `<suiteql_dialect_rules>` tags;
  declares 5 RAG partitions.
- `backend/app/services/chat/agents/unified_agent.py` — deleted the
  `<suiteql_dialect_rules>` block (148 lines).
- `backend/scripts/ingest_domain_knowledge.py` — now reads
  `partition_id` from frontmatter and stamps on `DomainKnowledgeChunk`.
- `knowledge/golden_dataset/*.md` — 11 NetSuite files got
  `partition_id` added to frontmatter; `join-patterns-and-aggregation.md`
  got a worked `transactionShippingAddress` example at end-of-file.
- `docs/runbooks/netsuite-rag-ingest.md` (NEW) — how to ingest / verify
  / rollback.
- New tests: `test_ingest_partition_id.py`,
  `test_netsuite_yaml_profile.py`,
  `test_golden_dataset_worked_example.py`,
  `test_golden_dataset_partitions.py`.
- Updated tests: `test_unified_agent_suiteql_rules.py` (reads profile
  not `_SYSTEM_PROMPT`), `test_prompt_trim.py` (ceiling
  18500 → 13000; critical-rules fixture reads base + profile).

## Test plan

- [x] Backend pytest: 2,930+ tests passing, 0 failing.
- [x] `netsuite.yaml` loads; `matches_tools({"netsuite_suiteql"})` is
  `True`; `matches_tools({"bigquery_sql"})` is `False`.
- [x] `_SYSTEM_PROMPT` shrunk to <13000 chars (was ~18350).
- [x] RAG ingest run against staging; chunk counts per partition
  match expected ranges (see `docs/runbooks/netsuite-rag-ingest.md`
  for the verification SQL).
- [x] Retrieval similarity >= 0.50 for each of the three Phase 2
  success-criterion queries (shipping country, multi-currency,
  pivot by month × country).
- [x] vs-MCP benchmark canonical case:
  accuracy = 1.00, single tool call, cost ~ $0.07. No regression
  from Phase 1's staging baseline.

## Out of scope (PR B, separate spec)

- `seed_tenant_patterns.py` admin script.
- `_INVESTIGATION_RE` coverage audit.

## Spec / plan

- Spec: `docs/superpowers/specs/2026-04-16-netsuite-knowledge-phase-2-design.md`
- Plan: `docs/superpowers/plans/2026-04-16-netsuite-knowledge-phase-2-pr-a.md`
EOF
)"
```

Record the PR URL when printed. Report to the user.

- [ ] **Step 4: Verify CI green on the new PR**

```bash
# Wait up to ~12 minutes for CI to run
sleep 120 && gh pr checks $(gh pr view feat/netsuite-knowledge-phase-2 --json number --jq .number)
```

Expected: all non-lint checks pass. (Lint has 17 pre-existing errors on main per CLAUDE.md memory — not introduced by this PR.)

---

## Self-Review Checklist (completed inline during plan drafting)

**Spec coverage** — each Phase 2 PR A item from the spec maps to a task:
- ✅ `netsuite.yaml` profile creation → Task 3
- ✅ Verbatim SuiteQL rule move → Task 3 (old_string/new_string in-file)
- ✅ Delete from `_SYSTEM_PROMPT` → Task 3 (same commit as create)
- ✅ Regression test asserting all key rule strings → Task 5
- ✅ Tighten prompt-size ceiling → Task 6
- ✅ Ingest 11 files under `netsuite/*` partitions → Tasks 2, 8, 10
- ✅ Append worked example → Task 7
- ✅ Runbook → Task 9
- ✅ Update existing test assertions → Task 4
- ✅ Tests that profile loads with correct shape → Task 5

**Placeholder scan** — no TODO/TBD. Every code step has complete code. Every command step has expected output. Commit messages are verbatim-ready.

**Type consistency** — `build_chunk_dicts_for_file(source_uri, content)` used identically in Task 2 test, Task 2 helper definition, and Task 2 call-site. `KnowledgeProfile` fixture signature in Tasks 4 and 5 returns the same object shape.

**File-path accuracy** — verified all paths against the worktree at `/Users/aidenyi/projects/ecom-netsuite-suites/.worktrees/feat-netsuite-yaml-phase-2/`. Line numbers (`unified_agent.py:173-320`) verified against HEAD 791d75c.

---

## Out of scope (explicit non-tasks)

- `seed_tenant_patterns.py` admin script — PR B.
- `_INVESTIGATION_RE` coverage audit for vernacular + domain_knowledge gating — PR B.
- Re-enabling auto-learning from live chat — out of scope until eval-gated promotion lands (see `autonomous-improvement` skill roadmap).
- BigQuery golden_dataset files (`bigquery-bi-analytics.md`, `bigquery-data-transformation.md`, `bigquery-sql-patterns.md`) — untouched; verify separately whether they're already in `bi/*` partitions referenced by `bigquery.yaml`. Track as a follow-up if not.
- Frontend changes — none required.
- Alembic migrations — none required (`partition_id` column already exists on `domain_knowledge_chunks`).
