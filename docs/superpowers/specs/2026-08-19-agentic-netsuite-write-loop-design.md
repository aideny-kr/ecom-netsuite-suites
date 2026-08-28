# Agentic NetSuite Write Loop — Design

**Date:** 2026-08-19
**ClickUp:** [86bbgnwaj](https://app.clickup.com/t/86bbgnwaj) (subtask of [86bbgnw82](https://app.clickup.com/t/86bbgnw82))
**Tier:** T2 — mutates customer data, HITL invariant, financial posting.

## Problem

The agent composes NetSuite write payloads blind and single-shot, and the human who
approves them cannot see what they are approving.

Reproduced live on staging 2026-08-18 (session `e63adad0-5cc2-44e3-b70b-53d2843f6bcb`,
no records written). Asked to create a customer, the agent called `ns_createRecord` at
**step 0** with:

```json
{"recordType": "customer", "data": "{\"companyname\": \"test ai customer\"}"}
```

No subsidiary — required in this OneWorld account. `ns_getRecordTypeMetadata` and
`ns_getSubsidiaries` were both in its inventory (17 discovered tools on connector
`NetSuite MCP 6738075`) and it called neither. The identical payload shape already failed
in production on 2026-07-21, recorded in `audit_events`:

```
record.create.failed — HTTP 400 USER_ERROR:
"Error while accessing a resource. Please enter value(s) for: Primary Subsidiary."
```

Four defects compound:

1. **No pre-composition inspection.** `MAX_STEPS` is 40 — the loop always had room for a
   research turn. The agent was never told to take one. The active `netsuite_writes.yaml`
   profile teaches it to *narrate* a write ("list the fields you'll set and why"), not to
   *compose* one correctly.
2. **The confirmation card is blind.** `write_confirmation_service.py:107-108` reads
   `tool_input["body"]`; this MCP sends `data` as a JSON *string*, so `proposed_fields` is
   `{}` on every NetSuite write. The human approves a blank cheque and therefore cannot
   catch defect 1.
3. **No self-repair.** NetSuite returns a precise, machine-readable error naming the exact
   missing field. Nothing parses tool errors or retries.
4. **Failed writes strand the card.** `orchestrator.py:1773` reverts status to `pending`
   on failure — permanently, with no terminal state and no retry affordance.

## Scope

All NetSuite posting, not one record type. Verified: the chat mutation path is the **only**
NetSuite write path in the codebase — `ns_createRecord`/`ns_updateRecord` have no direct
callers, the model invokes them dynamically through `execute_tool_call`. Reconciliation
never posts (`api/v1/reconciliation.py:699`); `NetsuitePosting` is an ingested canonical
model for matching, not a write path.

Because posting includes transactions (journal entries, invoices, customer deposits), the
validator handles nested line-level requirements and two invariants that record metadata
cannot express.

## Decisions

| # | Decision | Rejected alternative | Because |
|---|---|---|---|
| 1 | **Validator + in-loop repair** at the dispatch chokepoint | Hard precondition on call ordering; guidance-only in the YAML profile | Guidance is the class of control that just failed — `read_only_mode` is prompt-only and the model wrote anyway. A hard precondition forces a round-trip on every write and blocks legitimate writes when metadata is unavailable. The validator is deterministic where it must be (nothing invalid reaches NetSuite or the card) and model-native where it should be (no hardcoded field lists). |
| 2 | On exhaustion the **card becomes a small form** — missing required fields render as inputs, with real option sets where we have them | Ask in chat; fail clean with the reason | Keeps the human as the authority on values the agent cannot infer, and turns a dead end into one click. Asking in chat would reuse the Plan Mode clarify card, which is separately reported as freezing after selection. |
| 3 | **Server-declared editable slots.** The server stores which fields are editable and their allowed values; the client may submit values only for declared slots, validated against the declared type/allowlist, then merged server-side and the HMAC re-minted | Round-trip through the model; client returns the full payload | A manipulated client can fill declared blanks with allowed values and nothing else — it can never touch a field the agent composed. Client-authored payloads would make the browser the author of ERP writes. |
| 4 | **Metadata unavailable → skip validation, mark the card `unvalidated`, show the full payload** | Fail closed and block the write | The security-critical fail-closed belongs on *rendering* — never show an empty-but-approvable card. Once the human sees the full payload they are the control. Blocking every write on a metadata timeout trades a real outage for a theoretical gain. |
| 5 | Validate **header + line-level required fields**, plus exactly two invariants: **accounting period is open** and **journal entries balance** | Full posting-invariant layer now; lines only with invariants deferred | The two chosen invariants are cheap to check and catastrophic to miss. Amount provenance, approval envelopes and autonomy budgets belong to `docs/superpowers/plans/2026-08-02-autonomous-accounting-ops-program.md`, which already scopes them; folding them in here makes nothing shippable. |
| 6 | **No automatic retry after approval.** A repaired payload requires fresh confirmation with a new token | Auto-retry the repaired payload | The human approved a specific payload. A repaired one is a different payload. Follows directly from decision 3. |

## Architecture

One new seam at the dispatch chokepoint, four small units:

| Unit | Purpose | Depends on |
|---|---|---|
| `payload_normalizer` | Turn any tool_input shape (`body` dict, `data` JSON-string, future schemas) into one canonical dict | — |
| `record_metadata_service` | Fetch + cache `ns_getRecordTypeMetadata` per (tenant, connector, record_type); resolve enumerable option sets (`ns_getSubsidiaries`, …) for editable slots | MCP client |
| `write_validator` | `(record_type, payload, metadata) → {ok, missing_required[], invalid[], editable_slots}`; walks header and lines; applies the two posting invariants | the two above |
| `repair_loop` | Bounded recompose cycle inside the existing mutation intercept | validator |

`payload_normalizer` is the **same helper that fixes ClickUp 86bbgnw8h** — this design
subsumes that ticket rather than duplicating it. One parse site, so a new MCP tool schema
cannot silently produce an empty card again.

## Data flow

```
model proposes create/update
   │
   ▼
mutation intercept (base_agent.py:1237)
   │  normalize payload ──► validate vs cached metadata
   │                              │
   │              ┌───────────────┴───────────────┐
   │           invalid                          valid
   │              │                               │
   │      structured error to model               │
   │      "missing required: subsidiary"          │
   │              │                               │
   │      recompose (max 2, stall-detected)       │
   │              │                               │
   │      still invalid ──► declare editable      │
   │                        slots server-side     │
   │                              └───────┬───────┘
   │                                      ▼
   │                          CONFIRMATION CARD → human
   │                                      │
   │                             approve + slot values
   │                                      ▼
   │                    validate slots vs declared allowlist
   │                    merge → re-mint HMAC → execute
   │                                      │
   │                       ┌──────────────┴──────────────┐
   │                    success                    NetSuite error
   │                       │                             │
   │                  status=approved            status=failed (terminal)
   │                                             error shown, no auto-retry
```

The human never sees a payload that would fail validation. That is the point.

## Termination

Repair state lives in run state, not the prompt.

- **Cap:** 2 repair attempts.
- **Stall detection:** fingerprint the missing-field set. An identical set twice means
  recomposing will not help — exit immediately rather than burning the budget.
- **Exit reason enum:** `done | budget | stall | error`. Never a bare boolean.
- Sits **inside** the existing `MAX_STEPS = 40` turn budget, not alongside it.

## Error handling

| Condition | Behaviour |
|---|---|
| Metadata fetch fails / record type has no metadata endpoint | Skip **field** validation, mark card `unvalidated`, render the full payload (decision 4). The two posting invariants still run — neither period-open nor debits-equal-credits depends on record-type metadata, so a metadata outage must not silently disable them |
| Payload unparseable by the normalizer | **Fail closed** — block the write, explicit error. Never render an empty-but-approvable card |
| Validation fails after 2 attempts | Card renders with declared editable slots (decision 2) |
| Client submits an undeclared field on approve | Reject |
| Client submits an out-of-allowlist value for a declared slot | Reject |
| NetSuite rejects after approval | Terminal `failed` status carrying the NetSuite error; no auto-retry |
| Record type on `_BLOCKED_RECORD_TYPES` | Unchanged — blocked before any of this runs |

## Testing

Every test written failing first and proven red against current code.

- Normalizer extracts fields from the real `{"recordType","data":"<json string>"}` shape
- Normalizer fails closed on an unparseable payload
- Validator flags missing `subsidiary` on a customer create
- Validator walks **line-level** required fields on a transaction record
- Validator rejects a journal entry whose debits ≠ credits
- Validator rejects a posting into a closed accounting period
- Repair loop stops at 2 attempts; an identical missing-set exits `stall` on attempt 2
- Approve rejects a value for an **undeclared** field
- Approve rejects an **out-of-allowlist** value for a declared slot
- Token is re-minted after merge; the pre-merge token no longer validates
- NetSuite error leaves status `failed`, never `pending`
- Metadata-unavailable path renders `unvalidated` with the full payload, not an empty card

Plus one **executing** end-to-end probe against a real NetSuite sandbox. Per the
verification standard an AST/grep check does not count — the probe must import, call, and
run. This is **blocked on ClickUp 86bbgnwbf**: all three of the tenant's `netsuite_mcp`
connectors, including the one labelled "Staging", point at production account `6738075`,
so there is currently nowhere safe to run it.

## Explicitly not building

- Editing arbitrary fields — only missing required fields become editable slots
- Automatic retry after approval
- `read_only_mode` dispatch enforcement — separate ticket (86bbgnwb6)
- Changes to `_BLOCKED_RECORD_TYPES`
- Amount provenance, approval envelopes, posting budgets — deferred to the
  autonomous-accounting-ops program

## Open risk

`_BLOCKED_RECORD_TYPES` blocks `accountingPeriod` but not `journalEntry`, `invoice` or
`customerDeposit`. The agent can propose a journal entry today with HITL as the only
defence. Decision 5's two invariants narrow this materially; the full envelope belongs to
the posting program. Flagged here so it is not mistaken for closed.
