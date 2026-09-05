# Celigo flow map in chat — read-only access for the unified agent

**Status:** design, 2026-09-04. Operator decisions taken the same day: the assistant may read the synced
flow map (integrations, flows, steps and routers, schedules and stall state, open errors *with* their
messages, script names and attachment sites). **Script bodies stay out** — the N2 rule holds and every
drawer notice ("never sent to the assistant") stays true.
**Tier:** T2 (prompt-pollution surface, feature flag, tool inventory). Gate + vs-MCP benchmark before merge.
**Ticket:** ClickUp 86bbvbhkk. **Branch:** `feat/celigo-flow-sizing-and-agent-access`.
**Research:** design brief synthesized from three codebase maps (job tmp `agent-access-design-brief.md`);
the data-layer map is replaced by direct reading of `app/api/v1/celigo_flows.py`.

## 1. Problem

The flow map shipped in #220/#221 is visible to humans only. The operator wants to ask the assistant
things like "which Celigo flows are failing and why?" and get the same honest numbers the pages show.
Today the only Celigo-shaped chat tools are the external `celigo_mcp` connector's (Celigo's own hosted
API, live state, no history) — and Framework has no such connector; it has the flow-map token
connection (`connections.type = "celigo"`). Nothing in chat reaches the `celigo_*` tables.

## 2. Judgement criteria

- **One query definition over two callers.** The pages and the tools must not drift: the route
  aggregations move into a service module both call. A chat number that disagrees with the page is
  worse than no number.
- **Honesty travels with the data, not the prompt.** `errors_checked_at`, snapshot age, truncation and
  stall verdicts are carried in the result (`caveats` + columns); the profile only tells the model to
  relay them. A prompt rule can be ignored; a caveat in a table is shown.
- **Absence of a tool over a guarded parameter.** No tool can return a script body because no tool
  selects `content`; N2 is enforced by shape, not by a check.
- **Numbers reach the user through the table path.** Results are `data_table`-categorised so
  `_intercept_tool_result` shows them and tells the model not to restate them.
- **Gate where the decision is made.** Inventory is gated on the flow-map connection + the `celigo`
  feature flag; every `execute()` re-checks both (a `tool_use` emitted before the flag flipped must
  still fail closed). The existing flag is reused — a chat-only flag would be a second copy of a fact.

## 3. Tool set (four narrow local tools, dotted registry names → `celigo_*` for the model)

All share one envelope: `{"columns", "rows", "row_count", "query", "truncated", "caveats"}`.
`caveats` is the honesty channel; never an `error` key for a caveat (governance treats truthy `error`
as a failed call). Source is the Postgres mirror only, never the live Celigo API.

| Tool | Args | One row is | Backing (extracted into `services/celigo/read_queries.py`) |
|---|---|---|---|
| `celigo.integrations` | — | a production integration: name, flow count, scheduled / on-demand / paused counts, open errors, root causes, errors-checked rollup, last modified | `list_integrations` + `get_sync_status` |
| `celigo.flows` | `integration` (id or name fragment), `only_open_errors`, `only_stalled`, `limit ≤ 200` | a flow: integration, name, paused?, schedule (humanised or raw cron), timezone, last run, run state, step / router / branch / lookup counts, open errors, root causes, errors checked | `list_integration_flows`, predicates reused verbatim |
| `celigo.flow_steps` | `flow` (id or unique name) | a step or router: sequence, kind, adaptor family, referenced object name, branch, scripts attached here (name + site path) | `get_flow_detail`; **`content` / `content_hash` never selected** |
| `celigo.flow_errors` | `flow` (optional → tenant-wide), `status` open/resolved, `limit ≤ 50` groups | a root-cause group: flow, source, code, occurrences, first / last seen, one sample message (≤ 300 chars), distinct trace keys, purge deadline | `list_flow_errors` (+ tenant-wide variant, same GROUP BY without the flow predicate) |

Execute skeleton mirrors `mcp/tools/data_sample.py`: `db` / `tenant_id` from `context`; arg validation
raises `ValueError` only for caller mistakes; no db → empty envelope + caveat; gating (§5) → empty
envelope + caveat; query errors → empty envelope + `caveats=["Celigo flow map could not be read"]`
and a warning log that never formats a message column. Every query carries `tenant_id` in application
code; FORCE-RLS on the seven `celigo_*` tables is the backstop, not the mechanism.

## 4. Extraction (task 1, the regression risk)

`celigo_flows.py` carries ~800 lines of inline aggregation (`list_integrations`, `list_integration_flows`,
`get_flow_detail`, `list_flow_errors`). Move each into `read_queries.py` as `(db, tenant_id, …) →
dataclasses`; routes and tools become adapters. `_join_production_integration` and
`_get_celigo_connection` (today in `connector_status.py`) move with them. **Parity test first**: each
route's JSON on the seeded tenant is byte-equal before and after.

Schedule humanisation and stall detection exist only in TypeScript (`components/celigo/schedule.ts`).
Task 2 ports the same rules to `services/celigo/run_state.py`: `flow_run_state(flow, as_of) →
paused | on_demand | scheduled | stalled | unknown`; `stalled` only when enabled, the six-field cron
parses, and `as_of − last_executed_at` exceeds twice the cron's longest gap; unparseable cron or no
last run → `unknown`, never `stalled`; evaluated against the snapshot time, never `now()`. Table-driven
tests share cases with the TS tests so the two cannot drift silently.

## 5. Gating

- **Inventory:** the four tools appear iff the tenant has an active flow-map connection
  (`connections.type = "celigo"`) **and** the `celigo` feature flag is on. This is a DB read inside
  `build_all_tool_definitions`, alongside the existing connector lookup. It is *not* keyed on the
  `celigo_mcp` connector (Framework has none), so `_CONNECTOR_GATED_TOOLS` is left alone and its
  dual role (unlock local tools / exclude providers from external definitions) is untouched.
