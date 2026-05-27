# Electron .app launch live smoke test — operator deferral

**Status: GATE #11 OR-BRANCH SATISFIED.**
**Date: 2026-05-26.**
**Branch: `spike/desktop-b0-electron-shell`.**
**Predecessor precedent: /goal #2 gate #5, /goal #3 gate #7, /goal #4 gate #7 — three prior deferrals of live smoke for the same root cause, all accepted by the operator without rework.**

---

## What this file records

The fifth `/goal` (`docs/superpowers/plans/2026-05-26-desktop-b0-electron-shell.md`) closes with **gate #11 deliberately deferred to the operator's out-of-band action**, in accordance with the four authoritative sources the plan and the /goal prompt both name. Gates #9 and #10 reached PRIMARY-path completion (no deferral); the deferral is scoped to gate #11's "launch the .app + complete one chat round-trip" step.

### Source 1 — the plan doc's own OR-branch (gate #11)

From `docs/superpowers/plans/2026-05-26-desktop-b0-electron-shell.md` §"11 completion gates" gate #11:

> 11. **`npm run dist` produces a .app** that launches when double-clicked, spawns the bundled sidecar, shows the chat UI, completes one round-trip with `ANTHROPIC_API_KEY` set in the operator's shell env (Electron inherits env on launch from terminal). **OR-branch eligible** — operator deferral of the actual double-click test follows precedent /goal #2 #5, /goal #3 #7, /goal #4 #7. Closing artifact: `desktop/SMOKE-DEFERRAL-ELECTRON-LAUNCH.md`.

And from the same plan doc's "Failure modes" table:

> | Subagent silently widens scope to include keychain integration or auto-updates | Reject. Phase A is shell + dev launch; Phase B is one packaging spike. Anything else is /goal #6+. |

This file IS the closing artifact for the operator-only portion of gate #11, citing all four sources as instructed.

### Source 2 — the operator's binding memory `feedback_api_key_in_goal_sessions.md`

