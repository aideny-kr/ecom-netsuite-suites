# Suite Studio WebMCP

Native WebMCP lets a browser agent use the same visible application state and
handlers as a person. This implementation exposes 32 tools across the authenticated
app, general Chat, canonical Tables, and the Files workspace with workspace Chat. Browsers without
`document.modelContext.registerTool` continue to use the ordinary UI. No frontend
runtime dependency or application model change is required.

## Available tools

Every name below has the `suitestudio_` prefix. Discover again after navigation:
there are 3 shared tools, 10 total on Chat, 6 on a supported table, and 15 on the Files workspace
(22 while its Chat panel is open).

| Surface | Tools | Behavior |
| --- | --- | --- |
| Shared | `get_page_context`, `navigate`, `get_connection_status` | Current route/origin, active tenant, available tools, allowed destinations, and stored connection health. Health does not prove live provider connectivity. |
| General Chat | `chat_get_state`, `chat_create_session`, `chat_select_session` | Inspect readiness and conversations; create or select through existing UI handlers. Creating an empty session is not idempotent. |
| General Chat | `chat_send_message`, `chat_get_run`, `chat_get_messages`, `chat_cancel_run` | Retry-safe admission, lifecycle/outcome, paginated persisted structured output, and graceful cancellation. Submission may incur model usage under existing permissions. |
| Tables (excluding the new Orders workspace) | `table_get_state`, `table_set_query`, `table_open_row` | Visible search/filter, sorting, pagination and row drawer. Prior-page placeholder rows are withheld while fetching. |
| Files workspace | `workspace_get_state`, `workspace_select`, `workspace_list_files`, `workspace_open_file`, `workspace_read_editor` | Select through existing handlers, inspect a paginated tree and bounded lines of the loaded editor file. |
| Files workspace | `workspace_search`, `workspace_get_search`, `workspace_get_runs` | Visible search, snippets, and validation/test/deploy status. These tools do not start deployments or save files. |

| Workspace panels | `workspace_set_panel` | Open Chat, Changesets or Runs using the visible panel selector. Rediscover tools after opening Chat. |
| Workspace drafts | `workspace_list_changesets`, `workspace_open_changeset`, `workspace_read_diff` | Review bounded before/after/unified diffs and baseline drift. No approval, apply or deployment operation. |
| Workspace Chat | `workspace_chat_get_state`, `workspace_chat_create_session`, `workspace_chat_select_session`, `workspace_chat_send_message`, `workspace_chat_get_run`, `workspace_chat_get_messages`, `workspace_chat_cancel_run` | Same chat lifecycle, scoped to the selected workspace and explicit file context. Registrations are removed when the panel closes or workspace changes. |

The newer Orders/Transactions workspace retains its own UI and currently exposes
only shared WebMCP tools. Canonical table tools remain on Payments, Refunds,
Payouts and the other existing table views.

Route tools return bounded JSON diagnostics under `error` when an operation fails.
Check for that field before treating a result as success. Shared foundation tools
can still throw; Chrome may normalize their exceptions to `UnknownError`.

### Agent chat workflow

1. Navigate to `/chat`; rediscover tools and read `chat_get_state`.
2. Select an existing session, or call `chat_create_session` once. Read state until
   the expected `session_id` is selected and ready.
3. Generate a UUID for the logical message and keep it with its `session_id` and
   exact content. Call `chat_send_message` with those three arguments:

   ```json
   {
     "session_id": "10000000-0000-4000-8000-000000000001",
     "request_id": "10000000-0000-4000-8000-000000000003",
     "content": "Inspect the fixture orders and summarize the result."
   }
   ```

4. Save the returned `run_id`. `status: accepted` means admission, not completion.
   For a lost response, retry the **same session, request ID and content**. Changed
   input with an existing key receives HTTP 409. A concurrent different message
   receives HTTP 409 without persisting another user message.
5. Poll `chat_get_run` about once per second with a bounded deadline. `running` and
   `cancelling` are nonterminal. `complete`, `failed` and `cancelled` are terminal.
   Outcomes `awaiting_confirmation` and `awaiting_clarification` require the
   existing UI. No tool exposes write approval or confirmation credentials.
6. Read `chat_get_messages` using `offset`/`limit`. Structured tables, charts and
   file references stay structured; `truncated` marks an incomplete preview.
7. To stop, call `chat_cancel_run` with the selected session and active run. Wait
   for terminal status. Cancellation does not undo already executed tools.
   If stopping takes too long, call `chat_select_session` with `session_id: null`
   (or `workspace_chat_select_session` in workspace Chat), then create a session
   for **new independent work**. This opens a fresh composer without claiming the
   old worker stopped. Do not repeat an uncertain operation in the new session.

A missing/expired run is **not** proof of success. Inspect persisted messages;
never invent a fresh request ID to bypass an uncertain receipt. Conversation
creation, selection and query changes return requests; verify current state after
React has applied them. For table search/sort/page-size changes, start at page 1,
then paginate after fresh results arrive.


