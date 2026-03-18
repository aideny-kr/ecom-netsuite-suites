# Workspace Script Chunking — Capture Full File Content

**Date**: 2026-03-18
**Priority**: HIGH — agent can't find business logic in scripts
**Root Cause**: Entry-point chunking drops all code before the first entry point

---

## Problem

The workspace RAG seeder (`workspace_rag_seeder.py`) splits SuiteScript files at entry-point function boundaries (beforeSubmit, map, reduce, etc.). Code that lives **before** the first entry point — constants, configuration objects, helper functions — is silently dropped.

Example: `Framework_SalesOrder_UE.js` (61K) has:
- Lines 1-100: Constants including `CR_ITEMS_TO_REPLACE = { 1759: 1763 }` (FRANCR000B → FRANCR000Z)
- Lines 100+: Entry points (beforeSubmit, afterSubmit, each)

The seeder creates 4 chunks (#beforeSubmit, #afterSubmit, #each, #reduce) but the constants section (lines 1-100) is **never chunked**. So `rag_search` for "FRANCR000B" returns nothing, even though the file contains it.

**Impact**: The agent can't find business-critical configuration in scripts — item replacement rules, custom field mappings, hardcoded IDs, etc. These are exactly the things users ask about.

---

## TODO

### Fix 1: Capture preamble code as a chunk (CRITICAL)

**File**: `backend/app/services/workspace_rag_seeder.py`

In `_chunk_by_entry_points()` (line 124), the first entry point's offset is used as the start of chunking. Everything before it is lost.

**Fix**: Add a "preamble" chunk for code before the first entry point:

```python
def _chunk_by_entry_points(content, filepath, script_type):
    entry_points = _find_entry_points(content)

    if not entry_points:
        chunk = _build_chunk_content(content, filepath, script_type, None)
        return [(filepath, filepath, chunk)]

    chunks = []

    # NEW: Capture preamble (constants, imports, config before first entry point)
    first_offset = entry_points[0][1]
    if first_offset > 50:  # Only if there's meaningful content before first entry point
        preamble = content[:first_offset].rstrip()
        chunk = _build_chunk_content(preamble, filepath, script_type, "preamble")
        chunks.append((f"{filepath}#preamble", f"{filepath}#preamble", chunk))

    # Existing entry point chunking...
    for i, (name, offset) in enumerate(entry_points):
        end = entry_points[i + 1][1] if i + 1 < len(entry_points) else len(content)
        section = content[offset:end].rstrip()
        chunk = _build_chunk_content(section, filepath, script_type, name)
        chunks.append((f"{filepath}#{name}", f"{filepath}#{name}", chunk))

    return chunks
```

**Test**: After re-seeding, `FRANCR000B` should appear in doc_chunks.

### Fix 2: Include helper functions between entry points

Large files often have helper functions scattered between entry points. The current chunking captures them as part of the next entry point's chunk, which is correct. But for very large files (>6K chars per section), the content gets truncated at `_MAX_CHUNK_CHARS`.

**Fix**: When a section exceeds `_MAX_CHUNK_CHARS`, split into sub-chunks at function boundaries instead of hard-truncating:

```python
if len(full) > _MAX_CHUNK_CHARS:
    # Split at function boundaries instead of hard truncate
    # Each sub-chunk gets the same header
    sub_chunks = _split_at_functions(code, _MAX_CHUNK_CHARS - len(header))
    return [(header + sc) for sc in sub_chunks]
```

### Fix 3: Force re-seed after script sync

**File**: `backend/app/workers/tasks/sync_scripts.py`

After a successful script sync, trigger `seed_workspace_scripts(force=True)` to re-chunk all files. Currently the seeder only processes files with changed hashes, but if the chunking logic changes (like adding preamble), old files need re-processing.

**Fix**: Add `force=True` call at the end of the sync task, or add a management command:
```bash
docker exec ecom-netsuite-backend-1 python -c "
from app.services.workspace_rag_seeder import seed_workspace_scripts
# force=True re-processes all files regardless of hash
await seed_workspace_scripts(db, tenant_id, workspace_id, force=True)
"
```

### Fix 4: Test coverage

Add tests:
- `test_preamble_chunk_created`: File with constants before first entry point → preamble chunk exists
- `test_preamble_contains_constants`: The preamble chunk includes variable declarations
- `test_no_preamble_for_short_prefix`: Files where first entry point is near the top → no empty preamble chunk
- `test_large_section_split`: Entry point section > 6K chars → split into sub-chunks, not truncated

---

## Implementation Order

1. **Fix 1** (preamble chunk) — 15 min, highest impact
2. **Fix 4** (tests) — 15 min, validate fix
3. **Fix 3** (force re-seed) — 5 min, trigger after deploy
4. **Fix 2** (sub-chunk splitting) — 30 min, nice-to-have for very large files

---

## Success Criteria

After all fixes + re-seed:
- "is there a script that converts FRANCR000B?" → finds `Framework_SalesOrder_UE.js#preamble` chunk
- "what does CR_ITEMS_TO_REPLACE do?" → finds the constants
- "which scripts use hasPanurgyLocation?" → finds the helper function
- Both `rag_search` AND `workspace_search` find the content
