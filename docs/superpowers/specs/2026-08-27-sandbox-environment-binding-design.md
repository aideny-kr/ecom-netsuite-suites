# Sandbox environment binding — design

**Status:** draft, not started. Blocked on a provisioned NetSuite sandbox connector (ClickUp `86bbgnwbf`).

**Goal:** a user can say "try this in sandbox first", and the system *guarantees* the write lands
in the environment they chose — not merely encourages it.

---

## Why this is a product feature, not just a test fixture

Today every NetSuite connector for every tenant points at production. There is no way to try an
ERP write without doing an ERP write. That is a trust tax on the whole write surface: the first
time a customer lets an agent create a record, the stakes are real money and real audit history.

"Try it in sandbox, then run it for real" is the affordance that makes the write loop adoptable.
It is also what makes it *testable* — our own approve-path testing currently creates real records
in Framework's production account 6738075.

## What already exists (verified, with citations)

Considerably more than expected.

- **Coexistence is solved.** `api/v1/netsuite_auth.py` carries logic hardened over five review
  rounds (PR #194) specifically so a sandbox row and a production row cannot overwrite each other
  during authorize/re-authorize. It already guarantees that connecting a sandbox does not silently
  repoint production.
- **Tools are already namespaced per connector** — `ext__<connector_id>__ns_createRecord`
  (`tools.py:126-140`). Two NetSuite connectors give the agent two complete, independently
  addressed toolsets. No new plumbing.
- **The dispatcher is a real choke point.** `_execute_external_tool` (`tools.py:429`) loads the
  connector row before dispatching and already refuses there for two policies (Celigo read-only,
  the `celigo` feature flag). Its own comment states the doctrine: *"Refuse here regardless of how
  we got called."*
- **`human_approved` (2026-08-27) proved the pattern.** The HITL guard now lives at the dispatcher
  and defaults to denied. Environment binding is the same shape with a different predicate.
- **Sandbox is already first-class for SuiteScript deploys** — `sandbox_id` with validator pattern
  `(?i).*(sb|sandbox|tstdrv).*` (`schemas/workspace.py:218-237`). Useful precedent, not a
  sufficient one (see Decision 2).

## What does not exist

`get_active_connectors_for_tenant` (`mcp_connector_service.py:160`) returns **all** active
connectors. Enable a sandbox alongside production today and the agent sees both, choosing between
them by opaque UUID with nothing indicating which one is real money. `build_external_tool_definitions`
puts `provider` in the tool description but never the label or account id (`tools.py:188`), so
neither the model nor a human reading `tool_calls_log` can tell them apart by inspection.

---

## Decisions

### 1. Environment is enforced, not hinted

`session.source_pin` is deliberately advisory — *"a prompt hint; the model decides whether to
follow; no routing override."* That is right for choosing a data source and **wrong** here. A hint
that is followed 95% of the time is, on an irreversible ERP write, a 5% chance of writing to
production when the user said sandbox.

Enforcement lives at `_execute_external_tool`, beside the HITL guard, for the same reason: a caller
added tomorrow must be refused, not trusted.

`source_pin` is additionally unusable as a model here — its column was **dropped** in migration 067
and it is now a transient per-turn attribute (`models/chat.py:22-23`). An environment choice must
be durable.

### 2. Environment is DERIVED server-side, never operator-asserted

`metadata_json['account_id']` is free text supplied during OAuth setup; nothing validates it
(`api/v1/mcp_connectors.py:82-119`). So a column an operator sets to `"sandbox"` can point at
production — the exact failure the feature exists to prevent.

Therefore: derive at connector creation from the account id (`_SB*`/`_RP*` suffix ⇒ sandbox-class,
else production), **store the derivation**, and **re-derive at dispatch** from the live row. Refuse
on disagreement rather than trusting either. The stored value is a cache, not the authority.

### 3. The column is NOT NULL with an explicit backfill

Every existing `netsuite_mcp` row is production (verified: all three Framework connectors are
account 6738075). Backfill `production` explicitly. A nullable column makes the feature fail *open*
on day one, and "unknown" would be the most common value in the table.

Belt and braces: the dispatcher refuses any classified mutation whose connector environment is
missing or unrecognised.

### 4. Environment is signed into the confirmation envelope

The card's `tool_input` is HMAC-signed (`write_confirmation_service.py:95-132`). An **unsigned**
environment field is a label, not a guarantee — it could disagree with what executes.

At approve, re-derive from the live connector row and refuse unless it equals **both** the
environment in the signed envelope **and** the session's current choice. A card minted under
"sandbox" and approved after the session switched to production is void, not reinterpreted.

### 5. The switch is a UI control, never a chat tool

If the model can set the environment, the guarantee degrades back to `source_pin`. The switch is a
user-authenticated REST endpoint driven by an explicit control. The model may *suggest* switching;
only a human performs it.

---

## Threat model

From the adversarial pass. Each is a way a write reaches the wrong environment despite the design.

| # | Hole | Severity | Mitigation |
|---|---|---|---|
| 1 | Unlisted write tool skips classification entirely | critical | **Fixed 2026-08-27** — NetSuite tools are now allow-listed (`_NETSUITE_READ_ONLY_TOOLS`); unknown ⇒ treated as a write |
| 2 | Sessionless callers (workers, benchmarks) have no choice to bind to | critical | Environment is a **required argument** of mutation dispatch, not a lookup that can miss. No session ⇒ no mutation |
| 3 | Unguarded write path executes with no confirmation at all | critical | **Fixed 2026-08-27** — HITL guard moved to the dispatcher, `human_approved` defaults False |
| 4 | Mint/approve divergence — card outlives the choice | critical | Re-derive at approve; require agreement across envelope, live row, and session choice |
| 5 | Operator-asserted environment points at the wrong account | critical | Decision 2 — derive server-side, re-derive at dispatch |
| 6 | Existing rows have no environment | major | Decision 3 — NOT NULL + explicit backfill |
| 7 | RESTlet / REST-Connection writes bypass `execute_tool_call` entirely | major | **Out of scope, stated honestly.** The guarantee covers the MCP surface only until the RESTlet write entry points take a required environment attestation |
| 8 | Post-write link resolves the tenant's account, not the connector's | major | **Fixed 2026-08-27** — resolved from the executing connector |
| 9 | Reads still cross environments and poison a "sandbox test" | minor | When a session carries an explicit choice, apply the refusal to reads on the other environment too |

Three of nine were fixable immediately and are already fixed; they were bugs in the current system,
not in the proposed one.

## Scope boundary

This does **not** make writes safe. It makes them *land where you said*. HITL approval remains the
thing that makes them correct, and the two are independent guarantees.

## Open questions

- Per-session or per-tenant default? Per-session is more useful and harder to get right.
- What happens to a sandbox connector after a NetSuite sandbox refresh wipes its integration
  record? The connector goes `error`; the environment binding should survive so the tenant does not
  silently fall back to production.
- Does the recon/posting surface need the same binding, or is chat sufficient for v1?
