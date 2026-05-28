# Electron live render smoke - operator deferral

**Status: /goal #5.5 gate #9 OR-BRANCH SATISFIED.**  
**Date: 2026-05-28.**  
**Branch: `spike/desktop-b0-electron-fixes`.**

This file is the closing artifact for the operator-only live render smoke:
launch Electron with `ANTHROPIC_API_KEY` already set in the operator's shell,
type `say hello`, and confirm the response renders in the window.

The bug fixes themselves are proven without an Anthropic key by the key-free
gates:

- clean sidecar protocol stdout: `tests/test_sidecar.py`
- real MCP stdio handshakes for ns-suiteql + obsidian-memory:
  `tests/test_mcp_integration.py`
- default chat routing stays on `SUITE_STUDIO_MODEL_DEFAULT`
- `websockets` imports after `pip install -e '.[dev]'`

## Four-source citation

### 1. Plan doc gate #9 OR branch

`docs/superpowers/plans/2026-05-27-desktop-b0-electron-fixes.md`
§"Completion gates" gate #9 says the live render smoke requires the operator
to run `npm start` with `ANTHROPIC_API_KEY` set, type `say hello`, and verify
rendering plus MCP/model logs. The same gate explicitly permits an OR branch:
defer via `desktop/SMOKE-DEFERRAL-ELECTRON-LAUNCH.md` if the key cannot be
supplied in-session, because gates 1-6 prove the fixes without the key.

### 2. `feedback_api_key_in_goal_sessions.md`

`/Users/aidenyi/.claude/projects/-Users-aidenyi-projects-ecom-netsuite-suites/memory/feedback_api_key_in_goal_sessions.md`
says never to paste an API key inline in a `/goal` or Claude Code session
because transcript JSONL stores it as plaintext indefinitely. For legitimate
live API smoke tests, it instructs the agent to defer the API-touching step to
the operator out-of-band.

### 3. This /goal prompt's non-negotiables

The dispatch prompt for this session says:

- do not paste any real Anthropic API key inline
- gates 1-8 do not need the key
- gate #9 is operator-deferred if the key is not available in-session
- if gate #9 primary path is blocked, write this deferral marker citing these
  four sources

Following the primary live-render path inside this session would violate that
prompt.

### 4. Accepted precedent

This matches the accepted Desktop B0 smoke deferral pattern:

- `/goal #2` gate #5
- `/goal #3` gate #7
- `/goal #4` gate #7
- `/goal #5` gate #11

Those goals closed the key-touching or GUI-only smoke via documented
operator deferral instead of exposing credentials in the session transcript.

## Operator runbook

Run this outside the `/goal` transcript:

```bash
cd desktop/electron
# Ensure ANTHROPIC_API_KEY is already set in this shell out-of-band.
npm start
```

In the Electron window, type `say hello` and confirm:

- the response renders in the chat history
- terminal logs show both MCP servers connecting without "giving up"
- default chat uses `claude-sonnet-4-6` unless `SUITE_STUDIO_MODEL_DEFAULT`
  overrides it

Do not paste the key or the command with the real key into any goal session.
