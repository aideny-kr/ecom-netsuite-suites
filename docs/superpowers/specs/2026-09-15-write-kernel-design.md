# One write kernel — design (G3)

**Status:** draft for review, 2026-09-15. Branch `feat/g3-write-kernel`, stacked on `feat/g1-write-contracts` (PR #264).
**Inputs:** the Codex handoff (`~/.codex/work-checkpoints/claude-accounting-write-design-handoff.md`), the prompts plan
(`docs/superpowers/plans/2026-09-15-self-correcting-write-agent-codex-prompts.md`, Prompts 1, 3, 5, 7), PR #262's live
trace (CM 15778068 on order R013617167), PR #218's spec and probe evidence, and the code named in every section below.
**Goal G3 of seven:** G1 contracts and registries (PR #264) · G2 digest and kill switch (`feat/g2-ops-digest-kill-switch`) ·
**G3 one write kernel (this)** · G4 model-authored plans · G5 durable groups and the 54 cases · G6 outcome self-correction ·
G7 memory and measurement.

## 1. The problem, in the operator's words and in the code

The operator's complaint: *"the agent keeps declining write access to systems, and if it fails it stops."* Claude with the
bare MCP retries until the job is done; ours refuses, or fails once and hands the failure back.

Both halves are real and both are architectural, not prompt tuning.

**It declines** because a write can be refused at four places that share no vocabulary and surface no reason:
the prompt-level `read_only_mode` gate (`unified_agent.py:744`), the tool-advertising filter, `evaluate_tool_call` inside
`authorize_accounting_write` (`accounting_group.py:351`), and the dispatcher's `human_approved` default
(`chat/tools.py:531`). Each refusal is correct on its own. None of them tells the human *which* gate refused or what
would satisfy it, so from the chat it looks like the agent will not write.

**It stops** because the chat write path has one repair rule, `may_enter_repair_loop(outcome) == (outcome == "failed")`
(`write_outcome.py:76`), and no reconcile step. A NetSuite rejection re-enters the model with raw error text; a timeout
is classified `indeterminate` and the turn ends. That rule was the correct fix for the duplicate customer 5264348
(2026-08-27): a blind resubmit after a timeout is how a ledger gets a duplicate. But "never resubmit blind" became
"never do anything", because there is no read-only reconciliation to establish whether the write landed.

Underneath both symptoms there are four write substrates and three idempotency mechanisms on the Framework line:

| substrate | claim | send permit | outcome record | recovery |
|---|---|---|---|---|
| generic chat card (`WriteConfirmationCard`) | CAS on `structured_output.status` (`_cas_claim_write_confirmation`) | none | `classify_write_outcome` → success / failed / indeterminate, on the card | none |
| accounting chat card | same CAS + `execution_claim` JSON + `previous_execution` scan of other cards + `accounting_write_slot` advisory locks | none (the MCP call is the permit) | `accounting_verification` from `verify_after`, on the card | `accounting_recovery` scans cards, 3 attempts, read-only |
| transaction_ops scheduled | `TransactionOperation` row, UNIQUE (tenant, work_key), in-flight entity index | `reserve_operation_dispatch` one-use permit, committed before the send | `status` CHECK executing / verified / unknown / failed | `recovery` reads; `complete_operation` refuses to move `unknown` without `reconciled: True` |
| durable group dispatch (`accounting_dispatch`) | DB outbox, child reservation audit | delegates to the accounting card path | per-child card | interrupted reservations inspected, never resent |

Plus PR #218's `write_side_effects` table (states attempted / written / rejected, `externalId = ss-idem-<sha24>`) and the
plan's proposed `posting_log.claim`, neither merged. Every new session so far has added a mechanism instead of removing
one. G3 is the removal.

## 2. Decision: the transaction_ops ledger is the kernel

`transaction_ops_operations` + `state_service.claim_approved_operation` / `reserve_operation_dispatch` /
`complete_operation` become the only way any surface sends a mutation to NetSuite or Celigo. Chosen over the
alternatives because it is the only substrate that already has all four properties the handoff asks for:

- a **work-derived key** with a UNIQUE constraint (`work_key`) and an **in-flight collision index** (`entity_key`
  WHERE status IN executing/unknown), so a duplicate is refused by the database, not by a scan;
- a **one-use send permit** (`dispatch_reserved`) committed before the call, so a crash after it permits only reads
  and no job retry or reconstructed claim can send again (`state_service.py:1032-1105`);
- a **CHECK-constrained outcome** that refuses to move `unknown` without reconciliation evidence
  (`complete_operation`, `state_service.py:986-1029`);
- a **SIGKILL drill in CI** (`scripts/uat/transaction_ops_crash_drill.py`), which is the only recovery code on the line
  that has ever run (`agent-graph.md` #12).

What it lacks is generality: it is bolted to `TransactionProposal` (FK, `proposal.action` decides the provider, config
`action_mode` and the celigo/reconciliation feature flags gate the permit). Section 4 generalizes those into an
approval-source hook. The chat card's `accounting_execution` JSON, `previous_execution`, `accounting_recovery`'s card
scan and `#218`'s side-effect table are retired in favour of ledger rows. `_cas_claim_write_confirmation` stays, but
only as the card's *acceptance* step; it no longer implies that a send may follow.

Rejected: a new `posting_log` table (a fourth ledger), and keeping `#218`'s `write_side_effects` next to the ledger
(two rows per write that can disagree, which is exactly the "card ignores the settle verdict" defect #218 already has).

## 3. Outcomes

The ledger `status` becomes:

| status | meaning | terminal | what may happen next |
|---|---|---|---|
| `executing` | claimed; `result_json.dispatch_reserved` says whether a permit was consumed | no | preflight, permit, one send |
| `rejected_before_effect` | ended with **proof of no effect**: preconditions changed before a permit, the provider refused atomically, budget or kill switch stopped it. `code` names which | yes | a **lineage retry**: a new row, `retry_of_operation_id` set, same `base_work_key`, its own approval |
| `committed_unverified` | a receipt identified the record as saved (identity and work key echoed) but independent readback has not yet proved the approved end state | no | `verify` by read, bounded; never a send |
| `unknown` | a permit was consumed and there is no trustworthy receipt (timeout, transport error, unparseable body) | no | `reconcile` by read, bounded; never a send |
| `verified` | independent readback proved the approved end state | yes | settlement / recheck as today |
| `needs_review` | readback contradicts the approval, or reconciliation is exhausted; a human decides | yes for automation | operator action from the digest |

This is the handoff's five outcomes plus `needs_review`, which is the state `verify_after` already returns as
`{"status": "needs_review"}` and which the ledger had no column value for. Today's `failed` is renamed to
`rejected_before_effect`: every `failed` row the executor writes is pre-effect by construction (an exception after
`dispatch_reserved` always yields `unknown`, `executor.py:241-251`; `provider_rejected_without_save` is the guard
RESTlet's atomic refusal). The migration rewrites existing rows.

The repair rule is now a function of the status, and it is code:

- from `rejected_before_effect`: a retry is allowed **only** as a lineage row, and only when the failure class permits
  (section 6). The payload may change; the business identity may not (`base_work_key` is inherited).
- from `unknown`: reads only. `reconcile` may move it to `committed_unverified` (record present with our identity),
  `rejected_before_effect` (provably absent or unchanged: an update's before-snapshot still matches, a create's
  `externalId` is absent), or leave it `unknown` for the digest.
- from `committed_unverified`: readback only. `verified` or `needs_review`.
- from `needs_review` and a stale `unknown`: a person, via the G2 digest.

`termination_reason` keeps the enum the rules require (`done | budget | stall | error | blocked`):
verified → `done`; rejected_before_effect → `error` (or `budget` / `blocked:kill_switch` / `blocked:read_only_mode`
when that is the cause); unknown and committed_unverified → `stall`; needs_review → `blocked:needs_review`.

## 4. Approval sources and the claim

`claim_approved_operation` does seven things today: tenant row lock, work_key uniqueness, in-flight entity check,
approval validity (status approved, decider still a permitted human, config enabled and in `propose_actions`,
evidence fingerprint, `valid_until`), insert the `executing` row, audit, commit. The first three and the last three are
generic. Only "approval validity" is source-specific. So:

```python
class ApprovalSource(Protocol):
    kind: str                       # "transaction_proposal" | "chat_confirmation"
    async def load(db, tenant_id, approval_id, *, now) -> ApprovedIntent
    async def still_valid(db, tenant_id, intent, *, now) -> None      # raises StateError(code) at claim time
    async def still_authorized(db, tenant_id, intent, *, now) -> None # raises StateError(code) at permit time
```

`ApprovedIntent` is the frozen record the kernel and the adapter work from: `approval_kind`, `approval_id`,
`approved_by`, `approved_at`, `valid_until`, `surface` (chat / scheduled / group / backfill), `treatment` (a G1
registry row or `None` for a generic write), `record_type`, `target_record_id` (None for creates), `semantic_payload`
(what the human saw: field → value with amounts as Decimal strings and references as `(label, id)`), `evidence_digest`,
and the adapter name.

Two sources ship in G3:

- **`TransactionProposalSource`** wraps the existing checks unchanged (config enabled, `action_mode`, fingerprint,
  `valid_until`, decider re-check, celigo/reconciliation flags). The scheduled path's behaviour does not change.
- **`ChatConfirmationSource`** wraps a `WriteConfirmationCard`: `validate_and_extract_confirmation` (HMAC, session
  binding), `status == "executing"` after the card's CAS, the approver is still a permitted human
  (`_authorize(..., fresh=True)`), `evaluate_tool_call(policy, tool, input)["allowed"]`, **and `policy.read_only_mode`
  is False**. Read-only mode is enforced here, at the claim, for every surface, with the refusal code
  `blocked:read_only_mode` written to the row and rendered on the card. Prompt-level gating stays as advice to the
  model; it stops being the enforcement.

The card stores the ledger row id (`structured_output.operation_id`) and renders its outcome from the ledger. The
`accounting_execution` JSON, `execution_claim`, `previous_execution` and the `accounting_correction.approval_claimed`
audit are removed; the ledger's `operation.attempt` audit carries the same fields. Group dispatch
(`accounting_dispatch.invoke_child`) passes each child through the same source; the child's `surface` is `group`.

Claim rules that were implicit become explicit and shared:

- `work_key` is the **business identity**, never proposal- or session-scoped (Prompt 5): accounting treatments use
  `resolution_plan.operation_identity(proposal)`; a generic create uses
  `business_digest({account, subsidiary, record_type, natural_key})` with the natural key from the semantic payload
  (order reference / tranid / externalId when the user supplied one / the required-field registry's identity fields);
  a generic update uses `business_digest({account, record_type, record_id, semantic_payload})`.
- `entity_key` is the collision scope: `{account, subsidiary, record_type, document}` where `document` is G1's
  `collision_key` for treatments and the target record for generic writes. The partial unique index gains
  `committed_unverified`. This replaces `accounting_write_slot`'s per-record advisory lock; the three-slot account cap
  stays as it is until G5 replaces it with the tier-sized permit (Prompt 7).
- A `rejected_before_effect` row is the only kind that permits a lineage retry, at most one per base key per class
  budget (section 6), following the shape `create_proposal` already uses
  (`key = business_digest({"base_work": base, "retry_of_operation": id})`, `state_service.py:815-823`).

## 5. Adapters

Each of the six per-kind modules, the guard RESTlet transport and the Celigo transport do the same eight things in
different orders with different names. The protocol names them once:

```python
class WriteAdapter(Protocol):
    name: str
    provider: str  # netsuite_mcp | netsuite_restlet | netsuite_native | celigo
    async def preflight(db, tenant_id, intent, budget) -> Preflight
        # fresh reads under the operation budget; raises PreconditionChanged(code)
    def wire(intent, preflight) -> Wire
        # the exact bytes to send + payload_fingerprint; exact-money conversion happens
        # here and only here (credit_api_correction.typed_fields), evidence stays Decimal
    async def send(db, tenant_id, intent, wire, permit) -> Receipt
        # exactly one provider call; returns accepted(identity) | rejected(details) | indeterminate
    def no_effect_proof(intent, rejection, preflight) -> bool
        # can this rejection be trusted as "nothing changed"? (accounting_retry.rejected_credit_unchanged)
    async def verify(db, tenant_id, intent, receipt, budget) -> Verdict
        # independent readback: verified | contradicted(reason) | unavailable
    async def reconcile(db, tenant_id, intent, budget) -> Existence
        # read-only: present(identity) | absent | unknown
```

`Treatment` (G1) gains an `adapter` field. Adapters in G3, in order of retirement risk:

1. `CreditApiAdapter` from `credit_api_correction.py`: `prepare`/`validate_approved` → preflight, `typed_fields` +
   `schema_contract` → wire, the signed `ns_updateRecord` → send, `verify_after` → verify, `fresh()` + the
   before-snapshot compare → reconcile, `rejected_credit_unchanged` → no_effect_proof. This is the working nucleus; the
   adapter is a re-arrangement of its functions, not new behaviour.
2. `GuardRestletAdapter` from `netsuite_transport.dispatch_netsuite_operation` (correct_amounts, sync_missing_order)
   and `CeligoAdapter` from `celigo_actions`. The executor's `verify_outcome` becomes their `verify`.
3. `NativeAmendmentAdapter` from `native_accounting_dispatch.execute` + `native_accounting_service`.
4. `SalesCreditAdapter`, `InvoiceDiscountAdapter`, `SalesOrderAlignmentAdapter`, `InvoiceTaxAdapter` from the modules
   `tax_correction.validate_approved` dispatches to today (`tax_correction.py:376-440`). That dispatcher is deleted; the
   registry names the adapter.
5. `GenericRecordAdapter` for `ns_createRecord` / `ns_updateRecord` without a treatment: preflight =
   `write_validation.validate_mutation` + `ns_getRecord` before-snapshot for updates; wire = the approved semantic
   payload rendered to the connector schema, with `#218`'s `externalId` stamping for creates (`ss-idem-<sha24>`, never
   overwriting a user-supplied value); verify = readback of the semantic fields; reconcile = `ns_getRecord` by id for
   updates, SuiteQL `WHERE LOWER(externalid) = :key` for creates; no_effect_proof = before-snapshot unchanged (updates)
   or `externalId` absent (creates). "This entity already exists" on a create with our key is classified
   `duplicate:posted` and moves the row to `committed_unverified`, never to a resubmit.

Adapters do no ledger writes. The kernel owns claim, permit, outcome and audit; an adapter that wants to send must be
handed a permit it cannot mint.

## 6. The kernel loop and the repair policy

```
execute(intent, adapter):
  claim                      → executing            (StateError → refused with code; nothing sent)
  preflight (budgeted)       → PreconditionChanged  → rejected_before_effect(precondition_changed:<code>)
  wire                       → payload_fingerprint
  permit (budgeted)          → StateError           → rejected_before_effect(<code>)   # kill switch, read-only, budget, authority
  send (exactly once)
    rejected + proof         → rejected_before_effect(provider_rejected:<class>)
    rejected, no proof       → unknown(provider_rejected_unproven)
    accepted(identity ok)    → committed_unverified(receipt)
    indeterminate            → unknown(transport_indeterminate)
  verify (budgeted)          → verified | needs_review(<reason>) | stays committed_unverified
  any exception              → unknown if a permit was consumed else rejected_before_effect(kernel_error)
```

This is `executor.execute_proposal` with the per-action branches moved into adapters and two more exits. The G2 kill
switch (`TRANSACTION_OPS_DISPATCH_ENABLED`, checked inside `reserve_operation_dispatch`) therefore covers every
surface the moment the chat path is a kernel client, which is the gap G2 documented.

**Repair policy** (`write_kernel.repair`) decides what happens after a terminal or stalled row. It runs in code, it
persists its counters in the ledger (rows sharing `base_work_key`, grouped by failure class), and it never asks the
model whether to retry. The classes come from Prompt 1's `classify_write_failure` (`o:errorCode` first, HTTP status
second, the structural `INDETERMINATE_KEY` for transport; `unknown` is the fall-through, never a guess):

| class | from status | action | budget per class (operator default) |
|---|---|---|---|
| `transport_indeterminate` | unknown | `reconcile`; present → committed_unverified → verify; absent → one identical lineage retry (same key, same payload); unknown → digest | 1 reconcile + 1 retry |
| `auth_expired` | rejected_before_effect | refresh the token once, lineage retry with the identical payload | 1 |
| `rate_limited` | rejected_before_effect | honour Retry-After, else 1 s / 2 s / 4 s with jitter; lineage retry; never widen concurrency | 3 |
| `invalid_field`, `missing_required`, `invalid_reference` | rejected_before_effect | recompose: G3 ships the **cosmetic** transform list (field-id casing, date format, reference by id instead of label, sublist shape). A cosmetic delta resubmits under the same approval; a semantic delta opens a new card with the field diff and invalidates the old token (Prompt 3). Metadata-grounded recomposition is G4 | 3, reset when the class changes |
| `permission`, `business_rule`, `unknown` | rejected_before_effect | `needs_review`; the card says which gate or rule, and what a person can change | 0 |
| `duplicate:in_flight`, `duplicate:posted` | claim refused | show the existing row (status, approver, internal id); no send | 0 |

The **cosmetic / semantic** split needs the semantic payload to be what the HMAC token binds. Today the token binds
`tool_input` bytes (`mint_confirmation_token`). G3 adds `semantic_payload` to `WriteConfirmationPayload`, signs it, and
keeps signing the wire bytes too; a resubmit is cosmetic iff the semantic payload is byte-identical and the transform
applied is on the closed list. The environment and account are derived server-side and signed into the envelope (the
2026-08-27 sandbox-binding decision); a repair can never move them.

Every repair step writes one audit row (`operation.repair`) with correlation id, attempt, class, delta kind and the
field diff, and every path ends with a `termination_reason` on the row. Four hard stops hold for every lineage:
max attempts (the class budgets above), no-progress (an identical failure fingerprint twice in a row ends the lineage
with `stall`, `write_repair_bound.compute_failure_fingerprint` is reused), budget ceiling (`max_api_calls` and
`deadline_at` on the row), and no-new-knowledge (a reconcile that returns the same `unknown` twice stops).

## 7. What the two agent paths look like afterwards

**Generic chat write.** The model calls `ns_updateRecord`; `classify_mutation` intercepts; the card is built with the
semantic payload; the human approves; the approve branch in `orchestrator.py` (today ~220 lines from the CAS to the
outcome message) becomes: CAS-accept the card → `write_kernel.execute(ChatConfirmationSource.load(card),
GenericRecordAdapter)` → render the row. A 429 or a timeout is handled inside the kernel; the human sees "sent, checking
the record" and then the verified state, not an error and a dead turn. A rejection with a cosmetic fix resubmits; a
semantic fix produces a new card that shows exactly which two fields changed and why.

**Accounting correction.** Same shape with the treatment's adapter. `tax_correction.validate_approved`,
`execution_claim`, `previous_execution`, `accounting_recovery.candidates` and `accounting_write_slot`'s record lock go
away; `accounting_recheck` queues off `verified` exactly as it does today. The group path calls the kernel per child.

**Scheduled transaction_ops.** `execute_proposal` shrinks to `execute(TransactionProposalSource.load(id), adapter)`.
Behaviour is unchanged; the crash drill must pass unmodified before anything else in G3 merges.

What "declines" looks like afterwards: a refusal is a ledger row or a claim error with a code, and the card renders the
code as a sentence a person can act on (`blocked:read_only_mode` → "This workspace is in read-only mode; an admin can
change that under Policies"). There is no path where the agent silently omits the tool.

## 8. Schema change (alembic, T2)

One migration on `transaction_ops_operations`, re-parented onto the applied head at implementation time (never a merge
revision; `feedback_orphan_alembic_migrations`):

- `proposal_id` nullable; add `approval_kind TEXT NOT NULL` (CHECK in `transaction_proposal`, `chat_confirmation`),
  `approval_id UUID NOT NULL`, `surface TEXT NOT NULL` (CHECK in `chat`, `scheduled`, `group`, `backfill`),
  `provider TEXT NOT NULL`, `adapter TEXT NOT NULL`, `base_work_key VARCHAR(64) NOT NULL`,
  `retry_of_operation_id UUID NULL` (self FK).
- UNIQUE (tenant_id, approval_kind, approval_id) replaces UNIQUE (tenant_id, proposal_id). Backfill:
  `approval_kind = 'transaction_proposal'`, `approval_id = proposal_id`, `surface = 'scheduled'`, `provider` from
  `result_json.provider` or the proposal action, `base_work_key = work_key`.
- `ck_tx_operation_status` becomes `status IN ('executing','rejected_before_effect','committed_unverified','unknown',
  'verified','needs_review')`; `UPDATE ... SET status = 'rejected_before_effect' WHERE status = 'failed'`.
- The in-flight partial unique index on `entity_key` covers `('executing','unknown','committed_unverified')`.
- `downgrade` restores the four-value CHECK and maps the two new statuses back to `failed` / `unknown`; the
  `TransactionProposalSource` never depends on the new columns, so a downgraded scheduled path keeps working.
- The guard trigger carries the receipt rule: a `committed_unverified` row can never become `rejected_before_effect`
  or `unknown`, and becomes `verified` only with a `verification` object in `result_json`. The service's checks are
  the readable copy; the trigger is the one no writer can route around.
- Two compatibility measures are temporary and one follow-up migration removes both after G3.4 lands: `failed` stays
  in the status CHECK, and a BEFORE INSERT defaults trigger fills `approval_id` / `base_work_key` for writers that
  predate the columns (a rolling deploy inserts from the old image while the new schema is live).

No change to `chat_messages`; the card keeps `structured_output` and gains `operation_id`. `#218`'s migration 097
(`write_side_effects`) is not applied; the PR is closed with a comment pointing here once G3.3 merges.

## 9. Slices and order

Each slice is one PR, T2 (mutates customer data, alembic, MCP mutation writes), gated with
`Workflow code-review-multiangle` pinned to the branch, base `origin/main`, `codex_used` checked.

1. **G3.1 kernel** — `write_kernel.py` (`execute`, `repair`), `ApprovalSource` + `TransactionProposalSource`,
   `WriteAdapter` + `GuardRestletAdapter` + `CeligoAdapter`, the migration, the status taxonomy, `executor.py` reduced to
   a client. Acceptance: the existing crash drill passes unchanged against `ecom_netsuite_test`; the transaction_ops
   suites pass with a baseline diff against main; `status` values outside the CHECK are refused by the database
   (a test that asserts the IntegrityError).
2. **G3.2 accounting cards** — `ChatConfirmationSource`, `CreditApiAdapter` first (the live CM proposal through the
   kernel with a stub transport is the fixture), then the other five; the approve branch becomes a kernel call;
   `execution_claim` / `previous_execution` / `accounting_recovery` scan / record advisory lock removed; the recovery
   job reads the ledger. Acceptance: the SIGKILL drill extended to the chat card (kill after permit and before receipt
   → `unknown` → reconcile → `verified`; kill after receipt → `committed_unverified` → verify → `verified`); two
   sessions approving the same work key → one send, the loser sees the winner's row.
   *Built as PR #267 (2026-09-16), with two deliberate narrowings:* the five MCP treatments share one
   `AccountingCardAdapter` whose three steps are the treatment dispatchers (`tax_correction.validate_approved` /
   `verify_after`), because those dispatchers are the seams every existing test patches — the per-treatment adapter
   classes come when the dispatchers are deleted; and the **native amendment card is not yet on the kernel**: its
   dispatcher's reservation audit and `_authorize_read` read the card's own claim (`accounting_execution`), so it keeps
   `execution_claim` / `previous_execution` until `NativeAmendmentAdapter` lands. The per-record advisory lock stays
   (the account cap is G5's); the ledger's `entity_key` is the collision scope underneath it.
   *What the review rounds taught (four gate rounds + one reproducing review):* every reader of the card's own claim
   (completion, history, recheck, dispatch, plan group) had to be satisfied, so a kernel card carries a **projection**
   of its row in the legacy `accounting_execution` shape plus the `approval_claimed` audit, written at the claim;
   `accounting_recovery.render_settled_card` is the one renderer of a card from its row once its sender is gone, and
   every refusal to run a recovery escalates the row to `needs_review` so the document is freed. Those readers move
   to the ledger when the native card does.
3. **G3.3 generic writes and repair** — `GenericRecordAdapter`, `classify_write_failure`, the repair policy with
   per-class budgets and the cosmetic transform list, `semantic_payload` in the signed envelope, `#218` folded
   (externalId stamping, `duplicate:posted`), `may_enter_repair_loop` deleted. Acceptance: Prompt 1's and Prompt 5's
   test lists, minus the Redis permit; a 401 and a 429 produce no model turn; a timeout produces one reconcile and at
   most one identical retry; `python scripts/codegraph.py callers ns_createRecord` (and update / RESTlet POST) shows
   every dispatch site behind the kernel or a documented read-only exemption.
4. **G3.4 groups and the digest** — `accounting_dispatch.invoke_child` through the kernel; the G2 digest reports
   `unknown`, `committed_unverified` and `needs_review` counts per tenant with the oldest age; the operator list for
   `needs_review`.

G4 (model-authored `MutationPlan` + deterministic validators, metadata-grounded recomposition), G5 (tier-sized
concurrency permit, backfill lease, the 54 cases), G6 (self-correction from verification deltas) and G7 (learned
repairs with provenance, cost per verified write) build on the same rows and add no ledger.

## 10. Invariants that do not move

- Nothing sends without a permit, and a permit is minted only by `reserve_operation_dispatch` after the approval
  source's `still_authorized` and the kill switch. Adapters cannot mint one (`agent-graph.md` #3, #10, #11).
- A model never decides whether a write landed. Existence and end state come from reads (`agent-graph.md` #7).
- Amounts are Decimal strings everywhere except inside `wire()`; the wire JSON is signed separately from the evidence
  (PR #262's decimal → Float lesson).
- A resubmit never changes the business identity to evade the duplicate check; lineage rows inherit `base_work_key`
  (handoff directive 3; the accounting_retry proof stays the model for "no effect").
- No new RESTlets or SuiteScript; the connected MCP record API is enough for the accounting corrections (handoff).
- No auto-approval anywhere in G3; scheduled auto-approval remains a separate explicit policy (G5).
- The mutation guard's deny-list, case-normalized by G1, stays at the dispatcher.

## 11. Open decisions (assumed for now, the operator can overturn any of them)

1. Rename `failed` → `rejected_before_effect` with a data migration, rather than keeping both. Assumed yes: every
   existing `failed` row is pre-effect, and two names for one state is how the next reader adds a third.
2. `needs_review` as a sixth status beyond the handoff's five. Assumed yes: the human-decision terminal has to be a
   row value the digest can count, not a `result_json.code`.
3. The account-wide three-slot cap stays an advisory lock until G5's Redis permit. Assumed yes: it is process-safe
   today and the permit needs the tier detection G5 builds.
4. A generic update's work key includes the semantic payload digest, so two different approved edits to one record are
   two pieces of work and the identical edit twice is one. Assumed yes.
5. The cosmetic transform list ships closed with four entries and grows only by adding a test per entry. Assumed yes,
   per the operator's default on cosmetic resubmit.

## 12. Evidence this design is built on, not inferred

- Live trace: CM 15778068 verified through `credit_api_correction` (approval 3fd5c198…, verification audit f828bbc2…),
  and its three demonstrated failures (decimal → Float 400; floats broke `business_digest`; a rejection treated as a
  permanent execution). Every one of them is a contract this spec makes explicit (`wire()`, Decimal evidence,
  `rejected_before_effect` + `no_effect_proof`).
- The 54-case group crash (`accounting_treatments.py:34`, transport inferred from kind) → G1's registry; the kernel
  reads transport from the adapter, never the kind.
- `#218`'s probe on 6738075-sb1: `externalId` duplicates are rejected server-side with a distinguishable message; the
  MCP tool surface has no header slot. Both are load-bearing for `GenericRecordAdapter.reconcile` and
  `duplicate:posted`.
- The duplicate customer 5264348 (2026-08-27): a timeout classified as failure re-offered a byte-identical payload. The
  kernel's `unknown` → reconcile-first path is the structural fix; `may_enter_repair_loop` is deleted once it exists.
- `ops_digest.py` existed on no ref while `agent-graph.md:82` claimed it; G2 builds it. G3 gives it the states to count.
