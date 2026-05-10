# In-app Validate UX Design

> Sub-project C of the Oracle SuiteCloud SDK integration (ClickUp 86b9pre59). Sub-task: 86b9u1xx7.

**Status:** approved 2026-05-09 (brainstorming complete, codex-reviewed)
**Owner:** AI-den team
**Predecessor:** PR #74 (vendored Oracle skills) + PR #75 (RAG seed) + PR #76 (Docker skills bake-in)
**Successor:** implementation plan via `superpowers:writing-plans`

## Goal

Surface Oracle SuiteCloud `project:validate --server` policy hits in the workspace agent so deploy-blocking errors land *before* deploys, with Oracle policy guidance threaded inline via the 7 `oracle/*` RAG partitions seeded in PR #75. Replace today's `sdf validate` (Apache, shallow) with Oracle's modern CLI run in **server mode** so account-state issues (custom record existence, permissions, dependencies) are caught — not just code-shape errors.

End state: when the agent applies a patch, validate runs automatically against the live NetSuite account; hits surface as a structured table in the runs panel and as a chat narration grouped by error family with Oracle citations; mechanically fixable hits get a draft fix patch the user can one-click apply.

## Decisions

| ID | Question | Decision | Rationale |
|---|---|---|---|
| Q1 | Validate scope | Multi-layer: `suitecloud project:validate --server` + Oracle policy guidance from RAG | Local validate (codex #5) is theater. Server mode catches the account-state issues users actually hit. |
| Q2 | Where do hits surface? | Runs panel (expandable hits table) + chat thread (agent narrates with Oracle citations). No inline editor markers. | Code viewer is read-only display today; Monaco-style decorations need separate editor infra. |
| Q3 | When does validate fire? | Auto-after `workspace_apply_patch` success (debounced). Auto-on `workspace_deploy_sandbox` request (cached by snapshot hash). Manual retry button kept for failed/stale recovery. | Codex #15: removing manual retry leaves no recovery path on transient auth/network failures. |
| Q4 | Block vs warn | Errors block deploy. Warnings advisory (chat surfaces, no gate). | Match Oracle's convention; predictable. |
| Q5 | Agent role with hits | Narrate every hit with Oracle citation. For mechanically fixable hits matching an explicit allowlist of Oracle rule IDs, auto-call `workspace_propose_patch` with a draft fix. Judgment calls (OWASP severity, architectural) narrate only. | Existing `workspace_propose_patch` → review-card flow is the natural chain point for the C-scope payoff. Deny-by-default classifier (codex #10) prevents wrong auto-fixes. |
| Q6 | RAG partitions | All 7 `oracle/*` partitions (ai-connector, owasp, sdf-docs, sdf-roles, records, upgrade, uif-spa) | Codex #9 corrected the original "narrow to 3" answer: SDF schema errors need `sdf-docs`; missing-field hits need `records`; permission errors need `sdf-roles`. |
| Q7 | Validate mode | Server mode only (`--server`). No silent fallback to local or `sdf` legacy. | Local-only is theater (codex #5). Silent fallback hides degradation (codex #7). |

## Architecture

The workspace agent gains a **policy-aware validate loop** with three triggers, structured findings storage, and snapshot-hash-keyed freshness:

1. **Auto-after-apply trigger.** `workspace_apply_patch` success → orchestrator debounces 2s per workspace (cancels superseded queued runs) → `suitecloud project:validate --server` runs in the runner subprocess → parser emits `ValidationHit` records → SSE update to runs panel → agent reads findings, batches by error family, narrates one citation per family from RAG (capped), and for hits matching the mechanically-fixable allowlist calls `workspace_propose_patch` with a draft fix.

2. **Auto-on-deploy trigger.** `workspace_deploy_sandbox` request → `deploy_service` looks up latest validation by snapshot hash (workspace + changeset + CLI version + validator engine + account ID) → if fresh+clean within 5min: deploy proceeds → if errors: deploy blocked with chat narration → if stale: enqueues fresh validate first, then re-evaluates.

3. **Manual retry trigger.** Runs panel shows a "Retry validate" button only when the latest validate run is `failed` or `stale`. Hidden during normal flow.

Loop budget (max 3 auto-fix rounds per changeset) + finding fingerprinting (`hash(file + line + code)`) prevent the agent from cycling on the same hit. Status semantics on each run record `has_errors`, `has_warnings`, and `gate_status` independently — never derived from exit code alone (codex #11).

## Backend changes

### New files

| Path | Purpose |
|---|---|
| `backend/app/services/workspace/validate_parser.py` | Best-effort parser for `suitecloud project:validate --server` output. Returns `(list[ValidationHit], raw_output, parser_version)`. On malformed output, falls back to one synthetic `parser_error` hit + raw text. Stamps `parser_version` for forensics. |
| `backend/app/services/workspace/auto_validate_orchestrator.py` | Per-workspace debounce queue (cancels superseded runs). Per-changeset loop budget enforcement. Finding-fingerprint dedup so the same hit is never auto-proposed twice. |
| `backend/app/services/workspace/mechanical_fix_classifier.py` | Deny-by-default rule-ID allowlist mapping Oracle codes → deterministic patch generators. Unknown rule → narrate-only. |
| `backend/app/services/workspace/suitecloud_auth_seeder.py` | Per-tenant credential write. Reads decrypted credentials from `connections` table, writes the format `suitecloud` CLI expects (`~/.suitecloud-sdk/account-cache.json`-equivalent) at runner-pod startup or per-run. Token refresh story for long-lived runners. |

### Modified files

| Path | Change |
|---|---|
| `backend/app/services/runner_service.py` | Allowlist gains `suitecloud_validate`: `["suitecloud", "project:validate", "--server"]` with 180s timeout. Remove `sdf_validate` entry — no silent fallback. New `validator_engine` field on run records. |
| `backend/app/services/deploy_service.py` | Snapshot-hash freshness check on deploy. Reuse fresh validation; enqueue new one if stale; block if errors. |
| `backend/app/mcp/tools/workspace_tools.py` | `workspace_apply_patch` enqueues auto-validate via the orchestrator on success. `workspace_run_validate` continues to exist as the agent-callable tool but routes through the same orchestrator. |
| `backend/app/models/workspace.py` | New `ValidationHit` model: `id`, `run_id` (FK), `file`, `line`, `severity` (enum), `code`, `rule_id`, `message`, `fingerprint`, `created_at`. Plus `validator_engine`, `parser_version`, `has_errors`, `has_warnings`, `gate_status`, `snapshot_hash` on the existing run record. |
| `backend/app/services/chat/agents/workspace_agent.py` | Post-validate narration logic: batch findings by error family, retrieve one citation per family from RAG (cap N), call `workspace_propose_patch` for mechanically-fixable hits under loop budget. |
| `backend/Dockerfile.prod` (runner image) | Install `@oracle/suitecloud-cli` globally. Add startup hook that runs `suitecloud_auth_seeder` if `NETSUITE_TENANT_ID` env is set. |
| `backend/alembic/versions/NNN_validation_hits.py` | New migration for `ValidationHit` table + run record column additions. |

### Knowledge profile

`backend/app/services/chat/knowledge_profiles/suitescript_workspace.yaml` already lists all 7 `oracle/*` partitions in `rag_partitions` (verified during brainstorming). No change needed.

## Frontend changes

| Path | Change |
|---|---|
| `frontend/src/components/workspace/runs-panel.tsx` | Expandable validation-hits table under each validate run. Columns: file, line, severity badge (error red / warning amber), code, message. "Retry validate" button rendered only when latest validate run is `failed` or `stale`. |
| `frontend/src/lib/types.ts` | New `ValidationHit` and `ValidatorEngine` types. |
| `frontend/src/components/workspace/__tests__/runs-panel.test.tsx` | Vitest coverage for hits-table render + retry-button visibility states. |

No changes needed to chat panel or patch card — agent narration arrives as normal chat messages, and auto-proposed patches use the existing changeset card flow.

## Data flow

### Flow A — agent applies a patch

```
agent → workspace_apply_patch(changeset_id) → success
  → auto_validate_orchestrator.enqueue(workspace_id, snapshot_hash)
  → debounce 2s, cancel any superseded queued runs
  → runner subprocess: suitecloud project:validate --server
  → validate_parser → list[ValidationHit] + raw_output + parser_version
  → persist run + hits, derive has_errors / has_warnings / gate_status
  → SSE update to runs panel
  → agent reads findings:
      - batch by error family
      - for each family: retrieve 1 citation from oracle/* RAG, narrate (capped at N families)
      - for each mechanically-fixable hit (allowlist match) under loop budget:
          → workspace_propose_patch(draft_fix) → existing review-card flow
```

### Flow B — user clicks Deploy

```
deploy_service.run_deploy(changeset_id):
  snapshot_hash = hash(workspace + changeset + cli_version + engine + account_id)
  fresh = lookup(snapshot_hash, max_age=5min)
  if fresh and fresh.has_errors:
      → block, narrate hits in chat, surface in runs panel
  elif fresh and fresh.gate_status == "pass":
      → proceed to deploy_sandbox subprocess
  else:
      → enqueue new validate via orchestrator → wait → re-evaluate
```

### Flow C — validate fails (auth / network / CLI / parse)

```
run.status = failed
run.error_reason = "auth_required" | "network" | "cli_missing" | "parser_error" | "timeout"
runs panel shows red state + "Retry validate" CTA
no automatic re-run (avoids loop on persistent failure)
agent narrates: "validation infrastructure failed (<reason>); deploy blocked; retry to continue"
```

## Error handling / edge cases

| Case | Behavior |
|---|---|
| NetSuite auth missing/expired in runner | Run fails fast with `auth_required` reason. No local-mode fallback. Surface "reconnect NetSuite" CTA. Deploy blocked. |
| Server validate timeout (>180s) | Mark run `failed` with `timeout` reason. Don't auto-block deploy on NetSuite-side slowness — surface clearly, let user retry via manual button. |
| Parser failure on CLI output | Persist raw stdout/stderr verbatim. Synthesize one `parser_error` hit so it appears in the table. Stamp `parser_version` on the run. |
| Loop budget exhausted (3 auto-fix rounds same changeset) | Agent stops auto-proposing. Narrates remaining hits without action. Chat: "Hit auto-fix limit; remaining hits need manual review." |
| Same finding fingerprint re-appears mid-loop | Skip auto-propose. Narrate only. Prevents infinite cycle. |
| Workspace not a SuiteCloud project (no `manifest.xml`) | Refuse validate with clear error; deploy blocks. |
| Concurrent `workspace_apply_patch` calls | Orchestrator debounces — cancels superseded queued validates; only the latest snapshot is validated. |
| `suitecloud` CLI binary missing in runner image | Container startup fails with explicit setup error. Don't ship a half-broken runner. |

## Testing

| Layer | Coverage |
|---|---|
| Unit (parser) | Fixtures: clean run, errors-only, warnings-only, mixed, malformed, empty, stderr-only. Verify `ValidationHit` extraction + `parser_version` stamping + raw fallback. |
| Unit (orchestrator) | Debounce cancels superseded runs. Loop budget enforced per changeset. Fingerprint dedup blocks repeat auto-propose. |
| Unit (snapshot-hash freshness) | Hash changes on workspace edit / changeset update / CLI upgrade / engine swap / account change. Stale lookup misses → fresh run. |
| Unit (mechanical-fix classifier) | Deny-by-default. Allowlist of Oracle rule IDs maps to deterministic patch generators. Unknown rule → narrate only. |
| Unit (auth seeder) | Reads + decrypts credentials from `connections`; writes file in CLI's expected format; refreshes token when near expiry. |
| Integration | End-to-end: agent applies patch → auto-validate fires → hit narrated → fix proposed → user approves → re-validate clean → deploy gate passes. |
| Integration (deploy gate) | Stale validation triggers fresh run; fresh-and-clean reuses; errors block. |
| Benchmark (new `vs_mcp` case) | "I just edited X with an OWASP injection. Apply and deploy." → expect tool sequence: `workspace_apply_patch` → auto `workspace_run_validate` → narration mentions OWASP citation → `workspace_propose_patch` with fix → deploy blocked until fix applied. |
| Frontend (vitest) | Runs panel hits-table expand/collapse. Retry button only renders when last validate is `failed` / `stale`. Severity-badge styles. |

## Implementation constraints (codex-flagged)

These are baked into the design and must show up in the implementation plan:

1. Snapshot-hash freshness keying (workspace + changeset + CLI version + validator engine + account ID)
2. Debounce/coalesce auto-validate per workspace; cancel superseded queued runs
3. Loop budget: max 3 auto-fix rounds per changeset; finding fingerprinting blocks repeats
4. Deny-by-default mechanical-fix classifier — only deterministic transforms with Oracle-backed rule IDs
5. Best-effort parser + raw `stdout`/`stderr` fallback + `parser_version` stamp
6. First-class structured findings storage (`ValidationHit` table OR `findings_json` column on the run record — see open question 1) — never ad-hoc React parsing of raw stdout
7. `has_errors` / `has_warnings` / `gate_status` derived independently — never from exit code alone
8. No silent fallback to `sdf validate` — refuse loudly if `suitecloud` CLI is missing
9. Batched-by-family chat narration with citation cap; runs panel is canonical
10. 180s timeout for server mode (60s was the `sdf` legacy cap — too tight for `--server`)
11. NetSuite auth wiring: `suitecloud` CLI uses its own credential store, not REST OAuth. New seeder bridges from `connections` table.

## Out of scope

| Item | Why deferred |
|---|---|
| Inline editor markers (Monaco squiggles + hover popovers) | Needs new editor infrastructure; current code-viewer is read-only display. Separate project. |
| Cross-workspace validation policy | Each tenant uses its own NetSuite auth; no shared rules engine yet. |
| `suitecloud project:deploy --server` adoption | Keep existing deploy subprocess; only validate switches to Oracle CLI. |
| `validator_engine: sdf_legacy` fallback | Explicitly not a fallback. If a tenant needs the old behavior, file a follow-up. |
| Auto-fix for OWASP / architectural hits | Narrate-only forever — these are judgment calls, not deterministic transforms. |
| Server-validate-timeout override-once flow | Default is to block on timeout. Override-only is a future enhancement. |

## Open questions for the implementation phase

These don't block plan-writing but should be revisited during plan/execute:

1. `ValidationHit` as its own table vs. JSON column on `WorkspaceArtifact` — table preferred for query ergonomics, but artifact JSON wins on migration weight. Lean: table.
2. Auth seeding strategy — runner-pod startup (one-time) vs. per-run write (always-fresh). Lean: per-run write with 5min credential cache, since long-lived runners are rare and tokens may expire mid-shift.
3. Citation cap on chat narration — N=3 families with 1 citation each? Or N=5? Empirical tune after first benchmark run.

## Success criteria

- `suitecloud project:validate --server` runs successfully against staging tenant `ce3dfaad...` after deploy.
- Auto-validate fires within 3s of `workspace_apply_patch` success in the staging chat session.
- Validate hits render in the runs panel with file/line/severity/message.
- Agent narrates at least one hit with a verifiable RAG citation pulled from `oracle/owasp` partition (or other relevant partition for the hit family).
- Mechanically-fixable hit triggers `workspace_propose_patch` with a draft that, when applied, makes the hit disappear on re-validate.
- Deploy gate blocks when validate has errors; passes when clean within 5min snapshot-hash window.
- New `vs_mcp` benchmark case scores 1.00 (whether wired into the existing CI gate's sales suite or a new `--suite workspace` is an implementation-phase decision).

## References

- ClickUp ticket: 86b9pre59 (parent), 86b9u1xx7 (this sub-task)
- PR #74 — Oracle SuiteCloud SDK skill vendoring
- PR #75 — Oracle skill RAG seeding (1308 chunks across 7 `oracle/*` partitions)
- PR #76 — Docker image bake-in for skill content
- Oracle docs (`netsuite-ai-connector-instructions` skill) — validate guardrail spec
- Codex review: 2026-05-09 brainstorming session (15 findings, 11 baked into design, 4 reopened scope decisions)