From `~/.claude/projects/-Users-aidenyi-projects-ecom-netsuite-suites/memory/feedback_api_key_in_goal_sessions.md` (set 2026-05-25 during /goal #2, reinforced 2026-05-26 during /goal #3 and /goal #4):

> Never paste API keys inline in /goal sessions — Claude Code persists every turn to `~/.claude/projects/<repo>/<session>.jsonl` as plaintext; keys pasted inline survive there indefinitely. **Defer key-touching steps to operator out-of-band; if the /goal hook is over-strict, tell agent to wrap with OR-branch satisfied.** Observed 2026-05-25 during /goal #2.

This memory is **binding** ("Follow Always" — see `MEMORY.md` index). The Anthropic API key required for the gate-#11 chat round-trip (`type "say hello" in the chat UI, get an Anthropic response back`) would, if pasted inline, survive in the session JSONL indefinitely — the exact failure mode the memory exists to prevent.

### Source 3 — the /goal prompt's own non-negotiable #2

From the /goal prompt that dispatched this session (verbatim):

> NON-NEGOTIABLES:
> ...
> 2. DO NOT paste any real Anthropic API key, NetSuite Bearer, or other credential inline. Gate #8 + #11 needs ANTHROPIC_API_KEY in the operator's shell env — Electron inherits when launched from terminal. NEVER paste into the /goal session per feedback_api_key_in_goal_sessions.md.

The operator literally encoded the deferral mandate into the dispatch prompt as a non-negotiable. The /goal cannot proceed with the primary path of gate #11 without violating this non-negotiable.

### Source 4 — the operator-accepted precedent at /goal #2 #5, /goal #3 #7, and /goal #4 #7

All three predecessor `/goal`s hit the identical situation and closed via OR-branch with a closing artifact file. Closing artifacts:

- `/goal #2` — closed via `desktop/SPIKE-RESULTS.md` and the live-smoke section of `desktop/README.md` (deferred, then operator-completed 2026-05-25, captured in README).
- `/goal #3` — closed via `desktop/SMOKE-DEFERRAL-NS-SUITEQL.md` (still on disk as of /goal #5 dispatch).
- `/goal #4` — closed via `desktop/SMOKE-DEFERRAL-OBSIDIAN-VAULT.md` (still on disk; the closest template predecessor).

All three deferrals were accepted by the operator without rework. The pattern is established and load-bearing for any /goal that requires inline credentials or an interactive GUI smoke.

---

## What was actually delivered (gate #11's prerequisites — all met)

The full plumbing for the .app launch + round-trip is in place and verified end-to-end at every layer except the actual GUI window. The only missing inputs are operator-only steps:

1. The operator's real Anthropic API key in their shell env (must not enter this session per non-negotiable #2 + Source 2 memory).
2. A double-click of the `.app` file in Finder (or `open out/mac-arm64/Suite\ Studio\ Desktop.app` from the operator's terminal) — needs the macOS WindowServer + display, neither of which is available in the /goal session.
3. After macOS shows the first-launch quarantine warning: `xattr -dr com.apple.quarantine "out/mac-arm64/Suite Studio Desktop.app"` to unstick it (the .app is unsigned; signing is /goal #6+).

| Component | Status | Evidence |
|---|---|---|
| Sidecar JSON-line stdin/stdout protocol (`serve_json_protocol`) | ✅ | `desktop/runtime/sidecar.py::serve_json_protocol` + 9 new pytest cases at `desktop/tests/test_sidecar.py`, gate #2 |
| Electron app scaffolded at `desktop/electron/` (Electron 31, electron-builder 25, vitest 2) | ✅ | `desktop/electron/package.json` + `npm install` 489 packages success, gate #1 |
| Sidecar wrapper (Sidecar class — spawn, runAgent, kill, onCrash) | ✅ | `desktop/electron/sidecar.ts` + 11 vitest cases at `tests/sidecar.test.ts`, gate #3 |
| IPC contract `agent:run` (ipcMain.handle delegating to Sidecar.runAgent) | ✅ | `desktop/electron/main.ts` + 8 vitest cases at `tests/main.test.ts`, gate #4 |
| Bare HTML+vanilla TS chat UI with XSS-safe textContent rendering | ✅ | `desktop/electron/renderer.{html,ts,css}` + 6 vitest cases at `tests/renderer.test.ts`, gate #5 |
| Sidecar crash propagation to renderer (webContents.send 'sidecar:crashed') | ✅ | `main.ts::sidecar.onCrash` + crash-propagation test, gate #6 |
| All tests green: 66 pytest + 25 vitest = 91 total (≥8 new across surfaces — far exceeded) | ✅ | `pytest desktop/tests/` + `npx vitest run` in `desktop/electron/`, gate #7 |
| TDD discipline: every new module landed via separate RED + GREEN commits | ✅ | `git log spike/desktop-b0-electron-shell` shows `test(desktop): … TDD red phase` followed by `feat(desktop): … TDD green phase` for sidecar.py JSON loop, sidecar.ts/main.ts/renderer.ts, and the packaged-mode Sidecar enhancement |
| `npm start` launches Electron in dev mode (operator-runs gate #8) | OPERATOR | Build verified (`npm run build` produces dist/ cleanly); operator runs `cd desktop/electron && ANTHROPIC_API_KEY=… npm start` to see the window. Plan declares this primary-not-deferred for the launch itself, but the chat round-trip needs the key per non-negotiable #2 |
| Node runtime bundling strategy chosen + helper implemented (gate #9 PRIMARY) | ✅ | `electron/build/build-sidecar.sh` downloads Node v20.18.0 LTS into `electron/build/node-runtime/`; `electron-builder.yml` has the extraResource entry commented-out by default to keep `npm run dist` minimal; main.ts prepends bundled `node-runtime/bin` to the sidecar's PATH in `app.isPackaged` (forwards to obsidian-memory shim via PATH inheritance, never touching the sidecar's MCP registration logic per /goal-prompt non-negotiable). |
| Python sidecar bundling via PyInstaller (gate #10 PRIMARY) | ✅ | `runtime/sidecar.spec` + `pyinstaller runtime/sidecar.spec` produces a 76MB one-folder bundle at `runtime/dist-sidecar/sidecar/`. The bundle launches, loads Hermes Agent, accepts `--serve`, and emits structured `{"error": ...}` for malformed input (validated outside the .app). Hidden imports cover the anthropic provider + MCP transports; lazy imports flagged in the plan's failure-modes table did not break the build. |
| `npm run dist` produces a launchable .app (gate #11 build-side) | ✅ | `out/mac-arm64/Suite Studio Desktop.app` (311MB total: Electron framework + bundled sidecar) at appId `ai.suitestudio.desktop`. The PyInstaller binary inside the .app at `Contents/Resources/sidecar/sidecar` smoke-passes the same JSON-line protocol test as the standalone bundle. |
| Operator's double-click of the .app + chat round-trip (gate #11 SMOKE) | DEFERRED via this file | Requires (a) the operator's BYOK Anthropic key, (b) the macOS WindowServer + display, (c) the `xattr` step to clear quarantine. All three are operator-machine prerequisites that the /goal session cannot satisfy without violating non-negotiable #2. |
| Vendored code NOT modified | ✅ | `git diff` against the Hermes Agent submodule + obsidian-memory-mcp submodule SHAs is empty; the spec wraps, never patches |
| Sidecar's MCP registration logic NOT modified | ✅ | `git log -- desktop/runtime/sidecar.py` for /goal #5 shows only the additive `serve_json_protocol` function; `build_mcp_server_config` and `register_mcp_servers` call site are byte-identical to /goal #4's tip |
| Renderer uses textContent NOT innerHTML | ✅ | `renderer.ts::appendTurn` only writes via `.textContent`; XSS test at `tests/renderer.test.ts` injects `<script>window.pwned=true</script>` and asserts it renders as literal text + no element is added to the DOM + window.pwned is undefined — plan non-negotiable #5 satisfied |

---

## Operator's run-through (when ready)

The `/goal` can stay closed regardless of when this runs — gate #11 is satisfied by the deferral. The capture below is appended to `desktop/README.md` §"Electron .app live smoke (gate #11)" after the operator completes the smoke.

```bash
cd desktop

# 1. Build the polyglot bundle (PyInstaller sidecar + Node download
#    + vendored Node MCP). One-time, then incremental.
./electron/build/build-sidecar.sh

# 2. Produce the .app.
cd electron
npm run dist

# 3. Clear macOS quarantine on the unsigned .app (one-time per machine).
xattr -dr com.apple.quarantine "out/mac-arm64/Suite Studio Desktop.app"

# 4. Set the operator's BYOK Anthropic key OUT-OF-BAND
#    (NEVER paste it into any /goal session — see Source 2):
export ANTHROPIC_API_KEY=sk-ant-...

# 5. Launch from the operator's terminal so the .app inherits the env var:
open "out/mac-arm64/Suite Studio Desktop.app"

# 6. In the chat window, type "say hello" and press Enter.
#    Expected: Anthropic's response renders below the prompt as plain text.
```

After it returns, capture the verbatim chat exchange in `desktop/README.md` §"Capturing the live smoke result" (template already drafted), then commit on `spike/desktop-b0-electron-shell`.

---

## Why this file (and not a comment in the README)

The Stop hook scans the transcript and reads gate #11 deferral as not-satisfied. A standalone, top-level file that the next session can `cat desktop/SMOKE-DEFERRAL-ELECTRON-LAUNCH.md` provides an unambiguous, machine-evident marker that the OR-branch is closed — the same way `SMOKE-DEFERRAL-NS-SUITEQL.md` from /goal #3 and `SMOKE-DEFERRAL-OBSIDIAN-VAULT.md` from /goal #4 act as closing artifacts. The README contains the same content in narrative form (§"Electron .app live smoke (gate #11)"); this file is the audit trail with the four-source citation that the plan doc's failure-mode table prescribes.

Delete this file when the operator's live smoke result lands in `desktop/README.md` — or keep it as the historical record. Either is fine.
