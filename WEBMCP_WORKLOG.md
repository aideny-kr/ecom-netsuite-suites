# WebMCP development and testing goal

## Objective

Make Suite Studio usable by agents for reliable development and testing, starting
with chat. Agents should be able to understand page state, perform supported
actions, follow asynchronous work, inspect structured results, and verify outcomes.

## Isolation and ownership

- Worktree: `/Users/aidenyi/projects/ecom-netsuite-suites-webmcp`
- Branch: `codex/webmcp-agent-dev-testing`
- Base: `5d340bdb98c72fd2b68dd2212806e3a660c2f885`
- Implementer: GPT-6 Astra; one lead agent. No supporting agents requested.
- All subsequent edits, task commits, dependencies, and build outputs belong here.
- No merge, push, deployment, or changes to other worktrees are part of this goal.
- Preserve backend APIs, application model choices, tenant boundaries, financial
  confirmation, audit controls, idempotency, and working services.
- Use existing application handlers. Do not create alternative submission paths
  that bypass UI validation, state refresh, or backend authorization.
- Use a separate preview port when needed. Do not replace the existing dev server
  or alter shared Docker services/databases for this task.
- The root AGENTS.md was copied for local guidance; it belongs to the user's
  instruction work, so exclude it from feature commits. User-provided current
  preferences override older workflow prose in this worktree's base revision.

## Baseline transferred (2026-09-15)

Eight WebMCP foundation files were copied from the main checkout and verified by
SHA-256 before removing only this task's source changes from main. The main
frontend has no remaining WebMCP diff. Other agents' changes remain untouched.

Transfer backup and manifest: `/tmp/webmcp-worktree-transfer-20260915`.
Global Codex MCP server `chrome-webmcp` remains configured with Chrome DevTools MCP
1.9.0 and native WebMCP enabled. It uses a dedicated browser profile.

Foundation already verified before transfer:

- Three tools: page context, allowed navigation, stored connection health.
- 42 targeted WebMCP/auth/API-client tests and 3 native Chrome browser tests passed.
- Production build and TypeScript passed; lint passed with existing warnings.
- Direct MCP-to-Chrome-to-Suite-Studio discovery, reads, navigation, and route
  verification passed using an isolated browser and mocked API responses.
- Browser tests are fixtures, not proof of live backend integration.

These are prior verified results for the byte-identical transferred foundation,
not a claim that the expanded feature or this worktree has already been tested.

## Planned increments

1. **Foundation isolation — complete.** Dedicated branch/worktree, verified
   transfer, goal and checkpoint.
2. **Chat first.** Inspect existing conversation/run lifecycle and handlers; add
   context, explicit conversation creation, duplicate-safe message submission,
   run status, bounded structured output inspection, and cancellation where
   supported. Pending financial confirmation is inspectable, never auto-approved.
3. **Shared state and diagnostics.** Build/environment, selection, readiness,
   allowed actions, bounded failure details, and explicit operation completion.
4. **Workspace and tables.** File/search/editor inspection and table
   search/filter/sort/pagination using existing state and handlers. Preview changes
   through existing draft flows before considering any saved-file mutations.
5. **Reliable verification.** Deterministic chat responses, native Chrome tests,
   role/tenant/session/cancellation/duplicate-request coverage, and a dedicated
   fixture scenario against an isolated real backend. No real financial writes or
   unbounded model spend. Keep UI/keyboard/visual checks alongside direct tool calls.
6. **Delivery.** Update usage/scenario docs, record actual validation and remaining
   gaps. Preserve required independent review on the final revision before any
   integration/release; self-review does not substitute for that gate.

## Acceptance checks