- **Dispatch:** every `execute()` re-checks the flag (`feature_flag_service.is_enabled`) and the
  connection; either missing → empty envelope with "Celigo is turned off for this workspace" /
  "No Celigo connection".
- **Mutation guard:** local dotted tools never enter the `ext__` HITL classifier; the family is
  read-only by construction (no tool has a write path). Documented, and asserted by a test that no
  registry entry for `celigo.*` names a handler outside the read module.

## 6. Chat wiring

1. `mcp/registry.py`: four `TOOL_REGISTRY` entries with `params_schema`.
2. `mcp/governance.py`: four `TOOL_CONFIGS` (timeout 15 s, 30/min, allowlisted params).
3. `chat/nodes.py`: add to `ALLOWED_CHAT_TOOLS` (the recon family is deliberately absent because the
   chat path lacked gating — this family carries its own, §5).
4. `chat/tool_categories.py::_EXACT`: all four → `data_table`; add `is_celigo_source(name)` =
   external Celigo tool **or** `name.startswith("celigo_")`.
5. `chat/orchestrator.py::_compute_source_pin_update`: use `is_celigo_source` so a `data_table` Celigo
   result never pins the session to NetSuite.
6. `prompt_assembler.build_source_pin_hint`: `celigo → "Celigo"`.
7. `tests/test_prompt_tool_sync.py`: the four sanitized names join the known set. Tool names never
   appear in prompts except through `{{TOOL_INVENTORY}}`.

## 7. Knowledge profile — `knowledge_profiles/celigo.yaml`

`profile_id: celigo`, `trigger_tools` = the four names, `rag_partitions: []`, and a prompt fragment
written as intent and honesty, with **no table, column, or schema words**:

1. Which tool for what (landscape → one integration / filters → how a flow is built → what is breaking).
2. The map is a nightly snapshot of production only; relay every caveat verbatim before interpreting.
3. Tables are shown automatically; never restate, sum or rank the numbers in prose.
4. A zero with an unchecked flag is not a verified zero — say so.
5. Script bodies are never available; describe scripts by name and where they attach; never guess code.
6. Error messages may contain customer identifiers; quote one only when asked what an error says.
7. Read-only: nothing can be run, retried, enabled or edited; name what you would act on and stop.
8. Prefer these tools for history and trends (errors here outlive Celigo's purge); use the live
   Celigo tools only for right-now state newer than the snapshot.

## 8. Honesty carried by results

- `as_of`: first caveat of every envelope, from the flow-map cursor; no cursor → "never completed a
  Celigo sync" and empty rows.
- `errors_checked_at`: per flow a column `errors checked = verified <ts> | not checked`; per
  integration the MIN rollup plus a caveat naming how many flows are unchecked.
- Run state per §4; a caveat whenever any row is `unknown`.
- Truncation: `truncated=True` and "Showing N of M" in `query`.
- Empty is not clean: an empty error table under a gated / unsynced / unchecked condition carries the
  reason; the model may not say "no errors" without a verified timestamp beside it.

## 9. PII in error messages

Same-tenant authorised readers already see messages on the pages. Chat adds two sinks: the persisted
transcript and model reasoning. Bound them: counts-only in `integrations` / `flows`; one sample
message per root-cause group (≤ 300 chars) in `flow_errors`, never the per-error list; audit records
`row_count` only; no log line formats a message. No regex scrubbing — a scrubbed message is not a
diagnosis. Profile rule 6 governs quoting.

## 10. Tests

Unit (mirror `test_data_sample.py`): invalid arg; no db; flag off; no connection; seeded tenant rows;
sandbox absent; second tenant sees nothing; **no `content` / `content_hash` key and no JS-looking
string anywhere in any envelope**; `errors_checked_at` NULL → `not checked` + caveat; run-state table.
Extraction parity per route. Tool-sync invariants (known-tool set, categories, `is_celigo_source`,
inventory with / without the connection, interception keeps `caveats`). Profile tests (loads,
triggers on the four names only, key phrases, denylist of schema words). Benchmark case: "Which Celigo
flows are failing right now, and what is the most common cause?" — pass = table emitted, `as_of`
relayed, no numbers in prose, at most one sample message quoted; run on staging with both model flags.

## 11. Decisions and rejected alternatives

- Four narrow tools **over** one `resource` parameter — a parameter is an invitation to ask for bodies.
- Mirror tables **over** the live API — history, purge-proof, tenant-scoped, no rate limits in chat.
- Gate on the flow-map connection **over** the MCP connector — the tenant that matters has no connector.
- Port stall logic to Python **over** exposing raw cron only — "stalled?" is the one fact the pages
  exist to surface; the tool must carry it, with `unknown` as the default.
- One sample message per group **over** full lists — bounds transcript exposure to one line per cause.
- Existing `celigo` flag **over** a chat-only flag — one switch for the whole surface.

## 12. Tasks (dependency order; one implementer each; briefs written at dispatch)

1. Extract `read_queries.py`; routes become adapters; byte-parity tests.
2. `run_state.py` (cron gaps + stall verdict) with table-driven tests shared with `schedule.ts` cases.
3. Four handlers in `mcp/tools/celigo_flow_map.py` + registry / governance / allow-list; envelope,
   gating, caveats; unit tests.
4. Chat wiring: inventory gating on the connection, categories, `is_celigo_source`, source-pin
   exclusion, pin hint, known-tools set, interception check.
5. `celigo.yaml` profile + tests.
6. Benchmark case + staging run with both model flags.
7. T2 gate (chunked) + codex; fix rounds; ship with the UI batch.
