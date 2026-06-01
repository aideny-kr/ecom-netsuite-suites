# Desktop orchestration seed (rich-pipe slice 1)

Extraction-shaped core that wraps the vendored Hermes `AIAgent.run_conversation`
and turns a turn into a stream of **typed events** matching the webapp's
`ChatStreamEvent` shapes (`frontend/src/lib/chat-stream.ts`). The desktop
sidecar is the only adapter that serializes these events to stdout; this package
imports nothing from Electron/IPC so the later `packages/agent` extraction
*relocates* it rather than rewriting it.

## A0 research finding — the Hermes hooks the runner uses

All citations are against the vendored Hermes Agent at `desktop/runtime/hermes-agent`
(tag `v2026.5.16`, `version = "0.14.0"`). Verified by reading the source, not inferred.

- **Assistant text deltas** → set the **instance attribute** `agent.stream_delta_callback`
  (`run_agent.py:1172` init kwarg, `:1397` stored on `self`). It fires per text
  delta through `_fire_stream_delta` (`run_agent.py:7976`, dispatched `:8018`).
  At end-of-stream Hermes fires it once with `None` (`run_agent.py:15264-15266`),
  so the runner **guards against falsy/None deltas** before emitting a `text` event.
- **Named tool's structured result** → set the **instance attribute**
  `agent.tool_complete_callback` (`run_agent.py:1167` init kwarg, `:1391` stored).
  It is invoked **immediately after the tool runs** as
  `(tool_call_id, tool_name, tool_args, tool_result)` — concurrent path
  `run_agent.py:11382`, sequential path `:11804`. `tool_result` is the tool
  handler's **return value, a JSON string**, so the runner `json.loads` it and,
  when `tool_name` is the sample-dataset tool, converts `{columns, rows}` into a
  webapp-shaped `data_table` event.
- **Turn result / tokens** → `run_conversation(user_message, ...)`
  (`run_agent.py:12094`) returns a dict carrying `final_response`, `messages`,
  and token counters `total_tokens` / `input_tokens` / `output_tokens`
  (populated from the session counters ~`run_agent.py:15933`). The runner emits a
  terminal `done` event with `tokens_used` = `total_tokens` (fallback
  `input + output`, else `0`).

**Why instance-attribute callbacks (not a per-call param or message post-processing):**
they are plain attributes settable per-run on a **reused** agent, so the sidecar
keeps one agent across queries (no per-turn reconstruction) while each run gets
its own `emit` sink. The runner depends only on this duck-typed surface
(`stream_delta_callback`, `tool_complete_callback`, `run_conversation`), which is
what lets the A2 test drive it with a fake agent **key-free**. (`stream_callback`
exists as a per-call `run_conversation` param at `:12100`, but tool results have
**no** per-call hook — only the instance attribute — so we set both attributes for
symmetry.)

## Tool registration + the `tools` package collision (A1)

A local Python tool registers via `registry.register(name, toolset, schema, handler, check_fn=...)`
(`tools/registry.py:234`). With `enabled_toolsets=None` (the sidecar default) every
registered toolset whose `check_fn` passes is exposed to the model
(`model_tools.py::get_tool_definitions`), so a tool registered with `check_fn=lambda: True`
is offered to the live agent.

**Deviation from the plan's `desktop/runtime/tools/sample_dataset.py` path —
deliberate, to avoid a confirmed collision.** Hermes owns the top-level `tools`
package (`hermes-agent/tools/__init__.py` is a *regular* package) and exposes it
through an **editable meta-path finder** (`__editable___hermes_agent_0_14_0_finder.py`,
which `sys.meta_path.append(_EditableFinder)`). When the sidecar runs as
`python runtime/sidecar.py`, `desktop/runtime/` is `sys.path[0]`, so a
`desktop/runtime/tools/` directory would have `PathFinder` resolve the **`tools`
package itself to our dir** — shadowing Hermes' `tools/__init__.py` (726B of real
init) and leaving submodule resolution dependent on the finder's append-order
quirk. Empirically it "works" today, but it is brittle (a future `pip install -e`
or setuptools change that inserts the finder at the *front* breaks it) and is
exactly the silent-divergence trap called out in `feedback_netsuite_oauth_reuse_webapp`.
So the demo tool lives in a collision-free **`desktop/runtime/suite_tools/`**
package, imported as `suite_tools.sample_dataset`. Future desktop-local tools land
there too.