- Tools discoverable only in their intended authenticated page states.
- Context and outputs reflect the current route, tenant, selection, and run.
- Retry cannot submit a second message for the same logical request.
- Tool submission and ordinary UI submission use the same production behavior.
- Completion, cancellation, awaiting confirmation, and failure are distinct.
- Tables/charts/files survive structured output inspection without inventing data.
- Logout, session changes, tenant switching, and stale callbacks fail safely.
- Browser/UI remains functional when WebMCP is unavailable.
- No credentials, unrestricted URLs, arbitrary API execution, or approval bypass.
- Focused tests, frontend lint/typecheck/build, and applicable backend tests pass;
  prior unrelated failures are recorded accurately.

## Next action

Map the general chat page and existing run APIs before choosing tool contracts:
`frontend/src/app/(dashboard)/chat/page.tsx`, `frontend/src/hooks/use-runs.ts`,
`frontend/src/hooks/use-workspace-chat.ts`, `frontend/src/lib/chat-stream.ts`,
`backend/app/api/v1/chat.py`, and `backend/app/api/v1/chat_runs.py`.
Use the already-read Suite Studio architecture skill and chat rules. Reuse the
unified-agent pipeline and current knowledge profiles; do not add model routing.

## Local implementation complete (2026-09-15)

The increments above are implemented for the shared app, general Chat, canonical
Tables, and the Files workspace. The onboarding/workspace chat variants, saved-file
mutations, external deployment actions, and model/provider sandbox testing are
explicit future extensions, not silently exposed actions.

### Delivered

- 21 native tools across page scopes, with shared identity validation, bounded
  structured previews, actionable route-tool errors, and lifecycle cleanup.
- General chat tools reuse existing creation/selection/composer/stop handlers.
  Submission returns admission promptly while the ordinary SSE UI continues.
- Optional backend request IDs use a durable receipt table and session admission
  lock. Concurrent retries produce one message/worker; changed input conflicts.
- Run inspection, stream access and cancellation enforce tenant AND user ownership.
  Cancelling blocks another turn until the worker exits. Terminal outcome distinguishes
  success, failure, cancellation and pending human confirmation/clarification.
- Fixed terminal SSE replay: no indefinite Redis BLOCK 0, no truncation after the
  first drain page, and no held database connection for the stream lifetime.
- Table controls use current UI state and withhold stale placeholder rows.
- Files workspace tools inspect selected files, search and existing operation status.
- Migration `100_chat_submissions` was applied only to dedicated local fixtures.
  Existing API clients remain accepted; newer retry-aware clients refuse old servers
  that cannot honor their request IDs.
- Usage, limits, fast checks and release ordering are in `frontend/WEBMCP.md`.

### Final verification

| Check | Result |
| --- | --- |
| Frontend WebMCP/auth/API-client unit tests | 58 passed |
| Native Chrome 152, real WebMCP implementation | 9 passed |
| Backend focused regression runner | 125 passed |
| Production frontend build | Passed |
| TypeScript, no incremental cache | Passed |
| Frontend lint | Passed; existing image/hook warnings |
| Ruff changed Python files, including migration | Passed |
| Ruff formatting and git diff whitespace | Passed |

Backend coverage includes separate real DB connections/commits for simultaneous
retries, Redis loss, changed-input conflicts, authenticated HTTP submission/status/
cancel/stream, tenant/user isolation, non-bypass-role RLS read/write checks, cancelled
admission, late cleanup, long terminal replay, write-confirmation compatibility,
rate limits, and deterministic worker outcome classification. No provider calls.

Native browser tests use mocked APIs: they are separate evidence from real-backend
checks and do not prove end-to-end provider connectivity. Tests assert visible chat
results, table search/rows and the selected workspace file, plus native execution.

Evidence logs are under `/tmp/webmcp-worktree-{unit,browser,build,types,lint}.log`,
`/tmp/webmcp-backend-final.log`, and `/tmp/webmcp-migration.log`.
The reusable backend command is `backend/scripts/test_webmcp.sh`; frontend commands
are `npm run test:webmcp:unit` and `npm run test:webmcp`.

### Delivery and remaining gates

