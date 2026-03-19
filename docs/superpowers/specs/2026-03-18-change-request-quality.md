# Change Request Quality — Fast, Single Patch, Show Diff

**Date**: 2026-03-18
**Priority**: HIGH — feature works but UX is broken
**Depends on**: Workspace routing fix (PR #19, merged), Preamble chunking (PR #18, merged)

---

## Problem

The change request flow works end-to-end (chat → find script → propose patch → changeset in Dev Workspace) but the UX is poor:

1. **Duplicate changesets** — agent calls `workspace_propose_patch` twice for the same file, creating two identical draft changesets
2. **No diff shown in chat** — response is buried in `<reasoning>` tags, user never sees the actual code change in the chat response
3. **Slow (~60s)** — agent reads the full file twice, generates 400+ lines of reasoning analyzing code line-by-line, then proposes patch. Should be: search → read once → patch → show diff → done.

---

## Root Cause

### Duplicate patches
The agent proposes one patch, then second-guesses itself and proposes a "better" version. No instruction limits it to one `workspace_propose_patch` call per file.

### No diff in chat
The `<workspace_rules>` section says "ALWAYS show the code change in your response using a fenced code block" but it's buried at line 334 of the prompt. The agent generates massive `<reasoning>` blocks and by the time it reaches the response, it doesn't include the diff.

### Slow
The agent reads the file content via `workspace_read_file` (truncated at 6K chars), then reads it AGAIN to see more lines. It should read once, patch once, respond once.

---

## TODO

### Fix 1: Add "ONE patch per file" rule to prompt

In `unified_agent.py` `<workspace_rules>` section, add:

```
CHANGE REQUEST DISCIPLINE:
- Call workspace_propose_patch ONCE per file. Never propose two patches for the same file.
- Before calling workspace_propose_patch, you MUST have read the file via workspace_read_file.
- After calling workspace_propose_patch, show the unified diff in a ```diff fenced code block.
- Do NOT analyze the file line-by-line in <reasoning>. Read the file, identify the change, patch it.
- BUDGET: 3 tool calls max for a change request (search → read → patch).
```

### Fix 2: Move "show diff" instruction to output_instructions

The `<workspace_rules>` instruction is ignored because it's far from `<output_instructions>`. Add to `<output_instructions>`:

```
5. If you called `workspace_propose_patch`, show the unified diff in a ```diff code block
   and a one-sentence summary. The changeset is now in Dev Workspace for review.
```

### Fix 3: Prevent duplicate patch calls in code

In `base_agent.py`, track which files have been patched in the current turn:

```python
patched_files: set[str] = set()  # Track files patched this turn

for block in response.tool_use_blocks:
    if block.name == "workspace_propose_patch":
        file_path = block.input.get("file_path", "")
        if file_path in patched_files:
            # Skip duplicate patch for same file
            tool_results_content.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps({"skipped": "Already proposed a patch for this file."}),
            })
            continue
        patched_files.add(file_path)
```

### Fix 4: Test coverage

- `test_single_patch_per_file` — agent calls propose_patch twice for same file → second is skipped
- `test_diff_shown_in_response` — prompt contains diff display instruction in output_instructions
- `test_change_request_budget` — prompt mentions 3 tool call budget for changes

---

## Implementation Order

1. **Fix 1 + Fix 2** (prompt changes) — 10 min
2. **Fix 3** (code dedup) — 15 min
3. **Fix 4** (tests) — 10 min

---

## Success Criteria

Change request flow should be:
- **3 tool calls**: workspace_search → workspace_read_file → workspace_propose_patch
- **1 changeset** created (not 2)
- **Diff shown in chat** as a ```diff code block
- **< 20 seconds** total (not 60)
- Changeset visible in Dev Workspace with correct title + rationale
