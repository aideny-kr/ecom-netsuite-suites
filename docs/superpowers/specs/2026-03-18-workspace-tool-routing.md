# Workspace Tool Routing — Correct Workspace ID Resolution

**Date**: 2026-03-18
**Priority**: HIGH — agent can't create change requests when it picks the wrong workspace
**Depends on**: Workspace chunking fix (PR #18, merged)

---

## Problem

When the agent calls `workspace_search`, `workspace_read_file`, or `workspace_propose_patch`, it needs a `workspace_id`. Currently:

1. The agent **hallucates or uses a stale workspace ID** from conversation history — e.g., `757b3b01` which doesn't exist on staging
2. `_resolve_default_workspace` in `base_agent.py` only fires when workspace_id is empty or invalid UUID — it doesn't check if the workspace actually has files
3. Framework tenant has 2 workspaces: "SuiteScript" (0 files, empty) and "NetSuite Scripts" (311 files). The resolver may pick the empty one

**Impact**: The agent finds script content via `rag_search` (works) but can't `workspace_propose_patch` because it targets the wrong workspace. Falls back to describing the patch in text — no changeset created.

---

## Root Cause

`_resolve_default_workspace()` selects the most recently created active workspace:

```python
# base_agent.py line ~55
select(Workspace.id)
    .where(Workspace.tenant_id == tenant_id, Workspace.status == "active")
    .order_by(Workspace.created_at.desc())
    .limit(1)
```

This doesn't account for:
- Whether the workspace has any files
- Whether the workspace_id the LLM provided actually exists

---

## TODO

### Fix 1: Resolve to workspace with files (CRITICAL)

**File**: `backend/app/services/chat/agents/base_agent.py`

Update `_resolve_default_workspace()` to prefer the workspace with the most files:

```python
async def _resolve_default_workspace(db, tenant_id):
    result = await db.execute(
        select(Workspace.id, func.count(WorkspaceFile.id).label("file_count"))
        .outerjoin(WorkspaceFile, WorkspaceFile.workspace_id == Workspace.id)
        .where(Workspace.tenant_id == tenant_id, Workspace.status == "active")
        .group_by(Workspace.id)
        .order_by(func.count(WorkspaceFile.id).desc())
        .limit(1)
    )
    row = result.first()
    return str(row[0]) if row else None
```

**Tests (TDD)**:
- `test_resolves_to_workspace_with_files` — two workspaces, one empty, one with files → picks the one with files
- `test_resolves_when_only_one_workspace` — single workspace → returns it
- `test_returns_none_when_no_workspaces` — no workspaces → returns None
- `test_ignores_inactive_workspaces` — inactive workspace with files → skipped

### Fix 2: Validate LLM-provided workspace_id exists

**File**: `backend/app/services/chat/agents/base_agent.py`

Before accepting the LLM's workspace_id, verify it exists and belongs to the tenant:

```python
# In the tool call loop, after UUID validation
if block.name.startswith("workspace_"):
    ws_id = block.input.get("workspace_id", "")
    if not ws_id or not _is_valid_uuid(ws_id):
        resolved = await _resolve_default_workspace(db, self.tenant_id)
        if resolved:
            block.input["workspace_id"] = resolved
    else:
        # NEW: verify workspace exists for this tenant
        exists = await db.execute(
            select(Workspace.id).where(
                Workspace.id == ws_id,
                Workspace.tenant_id == self.tenant_id
            )
        )
        if not exists.scalar_one_or_none():
            resolved = await _resolve_default_workspace(db, self.tenant_id)
            if resolved:
                block.input["workspace_id"] = resolved
```

**Tests (TDD)**:
- `test_invalid_workspace_id_gets_resolved` — LLM provides UUID that doesn't exist → resolved to default
- `test_valid_workspace_id_preserved` — LLM provides correct UUID → kept as-is
- `test_wrong_tenant_workspace_rejected` — workspace exists but wrong tenant → resolved to default

### Fix 3: Log workspace resolution for debugging

Add print(flush=True) when workspace_id is resolved or corrected, so we can see it in docker logs.

---

## Implementation Order

1. **Fix 1** (resolve to workspace with files) — 15 min
2. **Fix 2** (validate LLM workspace_id) — 15 min
3. **Fix 3** (logging) — 5 min
4. **Tests** — write first (TDD), 15 min

---

## Success Criteria

After all fixes, the change request flow should work end-to-end:
1. User: "is there a script that converts FRANCR000B?"
2. Agent: `workspace_search` → finds `Framework_SalesOrder_UE.js#preamble` → shows conversion rule
3. User: "remove the conversion, we no longer need it"
4. Agent: `workspace_read_file` → reads full file → `workspace_propose_patch` → creates changeset
5. Changeset visible in Dev Workspace UI for review/approval