All changes remain uncommitted on `codex/webmcp-agent-dev-testing`, based on
`5d340bdb98c72fd2b68dd2212806e3a660c2f885`, in this dedicated worktree. No PR, push,
merge or deployment was made. AGENTS.md remains copied local guidance and must be
excluded from the feature commit. Global Codex Chrome MCP configuration is installed;
a new Codex session may be needed to load it.

Implementer: GPT-6 Astra. Independent reviewer: none; no reviewed revision exists.
The required T2 independent pre-merge review, complete CI, seeded UAT and approved
migration/deployment sequence remain integration/release gates. Local implementation
completion is not release approval, and these self-checks are not independent review.

Known limits: admission receipts prevent duplicates but do not recover a worker
lost after commit; Redis run metadata expires after 30 minutes. Old backend runs
lack ownership mappings and must drain before rollout. Creating an empty chat is
not idempotent. Rich outputs are bounded previews. Financial approval, file saves,
NetSuite push and deploy are deliberately retained in their existing reviewed flows.


### Final async guards and cleanup

Async chat actions read current committed UI state through a stable getter and
recheck route/auth/selection after capability lookup and after submission. A
late receipt cannot reopen the old conversation stream. Stream generations keep
old cleanup from clearing a new conversation, and pending submission ownership
prevents token-refresh failures from leaving the composer stuck. Two native race
scenarios and a pre-dispatch unit regression verify these boundaries.

This task's earlier main-checkout preview on port 3002 was stopped after confirming
its original PID and cwd. The dedicated fixture containers `webmcp-test-pg` and
`webmcp-test-redis` were stopped, retained for quick restart. The final worktree
preview on port 3004 was also stopped after verification. No other agent's service
was stopped. Source remains entirely in the dedicated worktree; no task commit,
PR, merge or deployment was created. Source-file hashes for this checkpoint are
in `/tmp/webmcp-final-files-20260915.json`.


## Follow-up: workspace chat and draft inspection (2026-09-15)

Continued in the same isolated worktree on user request. No backend code, model
selection, API contracts, shared services or database migrations changed in this
increment.

### Changes

- Expanded the catalog to 32 distinct tools. Workspace Files has 15 tools including
  shared tools; opening its Chat panel adds seven for 22 available tools.
- Workspace Chat reuses the existing composer, session, background SSE and stop
  handlers. Keyed sends return admission immediately and carry the same retry key
  through busy replay. Sends require explicit workspace ID and exact file context.
  File/selection changes during awaited work fail closed before dispatch.
- Fixed the workspace composer dropping uploaded-file IDs and silently truncating
  prompts to 4,000 characters. Both entry points now use one context-enrichment
  path; the composer reserves prefix space against the configured input limit.
- Added the existing Stop control to workspace Chat. Workspace/session changes
  invalidate pending submissions and detach streams. Version guards prevent late
  events or cleanup from changing the newly selected conversation.
- Added panel selection and read-only changeset listing/open/diff tools. Diff
  previews include baseline drift and stale/error metadata, line/file pagination
  and bounded output. Draft membership is checked against the current workspace;
  stale editor content is withheld while a diff is displayed. No apply/approve/
  push/deploy tool was added.
- Updated frontend/WEBMCP.md with the workspace development workflow and remaining
  improvements. The focused unit command now includes workspace chat and composer
  regressions.

### Verification

- 72 focused frontend unit/hook/composer tests passed.
- 9 native Chrome tests passed, with the workspace scenario expanded to exercise
  long chat prompts, receipts/retries, rejected context mismatch, panel cleanup,
  current-workspace draft opening and stale diff inspection through the real
  browser WebMCP registry and visible UI.
- Production build, TypeScript, lint and diff whitespace checks passed. Existing
  image/hook lint warnings remain.
- Previous 125 backend regressions remain the backend evidence; backend source
  was unchanged in this follow-up and those tests were not redundantly rerun.
- Logs: /tmp/webmcp-improvements-{unit,browser,build,types,lint}.log.
- Native browser APIs are fixtures; these results do not claim provider/model UAT.

### Delivery