### Workspace development workflow

1. Navigate to `/workspace`, select a workspace from `workspace_get_state`, then
   wait for `files_ready`. List/open files and inspect source or search results.
2. Call `workspace_set_panel` with `panel: "chat"`; rediscover the seven
   `workspace_chat_*` tools. Create/select a workspace conversation and wait for
   `workspace_chat_get_state.ready`.
3. Use the chat workflow above with the `workspace_chat_` names. In addition to
   `session_id`, `request_id` and `content`, each send requires `workspace_id` and
   the exact `context_file` from state (including JSON `null` when no file is
   selected). Retain **all five arguments** for retries. The adapter rejects a
   changed workspace/file context before dispatch, including changes during the
   capability check. Restore the original context before retrying an uncertain
   request; do not create a new key to get around this guard.
4. File context uses the same prefix as the composer. Messages are no longer
   silently cut at 4,000 characters; the configured limit includes that prefix.
   The UI reserves space for it, preserves oversized text, forwards uploaded-file
   IDs, and offers Stop for an active run. Agent attachment upload is not exposed.
5. Read proposed changes with `workspace_list_changesets`; open one with
   `workspace_open_changeset`, wait for `diff_ready`, then use
   `workspace_read_diff` with `side: "before"`, `"after"` or `"unified"` and optional
   `file_index`/line pagination. Inspect `diff_status`, `baseline_drift` and
   `truncated` before interpreting the preview. A stale diff is not proof a patch
   can apply. Editor reads are withheld while the diff viewer is active.
6. Inspect validation/test results with `workspace_get_runs`. Applying changes,
   approving financial writes, NetSuite push and deployment retain existing UI
   review and authorization controls.

Switching conversations detaches the old stream without cancelling server work.
Late receipts, text and cleanup cannot reopen or overwrite the new conversation.
Close/reopen the Chat panel to rediscover sessions and reconnect to active work.

## Backend compatibility and safety

- Migration `108_chat_submissions` adds a tenant-scoped receipt table with row-level
  security, including FORCE for the non-bypass table owner. Receipt and user
  message commit together under a session row lock. Matching retries return the
  existing receipt without consuming new-message burst quota.
  Retries remain deduplicated after HTTP loss, browser reload and Redis expiry.
- `POST /api/v1/chat/sessions/{id}/messages` accepts optional UUID `request_id`.
  Existing callers remain valid. Retry-aware clients require background execution;
  they receive 503 when unavailable. Existing unkeyed callers retain inline SSE.
- `GET /api/v1/chat/health` advertises retry support and the configured input limit.
  WebMCP refuses to send to an older backend that would ignore its retry key.
- `GET /api/v1/chat/runs/{id}` exposes lifecycle/outcome. Status, streaming and
  cancellation verify both the session's tenant and owner. Unknown or foreign
  runs return 404. Cancellation is atomic and remains `cancelling` until settled.
- Terminal SSE replay drains all remaining event pages without indefinite Redis
  blocking, and releases its database connection before streaming.
- Existing auth, feature checks, burst limits, model selection and financial
  confirmation remain authoritative. Admissions and cancellation are audited.
- Tools revalidate backend identity, discard stale results after token/tenant
  changes, and unregister on logout or route unmount. Query parameters,
  credentials and arbitrary API URLs are not accepted as tool arguments.
- Structured previews omit credential-named fields. Source text, user messages,
  labels, snippets and tool outputs are untrusted content; previews are not a
  general-purpose secret scanner or complete data export.

### Limits and release sequence

This is **at-most-once admission**, not a durable background job queue. A process
crash after receipt commit but before starting its worker can leave an admitted
run without an answer. Retries preserve the receipt and do not launch duplicate
model work. Cancellation stays nonterminal while a worker may still be executing;
repeated cancellation never forcibly frees that session. Live background tasks
have a 600-second timeout; a dead worker can leave its session blocked until the
Redis lease expires. New-conversation navigation is the safe escape for independent
work. Force-settling would permit overlapping workers and uncertain external writes,
so durable recovery/fencing remains a separate improvement. Redis run/event metadata has a 30-minute TTL; message history and
admission receipts persist with the conversation. Ownership TTL is refreshed
with status, outcome, event and cancellation writes so retained data stays readable.

Before integration/release, apply the additive migration in the target's approved
migration workflow and deploy the compatible backend before the frontend. Runs
created by the old backend lack the new owner mapping: drain those active runs
before rollout; unknown legacy/expired mappings fail closed with 404. Do not apply
this feature-branch migration to a shared staging database before integration.

This worktree is not merged or deployed. Required full CI, T2 seeded-tenant UAT and
independent pre-merge review remain release gates under
[CLAUDE.md](../CLAUDE.md) and [uat-review.md](../.claude/rules/uat-review.md).
Implementer: GPT-6 Astra. No independent reviewer has reviewed this revision;
local self-checks do not substitute for that gate.

## Codex and Chrome setup

