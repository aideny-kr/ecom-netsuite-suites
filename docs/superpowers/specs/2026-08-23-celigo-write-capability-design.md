# Celigo Write Capability — Resolve & Retry Known Errors

**Date:** 2026-08-23
**Status:** Approved for planning — user decision 2026-08-23
**Supersedes:** non-goal N1 of `2026-08-17-celigo-connector-design.md` ("no Celigo writes")
**Depends on:** PR #202 (`feat/celigo-mcp-connector`) — read-only connector + HITL classification
**Tier:** T2 (mutates customer data · financial records · HITL invariant · MCP mutation writes)

---

## 1. Decision

The agent must be able to **resolve and retry known flow errors**. Calling Celigo's write
endpoints is in scope. This reverses non-goal N1 of the read-only spec, deliberately.

The concern raised and accepted: `retry` re-processes a record into destination systems.
For `NS - Create Customer Deposits (Paid Orders)` the import is `operation: "add"` on
`customerdeposit`, so a careless retry creates **real financial records in NetSuite**.
The user reaffirmed the requirement. This spec therefore makes retry *safe*, not optional.

## 2. What PR #202 already paid for

The expensive prerequisite is done and reviewed:

- `mutation_guard._MUTATION_TOOL_NAMES` now classifies **all 29 Celigo write verbs** as
  mutations, so any write routes through HITL confirmation instead of auto-executing.
- Two-layer enforcement exists and works (`build_external_tool_definitions` +
  `_execute_external_tool`), verified end-to-end against a real connector row.
- `celigo_tool_policy` is the single source of truth both layers read.

Enabling writes is therefore *loosening a policy that already exists*, not building one.

## 3. ⚠ The finding that drives this design

Probed live on 2026-08-23 via `get_flow_error_retry_data`, error
`d7cf327b300440b09918b09843bda01d` on flow `66738c3e9711fdc90cd89e69`:

```
stage:         page_processor_export      traceKey: 15822111
state:         "canceled"                 payment_state: "void"
payment_total: "0.0"                      deposit_amount: "100.0"
email:         "deleted_user_462621@user.deleted"
updated_at:    2026-07-30                 error occurred: 2026-08-17
```

**Retry replays a stored point-in-time snapshot, not current source data.**

That single record, retried today, would:

1. Push a NetSuite sales order for an order that is **canceled**.
2. Potentially create a **$100 customer deposit against a payment that is `void`**
   (`deposit_amount: "100.0"`, `payment_total: "0.0"`).
3. Re-inject **erased PII** — `deleted_user_462621@user.deleted` is a GDPR erasure
   tombstone. Retrying re-materialises that customer downstream.

None of these are hypothetical; that is one real error currently sitting in the queue.

**Therefore: a retry is not "re-run the thing that failed". It is "write a stale record
into a financial system." It must be treated as an irreversible external write.**

## 4. The three guards

### 4.1 Argument-level policy (tool-level allowlisting is too coarse)

`triage_flow_errors` takes an `action` spanning wildly different blast radius:

| action | Effect | Tier |
|---|---|---|
| `resolve` | marks the error handled — metadata only | **bookkeeping** |
| `tag` | applies tags | **bookkeeping** |
| `assign` | assigns to a user | **bookkeeping** |
| `retry` | **re-processes the record; writes to destinations** | **data-moving** |
| `retryAll` / `resolveAll` | the same, in bulk, with no per-record review | **prohibited** |

Allowing the *tool* allows every action. So `celigo_tool_policy` must gain
**argument-level** policy: an allowed tool may still be refused based on its arguments.
`retryAll` and `resolveAll` are never permitted from the agent — bulk blast radius with no
per-record judgement is exactly what HITL cannot meaningfully approve.

### 4.2 Freshness — re-validate before replaying

Because the snapshot is stale, the agent must re-read current source state before any
retry and refuse when reality has moved:

- source record `state` is `canceled` / `void` / refunded
- payment no longer captured (`payment_total` is 0 while `deposit_amount` > 0)
- PII erasure tombstone present (`*@user.deleted`)
- the snapshot is older than a configurable staleness ceiling (default: **7 days**)

A failed freshness check is a **refusal with a reason**, surfaced to the user — not a
silent skip.

### 4.3 Idempotency — never double-post

Per `.claude/rules/agent-graph.md` #10, every external write needs a work-derived
idempotency key and a side-effect log written *before* the call.

Before retrying a record whose flow creates a NetSuite record, check whether the target
already exists (e.g. a `customerdeposit` already linked to that `salesorder`). If it does,
the correct action is `resolve`, not `retry`. Partial success followed by retry is the
mechanism that produces duplicate deposits, and recon would then flag them.

## 5. "Known errors" — making the word rigorous