Still uncommitted in codex/webmcp-agent-dev-testing. No PR, merge, deployment or
shared service change. AGENTS.md remains local guidance excluded from the feature.
Implementer GPT-6 Astra; independent reviewer none. Full CI, seeded UAT and the
required independent T2 pre-merge review remain integration/release gates.

Next useful increments: durable worker recovery and persistent terminal receipts;
onboarding chat and agent attachment upload; native-Chrome CI and provider-sandbox
UAT. Existing financial-write confirmation and deployment controls remain in force.

Task preview on port 3004 was stopped after checking its PID/cwd. Backend file
hashes match the preceding checkpoint. A screenshot confirms the existing draft
header/stale banner; Monaco was still loading at capture, so native checks assert
source via tool output and visible selection, not Monaco editor rendering. Final
source hashes are in /tmp/webmcp-workspace-improvements-20260915.json.

## Commit / draft PR preparation (2026-09-15)

User explicitly requested commit and PR. Full frontend validation found four
workspace surface tests missing the new layout auth dependency. Added the local
auth fixture; all 1,188 frontend tests across 103 files now pass. No runtime code
changed in this preparation. Full backend coverage run started against the same
disposable fixture services; completion/CI evidence will be recorded in the PR.

Fetched origin/main at 430d5751; it is 99 commits ahead of the tested worktree base.
The draft PR must reconcile current main and migration successors before release.
No integration review or release approval is claimed. AGENTS.md remains excluded.

### Current-main integration for PR #260

The pre-integration full backend suite passed: 7,262 tests, 6 skipped, 76.51%
coverage, in 541 seconds. Full frontend: 1,188 passed.

Merged origin/main at 430d5751 into the feature branch. Resolved table imports
and query-result fields while preserving the new Orders TransactionWorkspace,
tenant/filter remounting and error/retry UI. WebMCP remains on TableContent and
uses its actual visible columns; Orders exposes shared tools only for now.
Renamed the unpublished receipt migration to 108_chat_submissions and reparented
it onto current main's 102_schedule_retry_job to preserve one migration head.
The earlier 100_chat_submissions revision was used only in disposable local tests.

Integrated verification: 1,440 frontend tests passed across 138 files; 134 focused
backend + seeded-tenant lifecycle tests passed; all 9 native Chrome tests passed.
Production frontend build and TypeScript passed. Fresh migration from base, one
head at 108_chat_submissions, downgrade and re-upgrade passed on the task-only
fixture database. The fixture database was recreated after the pre-integration
full suite completed, avoiding the old unpublished migration stamp.

The newer layout test also needed its feature-query dependency stubbed; both
layout responsiveness tests now pass with the native-tool mount present. No
runtime authentication or feature checks were relaxed. PR #260 remains draft
for CI and independent release review; no deployment was requested/performed.

## Independent release review (2026-09-15)

Claude Opus 5 independently reviewed 7780a5cc against 430d5751 through all eight
risk angles. It requested FORCE RLS and flagged cancellation recovery; two minor
findings covered replay burst quota and ownership TTL. Implementer remains GPT-6
Astra. Evidence: /tmp/webmcp-release-review/result.json.

Fixes add FORCE RLS with the repo tenant helper and test the actual non-bypass
table-owner role; matching receipt replays bypass new-message burst quota; Redis
ownership TTL follows retained run data. Agents can select session_id:null to use
the existing New Chat behavior for independent work while a prior run stops.

Cancellation disposition: do not force-settle a possibly live worker after a
timer or second cancel. CAS protects the Redis pointer only; it does not fence
old worker message writes or external tool side effects. Reopening the same
session early can overlap workers. Keep truthful cancelling + 600-second live
worker timeout + the documented lost-worker/TTL bound. A fresh-conversation escape
is safe; retrying an uncertain operation under another session/key is not.
Bounded dead-worker recovery needs durable fencing and is explicitly deferred.
A focused independent follow-up must assess this rationale and the fixes.
