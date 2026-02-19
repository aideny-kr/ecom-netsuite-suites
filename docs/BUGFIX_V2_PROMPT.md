# Multi-Agent Bugfix — Code Review Findings (Round 2)

Findings from external code review. One false positive excluded; remaining issues confirmed.

---

## Agent 1: Frontend Engineer — UX Bugs

### Fix 1A (P2): Reset tab state when switching workspaces

**File:** `frontend/src/app/(dashboard)/workspace/page.tsx`

When the user picks a different workspace from the WorkspaceSelector, `openTabs`, `activeTabId`, `selectedFileId`, and `selectedFilePath` still hold values from the previous workspace. This causes stale content and confusing UI.

Find where `setSelectedWorkspaceId` is called (in WorkspaceSelector's `onSelect` and in `handleCreate`). Clear the editor state:

```tsx
const handleWorkspaceSwitch = useCallback((wsId: string | null) => {
  setSelectedWorkspaceId(wsId);
  // Reset editor state for the new workspace
  setOpenTabs([]);
  setActiveTabId(null);
  setSelectedFileId(null);
  setSelectedFilePath("");
  setViewingDiffId(null);
  setSearchQuery("");
}, []);
```

Then pass `handleWorkspaceSwitch` instead of `setSelectedWorkspaceId` to `<WorkspaceSelector onSelect={handleWorkspaceSwitch} />` and use it in `handleCreate` too.

### Fix 1B (P2): Truncate file-context prefix to prevent 422

**File:** `frontend/src/components/workspace/workspace-chat-panel.tsx`

The `handleSendWithContext` prepends `[Currently viewing file: <path>]` which can push the total content over the backend's 4000-char limit (defined in `backend/app/api/v1/chat.py` line 32: `max_length=4000`).

Add a safety truncation:

```tsx
const handleSendWithContext = useCallback(
  (content: string) => {
    let enrichedContent = content;
    if (currentFilePath) {
      const prefix = `[Currently viewing file: ${currentFilePath}]\n\n`;
      // Backend max is 4000 chars — leave room for prefix
      const maxContentLen = 4000 - prefix.length;
      if (content.length > maxContentLen) {
        enrichedContent = prefix + content.slice(0, maxContentLen);
      } else {
        enrichedContent = prefix + content;
      }
    }
    handleSend(enrichedContent);
  },
  [handleSend, currentFilePath],
);
```

### Fix 1C (P3): parseThinkingBlocks — handle multiple and non-leading blocks

**File:** `frontend/src/components/chat/message-list.tsx`

Replace the current `parseThinkingBlocks` with one that handles multiple and non-leading thinking blocks:

```tsx
function parseThinkingBlocks(content: string): Array<{
  type: "text" | "thinking";
  content: string;
}> {
  const parts: Array<{ type: "text" | "thinking"; content: string }> = [];
  const regex = /<thinking>([\s\S]*?)<\/thinking>/g;
  let lastIndex = 0;
  let match;

  while ((match = regex.exec(content)) !== null) {
    if (match.index > lastIndex) {
      const text = content.slice(lastIndex, match.index).trim();
      if (text) parts.push({ type: "text", content: text });
    }
    parts.push({ type: "thinking", content: match[1].trim() });
    lastIndex = regex.lastIndex;
  }

  if (lastIndex < content.length) {
    const text = content.slice(lastIndex).trim();
    if (text) parts.push({ type: "text", content: text });
  }

  return parts.length > 0 ? parts : [{ type: "text", content }];
}
```

Then update the rendering to iterate over the parts array:

```tsx
{message.role === "assistant" ? (() => {
  const parts = parseThinkingBlocks(message.content);
  return (
    <div>
      {parts.map((part, i) =>
        part.type === "thinking" ? (
          <ThinkingBlock key={i} content={part.content} />
        ) : (
          <div key={i} className="prose prose-sm dark:prose-invert max-w-none text-[14px] leading-relaxed">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {part.content}
            </ReactMarkdown>
          </div>
        )
      )}
    </div>
  );
})() : ( /* user message rendering */ )}
```

---

## Agent 2: Backend Engineer — auth_type Consistency

### Fix 2A (P1): Add validation to auth_type

**File:** `backend/app/schemas/connection.py`

Add enum validation to the `ConnectionUpdate` schema:

```python
from typing import Literal

class ConnectionUpdate(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=255)
    auth_type: Literal["oauth2", "oauth1_tba"] | None = None
```

### Fix 2B (P2): Include auth_type in list and create responses

**File:** `backend/app/api/v1/connections.py`

In `list_connections` (line ~28-39) and `create_connection` (line ~79-89), the `ConnectionResponse` construction is missing `auth_type`. Add it:

```python
# In list_connections:
ConnectionResponse(
    id=str(c.id),
    tenant_id=str(c.tenant_id),
    provider=c.provider,
    label=c.label,
    status=c.status,
    auth_type=c.auth_type,  # ADD THIS
    encryption_key_version=c.encryption_key_version,
    metadata_json=c.metadata_json,
    created_at=c.created_at,
    created_by=str(c.created_by) if c.created_by else None,
)

# Same fix in create_connection response
```

### Fix 2C (P2): Sanitize file paths in system prompt

**File:** `backend/app/services/chat/orchestrator.py`

File paths from the workspace are user-controlled data being injected into the system prompt. Add minimal sanitization to prevent prompt manipulation via crafted file names:

In the workspace context injection section, sanitize the file listing:

```python
import re

def _sanitize_for_prompt(text: str) -> str:
    """Strip control characters and limit length for prompt injection safety."""
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', text)
    return cleaned[:500]

# When building file_listing:
file_listing = "\n".join(
    f"- {_sanitize_for_prompt(p)}" for p in file_paths[:50]
)
```

---

## Agent 3: QA Engineer — Verification

### Task 3A: Verify workspace switch resets tabs
Trace `handleWorkspaceSwitch` to confirm all editor state is cleared:
- `openTabs` → `[]`
- `activeTabId` → `null`
- `selectedFileId` → `null`
- `selectedFilePath` → `""`
- `viewingDiffId` → `null`

### Task 3B: Verify auth_type Literal validation
Test that `PATCH /connections/{id}` with `auth_type: "garbage"` returns 422 not 500.

### Task 3C: TypeScript compilation
```bash
cd frontend && npx tsc --noEmit
```

### Task 3D: Python syntax check
```bash
cd backend && python -c "
import ast, pathlib
errors = []
for f in pathlib.Path('app').rglob('*.py'):
    try: ast.parse(f.read_text())
    except SyntaxError as e: errors.append(f'{f}: {e}')
print(f'{len(errors)} errors')
for e in errors: print(e)
"
```

---

## Agent 4: Project Lead — Summary

### ChatGPT Review Triage:

| Finding | Severity | Verdict | Action |
|---------|----------|---------|--------|
| Pull/push ID contract wrong | P1 | **FALSE POSITIVE** — code correctly maps workspace UUID → netsuite_file_id | None |
| auth_type not source of truth | P1 | Valid but low impact — cosmetic field for now | Defer |
| auth_type no validation | P1 | Valid | Fix 2A |
| auth_type omitted in responses | P2 | Valid | Fix 2B |
| Tab state not reset on switch | P2 | Valid — real UX bug | Fix 1A |
| Message length overflow | P2 | Valid — edge case | Fix 1B |
| Prompt injection surface | P2 | Low risk but worth hardening | Fix 2C |
| inputRef not attached | P3 | Valid but low impact (paperclip feature) | Defer |
| Single thinking block only | P3 | Valid | Fix 1C |

Summarize what was fixed and what was deferred. List manual test steps.