A **known error** is a catalog entry, not a human's impression. Each entry carries:

| Field | Purpose |
|---|---|
| `signature` | matcher on `code` + `source` + normalised `message` |
| `classification` | `transient` · `deterministic` · `unknown` |
| `disposition` | `retry_safe` · `resolve_only` · `needs_human` |
| `rationale` | why — shown to the approver |
| `evidence` | links to the occurrences that justified the entry |

**Classification decides retry eligibility, not just approval:**

- **transient** (lock contention, rate limit, upstream 5xx, timeout) → retry can succeed.
- **deterministic** (`script_error`, bad mapping, missing lookup value) → **retry is
  futile**. The live example, `TypeError: Cannot read properties of null (reading 'name')`
  in `pre_save_page_hook`, will re-fail identically every time. Retrying it 12 times is
  noise; the fix is in the tenant's script. These are `resolve_only`.
- **unknown** → `needs_human`. Default. Nothing is retryable until classified.

The catalog starts **empty and seeded only by explicit human curation.** No auto-learning
— per `project_recon_*` precedent, auto-learned dispositions were disabled for exactly this
class of reason.

## 6. Phasing

| Phase | Scope | Risk |
|---|---|---|
| **D1 — Bookkeeping** | `resolve`, `tag`, `assign` via argument-level policy. HITL. No data moves. | low |
| **D2 — Error catalog** | Catalog table + classification + curation UI. Read-only; makes "known" real. | low |
| **D3 — Guarded retry** | `retry` for `retry_safe` entries only, gated on freshness + idempotency, one record at a time, full HITL with a Celigo-shaped confirmation card. | **high** |

`retryAll` / `resolveAll` are out of scope permanently.

D1 delivers real value alone: clearing handled errors is most of the queue-hygiene win,
and it cannot move data.

## 7. What must be built

- **`celigo_tool_policy`** — argument-level policy: `(tool, args) -> allow | refuse(reason)`.
- **`WriteConfirmationCard`** — currently NetSuite-shaped. Must render a Celigo retry:
  which flow, which record, what will be written where, freshness verdict, idempotency
  verdict. **A confirmation card that does not show what will change is worse than none.**
- **`mutation_type` widening** — `run_flow`/`retry` are semantically "execute";
  `write_confirmation_service.py:39` types the Literal as
  `create|update|delete|upsert`. Widen it *and* the frontend card, or keep the documented
  `retry → "update"` mapping. Decide explicitly.
- **Error catalog** — table (FORCE-RLS per the `092` template), curation UI, audit trail.
- **Freshness re-read** — a source-system read before retry.
- **Idempotency pre-check** — target-exists lookup + side-effect log written before the call.
- **Token scope** — the Custom-scoped read-only service token must be widened to permit
  error triage, and *only* error triage. Least privilege still applies.

## 8. Risks

| Risk | Mitigation |
|---|---|
| **Duplicate financial records** | idempotency pre-check; side-effect log before the call; `add`-operation imports treated as irreversible |
| **Writing canceled/voided orders** | freshness re-read; refuse on `canceled`/`void`/refunded |
| **Re-injecting erased PII** | tombstone detection (`*@user.deleted`); hard refusal, never overridable |
| **Bulk blast radius** | `retryAll`/`resolveAll` permanently prohibited; one record per confirmation |
| **Futile retry loops** | deterministic errors are `resolve_only`; retry attempts bounded and counted in run state, not prompt |
| **Approval fatigue** | D1/D2 keep low-risk actions cheap so D3 approvals stay rare and meaningful |

## 9. Decisions (chose X over Y because ___)

- **Writes in scope** over read-only-forever — user decision 2026-08-23, reaffirmed after
  the double-post risk was raised.
- **Argument-level policy** over tool-level — `triage_flow_errors` spans bookkeeping and
  data-moving actions in one tool; tool-level allowlisting cannot separate them.
- **Curated catalog** over model judgement — "known" must be checkable; auto-learned
  dispositions were disabled elsewhere in this product for the same reason.
- **Freshness re-read** over trusting the snapshot — proven necessary by a live error whose
  snapshot is canceled, voided, and PII-erased.
- **`retryAll` prohibited** over HITL-gated — a bulk action is not meaningfully approvable.
- **D1 first** over shipping retry immediately — bookkeeping delivers queue hygiene with
  zero data movement, and buys time to build the catalog retry depends on.

## 10. DON'T

- **DON'T** expose `retryAll` or `resolveAll` to the agent, under any approval flow.
- **DON'T** retry an `unknown` or `deterministic` error.
- **DON'T** retry without a freshness re-read and an idempotency pre-check.
- **DON'T** auto-learn catalog dispositions from live sessions.
- **DON'T** widen the Celigo token beyond error triage.
