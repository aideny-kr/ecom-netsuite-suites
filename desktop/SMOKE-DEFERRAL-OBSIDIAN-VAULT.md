# `obsidian-memory` live entity-write smoke test — operator deferral

> ## ✅ RESOLVED — live smoke PASSED 2026-06-18 (no operator key handling)
>
> The deferral's root cause (a forced `ANTHROPIC_API_KEY`) was removed by the
> Keychain-auth slice (`feat(desktop): resolve Anthropic auth from Claude Code
> Keychain, not just env`). The live smoke then ran headlessly off the
> signed-in Claude Code **macOS Keychain** OAuth credential — **no env key, no
> `~/.hermes/.env`, nothing in any transcript**:
>
> ```
> $ .venv/bin/python runtime/sidecar.py \
>     "create an entity called 'TestEntity' with observation 'first vault write 2026-06-18 keychain auth'"
> 🔑 Using token: sk-ant-o...          # OAuth token from the Keychain, NOT an env key
> 🔧 Tool 1: mcp_obsidian_memory_create_entities(['entities'])   # agent chose the tool autonomously
> ✅ Tool 1 completed
> 🤖 Assistant: Done. `TestEntity` created with that observation.
> exit 0
> ```
>
> Persisted memory note `~/SuiteStudio/default/TestEntity.md`:
> ```markdown
> ---
> entityType: test
> created: '2026-06-18'
> updated: '2026-06-18'
> ---
>
> # TestEntity
>
> ## Observations
> - first vault write 2026-06-18 keychain auth
> ```
>
> Proves: composed Hermes agent + `obsidian-memory` MCP autonomously writes a
> markdown memory note to the vault. The history below is kept as the audit trail.

**Status: GATE #7 OR-BRANCH SATISFIED.**
**Date: 2026-05-26.**
**Branch: `spike/desktop-b0-obsidian-vault`.**
**Predecessor precedent: /goal #2 gate #5, /goal #3 gate #7 — both deferrals of live smoke for the same root cause, accepted by the operator.**

---

## What this file records

The fourth `/goal` (`docs/superpowers/plans/2026-05-26-desktop-b0-obsidian-vault.md`) closed with **gate #7 deliberately deferred to the operator's out-of-band action**, in accordance with the four authoritative sources the plan and the /goal prompt both name:

### Source 1 — the plan doc's own OR-branch (gate #7 option (b))

From `docs/superpowers/plans/2026-05-26-desktop-b0-obsidian-vault.md` §"11 completion gates" gate #7:

> 7. **Live entity-write smoke test** — operator runs:
>    `python runtime/sidecar.py "create an entity called 'TestEntity' with observation 'first vault write 2026-05-XX'"`
>    and verifies a markdown file appears under `~/SuiteStudio/default/`. Options:
>    - (a) Operator runs out-of-band with their existing `ANTHROPIC_API_KEY` from env (recommended; key never enters /goal session)
>    - **(b) Operator defers via OR-branch (same precedent as /goal #2 #5 and /goal #3 #7) — `desktop/SMOKE-DEFERRAL-OBSIDIAN-VAULT.md` is the closing artifact**

And from the same plan doc's "Failure modes" table:

> | The Stop hook hits the same loop spiral as /goal #2 #5 and /goal #3 #7 on gate #7 | Wrap with the OR-branch satisfied. Write `desktop/SMOKE-DEFERRAL-OBSIDIAN-VAULT.md` citing: (1) this plan doc's gate #7 option (b), (2) `feedback_api_key_in_goal_sessions.md` memory, (3) the /goal prompt's non-negotiable, (4) precedent /goal #2 #5 and /goal #3 #7. |

This file IS that closing artifact, citing all four sources as instructed.

### Source 2 — the operator's binding memory `feedback_api_key_in_goal_sessions.md`

From `~/.claude/projects/-Users-aidenyi-projects-ecom-netsuite-suites/memory/feedback_api_key_in_goal_sessions.md` (set 2026-05-25 during /goal #2, reinforced 2026-05-26 during /goal #3):

> Never paste API keys inline in /goal sessions — Claude Code persists every turn to `~/.claude/projects/<repo>/<session>.jsonl` as plaintext; keys pasted inline survive there indefinitely. **Defer key-touching steps to operator out-of-band; if the /goal hook is over-strict, tell agent to wrap with OR-branch satisfied.** Observed 2026-05-25 during /goal #2.

This memory is **binding** ("Follow Always" — see `MEMORY.md` index). The Anthropic API key required by the sidecar to drive `AIAgent.run_conversation` would, if pasted inline, survive in the session JSONL indefinitely — the exact failure mode the memory exists to prevent.

### Source 3 — the /goal prompt's own non-negotiable #2

From the /goal prompt that dispatched this session (verbatim):

> NON-NEGOTIABLES:
> ...
> 2. DO NOT paste any real Anthropic API key, NetSuite Bearer token, or other credential inline. If gate #7 primary path needs the key, defer to operator OR-branch per `~/.claude/projects/-Users-aidenyi-projects-ecom-netsuite-suites/memory/feedback_api_key_in_goal_sessions.md`.

The operator literally encoded the deferral mandate into the dispatch prompt as a non-negotiable. The /goal cannot proceed with the primary path of gate #7 without violating this non-negotiable.

### Source 4 — the operator-accepted precedent at /goal #2 #5 and /goal #3 #7

Both predecessor `/goal`s hit the identical situation: a live smoke that required the operator's BYOK Anthropic key (and at /goal #3, additionally a NetSuite Bearer token), and both resolved by deferring via OR-branch with a closing artifact file. Closing artifacts:

- `/goal #2` — closed via `desktop/SPIKE-RESULTS.md` and the live-smoke section of `desktop/README.md` (deferred, then operator-completed 2026-05-25, captured in README).
- `/goal #3` — closed via `desktop/SMOKE-DEFERRAL-NS-SUITEQL.md` (this file's template predecessor; still on disk as of /goal #4 dispatch).

Both deferrals were accepted by the operator without rework. The pattern is established and load-bearing for any /goal that requires inline credentials.

---

## What was actually delivered (gate #7's prerequisites — all met)

The full plumbing for the live entity-write smoke is in place and verified with mocks. The only missing inputs are operator-only steps:

1. The operator's real Anthropic API key (must not enter this session per non-negotiable #2 + Source 2 memory).
2. A built `dist/index.js` in the vendored Node submodule (`npm install && npm run build` inside `desktop/runtime/obsidian-memory-mcp/`) — this is operator-side machine setup, not a code deliverable.

| Component | Status | Evidence |
|---|---|---|
| `obsidian-memory-mcp` vendored at pinned commit `c3708dd` | ✅ | `.gitmodules` + `desktop/runtime/obsidian-memory-mcp/` at the locked SHA, gate #1 |
| Pin target documented with rationale | ✅ | `desktop/README.md` §"Vendoring strategy — Obsidian Memory MCP at c3708dd", gate #2 |
| MCP server lives at `desktop/runtime/mcp-servers/obsidian-memory/` | ✅ | runtime layout mirrors /goal #3's pattern; plan non-negotiable #3 satisfied, gate #3 |
| Sidecar registers BOTH `ns-suiteql` and `obsidian-memory` BEFORE constructing AIAgent | ✅ | `desktop/runtime/sidecar.py::main()` + `build_mcp_server_config`; regression test `test_main_registers_both_mcp_servers_before_agent`, gate #4 |
| Vault auto-scaffold idempotent, frontmatter-only 00-Home.md | ✅ | `sidecar.ensure_vault_scaffold`, regression test `test_ensure_vault_scaffold_is_idempotent` + `test_ensure_vault_scaffold_home_md_is_frontmatter_only`, gate #5 |
| 19 new mocked tests added (+11 obsidian-memory shim, +8 sidecar); test count goes from 38 → 57 | ✅ | `pytest desktop/tests/` on `spike/desktop-b0-obsidian-vault` — 57 passed, gate #6 |
| TDD discipline: separate RED commits then GREEN commits, no merged commits | ✅ | `git log` shows `test(desktop): … TDD red phase` followed by `feat(desktop): … TDD green phase` for both modules — see `5f049ad` → `718b6c0` (shim) and `3dba393` → `1728f05` (sidecar) |
| README extended with vault architecture + scaffold runbook + entity-write runbook + deferral marker reference | ✅ | `desktop/README.md` §"Vault scaffold + obsidian-memory MCP server", gate #8 |
| Vendored code NOT modified | ✅ | `git diff` against the submodule SHA is empty; shim wraps, never patches; plan non-negotiable #4 satisfied |
| Vault scaffold is frontmatter-only (no fabricated operator content) | ✅ | `test_ensure_vault_scaffold_home_md_is_frontmatter_only` parses the file and asserts post-frontmatter body is whitespace-only; plan non-negotiable #5 satisfied |

---

## Operator's run-through (when ready)

The `/goal` can stay closed regardless of when this runs — gate #7 is satisfied by the deferral. The capture below is appended to `desktop/README.md` §"Capturing the live smoke result" after the operator completes the smoke.

```bash
cd desktop
source .venv/bin/activate
pip install -e ./runtime/hermes-agent
pip install -e '.[dev]'

# 1. Build the vendored Node.js MCP server (one-time after submodule update):
( cd runtime/obsidian-memory-mcp && npm install && npm run build )

# 2. Set the operator's BYOK Anthropic key OUT-OF-BAND
#    (NEVER paste it into any /goal session — see Source 2):
export ANTHROPIC_API_KEY=sk-ant-...

# 3. First run scaffolds the vault (idempotent):
python runtime/sidecar.py
# → creates ~/SuiteStudio/default/ + .obsidian/ + 00-Home.md (frontmatter only)

# 4. The gate-7 entity-write prompt:
python runtime/sidecar.py "create an entity called 'TestEntity' with observation 'first vault write 2026-05-XX'"

# 5. Verify the file landed:
ls ~/SuiteStudio/default/
cat ~/SuiteStudio/default/TestEntity.md
```

After it returns, capture the verbatim output in `desktop/README.md` §"Capturing the live smoke result" (template already drafted), then commit on `spike/desktop-b0-obsidian-vault`.

---

## Why this file (and not a comment in the README)

The Stop hook scans the transcript and reads gate #7 deferral as not-satisfied. A standalone, top-level file that the next session can `cat desktop/SMOKE-DEFERRAL-OBSIDIAN-VAULT.md` provides an unambiguous, machine-evident marker that the OR-branch is closed — the same way `SMOKE-DEFERRAL-NS-SUITEQL.md` from /goal #3 acted as a closing artifact. The README contains the same content in narrative form (§"Live entity-write smoke runbook (gate #7)"); this file is the audit trail with the four-source citation that the plan doc's failure-mode table prescribes.

Delete this file when the operator's live smoke result lands in `desktop/README.md` — or keep it as the historical record. Either is fine.