Installed and verified with Chrome 152.0.7977.83 and Chrome DevTools MCP 1.9.0:

```sh
codex mcp add chrome-webmcp -- npx --yes chrome-devtools-mcp@1.9.0 \
  --categoryExperimentalWebmcp \
  --chrome-arg=--enable-features=WebMCP \
  --no-usage-statistics --no-performance-crux
```

The server uses a dedicated Chrome profile. Sign in through Suite Studio's normal
UI in that profile; regular Chrome cookies are not copied. Start a new Codex
session if the newly installed server is not yet in the current tool inventory.

Open the app using the bridge's `new_page`. Call `list_webmcp_tools` with its
`pageId`, then `execute_webmcp_tool` with `pageId`, `toolName` and a JSON string in
`input`. For example, `suitestudio_get_page_context` takes `input: "{}"`.
The installed version's flag is **`--categoryExperimentalWebmcp`**.

## Fast local verification

From this dedicated worktree, using installed frontend dependencies:

```sh
cd frontend
npm run test:webmcp:unit
npm run lint
npx tsc --noEmit --incremental false
npm run build
npm run start -- --port 3004
# Another terminal in frontend:
npm run test:webmcp
```

`BASE_URL` overrides `http://localhost:3004`. Native tests use installed Chrome
with WebMCP enabled in a temporary test profile. API responses are deterministic
fixtures, so browser tests make no provider calls or business writes. They verify
visible UI results as well as direct tool execution.

For real-backend checks, use **dedicated disposable services**, not shared Docker
Compose services. Initial setup:

```sh
docker run -d --name webmcp-test-pg --label suite-studio.task=webmcp \
  -e POSTGRES_USER=webmcp -e POSTGRES_PASSWORD=webmcp-fixture -e POSTGRES_DB=webmcp \
  -p 127.0.0.1:15435:5432 pgvector/pgvector:pg16
docker run -d --name webmcp-test-redis --label suite-studio.task=webmcp \
  -p 127.0.0.1:16381:6379 redis:7-alpine
# From backend, with backend dev dependencies installed:
WEBMCP_PYTHON=.venv/bin/python bash scripts/test_webmcp.sh
```

If the task containers already exist but are stopped, run `docker start
webmcp-test-pg webmcp-test-redis` instead of creating them again.

The runner explicitly overrides database/direct-database/Redis settings, migrates
only the dedicated fixture database, then runs the focused regressions. Concurrency
checks use separate real commits and connections. HTTP coverage exercises real
authentication and feature checks. Deterministic generators replace provider/model
execution. RLS is tested with a role that cannot bypass it. These tests also run
in the existing ephemeral CI database; they skip on ordinary development or remote
databases. To remove only these disposable containers: `docker rm -f webmcp-test-pg
webmcp-test-redis`.

## Verification recorded (2026-09-15)

- Native Chrome: 9 tests passed, covering chat submit/retry/output/cancel, table
  search and visible results, workspace editor/diff inspection and context-bound
  workspace chat (including long prompts, retries and panel cleanup), navigation/logout,
  permission denial, fallback without native WebMCP, and navigation/selection races.
- Frontend: 1,440 full-suite tests passed after integration with main, including
  the 72 focused WebMCP/auth/hook/composer tests; production build, TypeScript and
  lint passed. Existing image/hook lint warnings remain.
- Backend: 134 focused PostgreSQL/Redis and seeded-tenant lifecycle tests passed
  after integration with main. The pre-integration full suite passed 7,262 tests
  with six skips and 76.51% coverage; full integrated CI remains a release gate.
- Migration 108 has a single head after 102_schedule_retry_job; fresh upgrade,
  downgrade and re-upgrade passed on the dedicated local fixture database.
- The saved Codex server configuration initialized with both bridge tools. An
  earlier direct MCP smoke test discovered/executed the shared foundation tools.
  Expanded tools were verified through native Chrome, not a second MCP smoke.
- Draft [PR #260](https://github.com/aideny-kr/ecom-netsuite-suites/pull/260)
  contains the changes. No shared migration, provider/model call, financial write
  or deployment was performed. Other agents' services and source changes were preserved.

## Useful next improvements

1. Move accepted work to a durable queue/outbox and persist terminal run receipts,
   so process restarts and Redis expiry can be recovered automatically.
2. Extend the same adapter to onboarding chat, attachment upload and
   clarification selection. Keep financial approvals
   and NetSuite push/deploy in their existing reviewed flows.
3. Add a dedicated native-Chrome CI lane and a provider-sandbox scenario after the
   required release review; current deterministic tests do not prove live NetSuite
   connectivity or model answer quality.
4. Add richer per-page filters only where the existing UI/API supports them, plus
   complete exports for previews too large for the bounded tool output.

## References

- [Chrome WebMCP](https://developer.chrome.com/docs/ai/webmcp)
- [Imperative API](https://developer.chrome.com/docs/ai/webmcp/imperative-api)
- [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp)
- [OpenAI site tools](https://learn.chatgpt.com/docs/webmcp)
