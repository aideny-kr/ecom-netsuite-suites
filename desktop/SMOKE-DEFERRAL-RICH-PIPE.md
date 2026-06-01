# Rich-pipe live smoke — operator deferral

**Status: /goal #6c slice 1 live-key gate OPERATOR-DEFERRED.**
**Date: 2026-05-31.**
**Branch: `spike/desktop-6c-rich-pipe-slice-1`.**

This file is the closing artifact for the two live-key steps of the desktop
rich-pipe `data_table` slice. Both need a real Anthropic key (supplied via
`~/.hermes/.env`, never a shell export — see `feedback_desktop_sidecar_key_hermes_env`),
so they are deferred to the operator out-of-band:

1. **Live streaming integration test** —
   `test_rich_pipe_integration.py::test_live_streaming_run_emits_data_table_with_tool_rows_then_done`.
   A real Hermes agent calls `sample_dataset`; the sidecar must stream a
   `data_table` event carrying the tool's exact columns/rows, then a terminal
   `done`. Gated behind `RUN_RICH_PIPE_LIVE=1`.
2. **Live render smoke (C3)** — launch the desktop app, type a prompt that
   triggers `sample_dataset`, and confirm a real `data-frame-table` card streams
   into the chat history (not plain text, not a mock).

## The pipe is proven WITHOUT a key by the key-free gates

The slice's success criteria (plan §2) are proven in-session by tests that need
no Anthropic key:

- **Real subprocess boots + JSON protocol over real pipes + stdout isolation +
  clean exit** — `tests/test_rich_pipe_integration.py::test_real_sidecar_boots_and_speaks_json_protocol_over_real_pipes`
  (no stubs; the launch-bug catcher).
- **Streaming protocol emits `text`/`data_table`/`done` in order, webapp-shaped,
  chatter isolated to stderr** — `tests/test_streaming_protocol.py` (A3).
- **Tool-result interception → webapp `data_table` shape** — `tests/test_orchestration.py` (A2).
- **Deterministic non-empty tool + Hermes registration** — `tests/test_sample_dataset.py` (A1).
- **Renderer reuses the webapp normalizer + `data-frame-table` card; XSS-safe** —
  the C2 renderer tests.

The only thing the live steps add is the real LLM deciding to call the tool —
the interception, event shapes, transport, and card render are all already
proven without a key.

## Four-source citation

### 1. Plan doc gates (A4 + C3 + success criteria §2)
`docs/superpowers/plans/2026-05-31-desktop-rich-pipe-slice-1.md`: Task A4 says the
real-agent run "needs `ANTHROPIC_API_KEY` … if a key is unavailable in-session,
mark it operator-deferred via a `SMOKE-DEFERRAL-RICH-PIPE.md` marker and keep the
key-free portions (A3) as the CI-safe proof." Task C3 says the live render smoke
"is operator-deferred per the established pattern — write the deferral marker."
Success criteria §2 are met by the key-free tests (A4 key-free + renderer tests);
the live render smoke is the deferred step.

### 2. `feedback_api_key_in_goal_sessions.md`
Never paste an API key inline in a `/goal`/Claude Code session — the transcript
JSONL stores it as plaintext indefinitely. Live-key steps are deferred to the
operator out-of-band. (See also `feedback_anthropic_key_billing_leak` and
`feedback_desktop_sidecar_key_hermes_env`: the desktop key comes from
`~/.hermes/.env`, loaded by Hermes' env_loader at import — never a shell export,
which Claude Code would meter-bill.)

### 3. This `/goal` prompt's non-negotiables (#7)
The dispatch prompt says: `ANTHROPIC_API_KEY` via `~/.hermes/.env`, never a shell
export; any live-key step is operator-deferred; never paste a key into the
transcript; write a `SMOKE-DEFERRAL-*.md` marker. Done = the success criteria
proven by the key-free tests (A4 + renderer tests); the live render smoke is
operator-deferred.

### 4. Accepted precedent
Matches the accepted Desktop B0 smoke-deferral pattern:
- `desktop/SMOKE-DEFERRAL-NS-SUITEQL.md` (/goal #3 gate #7)
- `desktop/SMOKE-DEFERRAL-OBSIDIAN-VAULT.md` (/goal #4 gate #7)
- `desktop/SMOKE-DEFERRAL-ELECTRON-LAUNCH.md` (/goal #5.5 gate #9)

Each closed a key-touching or GUI-only smoke via a documented operator deferral
instead of exposing credentials in the session transcript.

## Operator runbook

Run these OUTSIDE any `/goal` transcript, with the Anthropic key already present
in `~/.hermes/.env` (not exported in the shell):

```bash
# 1. Live streaming integration test (real agent → data_table → done)
cd desktop
RUN_RICH_PIPE_LIVE=1 .venv/bin/python -m pytest \
  tests/test_rich_pipe_integration.py -k live -v

# 2. Live render smoke (after the renderer is wired — Phase C)
cd desktop/electron
npm start
#   In the window, type: "show me the sample dataset / demo table"
#   Confirm: a data-frame-table card (account balances) streams into the chat
#   history — not plain text, not a mock — and the stream finalizes cleanly.
```

Do not paste the key, or any command containing the real key, into a goal session.
