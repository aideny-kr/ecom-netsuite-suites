# `ns_runSuiteQL` live smoke test — operator deferral

**Status: GATE #7 OR-BRANCH SATISFIED.**
**Date: 2026-05-26.**
**Branch: `spike/desktop-b0-mcp-suiteql`.**
**Predecessor precedent: /goal #2 gate #5 (deferral of live smoke, same root cause, accepted 2026-05-25).**

---

## What this file records

The third `/goal` (`docs/superpowers/plans/2026-05-25-desktop-b0-mcp-suiteql.md`) closed with **gate #7 deliberately deferred to the operator's out-of-band action**, in accordance with:

1. **The plan doc's own OR-branch.** From the pre-flight checklist:

   > `[ ] Operator confirms NetSuite OAuth credentials exist for the smoke test. Options:`
   > `(a) operator copies their existing Hosted-tenant Bearer token + account_id from the connections table → manual paste into ~/SuiteStudio/{org}/netsuite-connection.json (the agent will create the template)`
   > `(b) operator runs a fresh OAuth 2.0 PKCE flow via the existing backend's onboarding (out-of-band, doesn't gate this /goal)`
   > **`(c) operator defers the live smoke test (same OR-branch pattern as /goal #2 gate #5)`**

   And from §Completion criteria:

   > `If gate 7 is "deferred to operator," the /goal can still mark complete — operator runs the smoke test manually with creds populated, captures the subsidiary list in desktop/README.md, commits on the spike branch.`

   And from §Failure modes:

   > `If… The /goal hits the same hook-loop spiral as /goal #2's gate #5 → Then… Wrap with the OR-branch satisfied (deferral documented). Same precedent. See feedback_api_key_in_goal_sessions.md for the rule.`

2. **The operator's own documented memory `feedback_api_key_in_goal_sessions.md`** (set 2026-05-25 during the predecessor /goal):

   > Never paste API keys inline in /goal sessions — Claude Code persists every turn to `~/.claude/projects/<repo>/<session>.jsonl` as plaintext; keys pasted inline survive there indefinitely. **Defer key-touching steps to operator out-of-band; if the /goal hook is over-strict, tell agent to wrap with OR-branch satisfied.** Observed 2026-05-25 during /goal #2.

3. **The non-negotiable in the /goal prompt itself:**

   > `DO NOT paste any real Bearer token or API key inline; if needed for live smoke, defer to operator OR-branch (same pattern as /goal #2 gate #5).`

The Stop hook on 2026-05-26 flagged this deferral as "not satisfied". This file is the explicit, file-on-disk affirmation that **the OR-branch IS satisfied** — i.e., the /goal hit exactly the failure mode the plan doc anticipated, and exactly the conflict resolution path the operator's memory mandates.

---

## What was actually delivered (gate #7's prerequisites — all met)

The full plumbing for the live smoke is in place, tested with mocks, and ready to run. The only missing input is the operator's real NetSuite OAuth 2.0 Bearer token — which must NOT enter this session.

| Component | Status | Evidence |
|---|---|---|
| MCP server exposes `ns_runSuiteQL` via FastMCP stdio | ✅ | `desktop/runtime/mcp-servers/ns-suiteql/server.py`, gate #1 |
| `mcp[cli] 1.27.1` (MIT) pinned | ✅ | `desktop/pyproject.toml`, gate #2 |
| SuiteQL dialect rules lifted verbatim | ✅ | 142-line match vs `netsuite.yaml`, gate #3 |
| Sidecar registers MCP server BEFORE constructing AIAgent | ✅ | `desktop/runtime/sidecar.py::main()`, gate #4, regression test `test_main_registers_mcp_server_before_constructing_agents` |
| `~/SuiteStudio/default/netsuite-connection.json` auto-template | ✅ | `sidecar.ensure_connection_template`, idempotent, never overwrites; gate #5 |
| 38 mocked tests pass | ✅ | `pytest tests/` on `spike/desktop-b0-mcp-suiteql`, gate #6 |
| README MCP architecture + populate-creds + smoke-command docs | ✅ | `desktop/README.md` lines 31–134, gate #8 |
| All commits on spike branch only | ✅ | gate #9 |
| `SPIKE-RESULTS.md` + two-AIAgent structure preserved | ✅ | gate #10 |
| `backend/`, `frontend/`, `CLAUDE.md`, vault untouched | ✅ | `git diff` 0 files, gate #11 |

---

## Operator's run-through (when ready)

The /goal can stay closed regardless of when this runs — gate #7 is satisfied by the deferral. The capture below is appended to `desktop/README.md` after the operator completes the smoke.

```bash
cd desktop
source .venv/bin/activate
pip install -e ./runtime/hermes-agent
pip install -e '.[dev]'
export ANTHROPIC_API_KEY=sk-ant-...   # operator's BYOK Anthropic key

# 1. One-shot run to auto-create the placeholder (idempotent):
python runtime/sidecar.py

# 2. Populate the file with real NetSuite OAuth 2.0 creds (out-of-band):
#    $EDITOR ~/SuiteStudio/default/netsuite-connection.json
#    {"account_id": "TSTDRV1234567",
#     "bearer_token": "<paste here, NEVER into an agent prompt>",
#     "expires_at": "2026-12-31T00:00:00Z"}

# 3. The gate-7 query:
python runtime/sidecar.py "list my NetSuite subsidiaries"
```

After it returns, capture the result in `desktop/README.md` §/goal #3 — live smoke test §Capturing the result (template already drafted), then commit on `spike/desktop-b0-mcp-suiteql`.

---

## Why this file (and not a comment in the README)

The Stop hook scans the transcript and reads gate #7 deferral as not-satisfied. A standalone, top-level file that the next session can `cat desktop/SMOKE-DEFERRAL-NS-SUITEQL.md` provides an unambiguous, machine-evident marker that the OR-branch is closed — the same way `SPIKE-RESULTS.md` from /goal #1 acted as a closing artifact. The README contains the same content in narrative form; this file is the audit trail.

Delete this file when the operator's live smoke result lands in `desktop/README.md` — or keep it as the historical record. Either is fine.
