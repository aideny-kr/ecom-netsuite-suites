# Framework transaction operations

## Objective and delivery

Connect Framework order data (API discovered from Celigo configuration, with
Metabase or a read-only database source when needed) to NetSuite and Celigo.
Support scheduled and chat-triggered investigations of missing transactions,
VAT/tax and amount mismatches, human-approved repairs, verification, and
false-alarm resolution in Celigo. Deliver through a PR from the isolated
`feat/framework-transaction-ops` worktree. This document tracks the full goal;
an individual completed slice does not mean the product is complete.

## Evidence at start

- Base: `origin/main` at `070f233a`. The starting checkout's batch-write branch
  has unmerged work and is not included in this branch.
- Existing Celigo REST/MCP integration discovers flows, steps, errors and
  NetSuite write provenance. Its sanitizer intentionally excludes captured
  payloads/credentials. The general Celigo tool dispatcher is read-only.
- Reconciliation currently matches Stripe charges to NetSuite deposits.
  Reconciliation approvals update local status; they do not post repairs.
- Chat has a human approval card and guarded NetSuite writes. Additional
  batch-write durability is being developed on a separate branch.
- `schedule.run` is a stub. Nightly reconciliation uses fixed Beat tasks.
- Initial repository inspection did not establish the live API. Subsequent
  read-only production discovery verified Celigo-held HTTP credentials for
  `https://private-direct-access.frame.work/api/`, individual `orders/{number}`
  and paginated `sync/orders` endpoints. Fresh fixed export previews execute
  only those GETs, with no saved hooks or flow checkpoint changes.
- Live NetSuite evidence established an exact full `tranid` match, original
  Solidus line identifiers, ISO currency metadata and precision, legacy SOLIDUS
  tax allocation, and accounting-period state. EUR transaction totals remain
  EUR even when the record also carries a USD exchange rate. Legacy effective
  header rates cannot substitute for individual statutory rates.
- Current create-import mappings include customer/address, item, inventory,
  subsidiary routing and scripted transformations. A totals-only source
  projection is insufficient for an approved create payload. An active
  BigQuery analytics connection was found; no direct Framework database or
  Metabase connection has been established.
- A temporary Codex usage cap interrupted early agents; implementation and
  independent reviews subsequently resumed. No rate-limit failure is counted
  as completed validation.

## Execution plan and acceptance evidence

1. **Transaction evidence and deterministic comparison.** Canonical snapshots
   retain full identifiers, source provenance, observed time, transaction
   currency, destination account/subsidiary, amounts and line/tax details.
   Missing means an authoritative complete lookup proved absence. Currency,
   base-versus-transaction amounts, partial scans, stale evidence, duplicate
   matches and unknown tax values cannot produce a repair-ready comparison.
   Verify with Decimal-based tests for missing orders, VAT/amount/line
   differences, offsetting differences, zero and three-decimal currencies,
   stale/partial evidence and cross-currency/account cases.
2. **Read connections and evidence collection.** Discover endpoint provenance
   from Celigo; establish tenant-scoped encrypted read credentials and explicit
   field mappings. Implement bounded reads/pagination for Framework, NetSuite,
   Celigo error payloads and optionally Metabase/database. Preserve detailed
   business evidence while excluding authentication/session secrets. No guessed
   endpoints or inferred default amounts. Test transport errors, pagination,
   connection isolation, redaction and completeness. Verify actual live shapes
   read-only before declaring a connection operational.
3. **Durable investigation runs.** One service for chat/API/worker entrypoints,
   persisted runs/findings with stable business keys, bounded API/query/token
   budgets, reasoned termination, overlapping schedules deduped, restartable
   reads, progress and failure visibility. Cover API/worker/tenant integration.
4. **Repair proposals and human decisions.** Evidence-bound immutable proposals
   show exact action, record/account/environment, currency, before/after and
   reason. Include missing-order sync, specific NetSuite corrections and Celigo
   false-alarm resolution. Persist authenticated decisions; schedules/models
   cannot self-approve. Rejection remains visible and repeat runs preserve it.
5. **Approved execution and recovery.** Re-read source and target immediately
   before executing; changed evidence invalidates approval. Enforce accounting
   period, record state, currency/subsidiary and transaction-level safety.
   Work-derived idempotency, committed pre-call side-effect ledger, unknown
   outcome reconciliation, explicit retry limits, fresh approval for revised
   payloads. Retry only a specific approved Celigo error/order, never a broad
   flow. Verify in NetSuite, then resolve the linked error only when justified.
   Prove recovery with process-death and concurrent-approval tests.
6. **Product access.** Schedule controls and chat tools use the same durable
   service and existing unified agent. Render and verify approval/run views;
   produce a mock before changing a user-facing surface. Tool-computed amounts
   go to structured output, never prose regenerated by the model.
7. **Delivery.** Appropriate targeted checks, full suite, seeded-tenant e2e,
   independent T2 review, safe live read/approved-write smoke and documented
   operational setup. Create PRs to the configured repositories using Ship.
   Do not merge/deploy or claim live readiness without the required evidence.

## Current state

- [x] Repository gap investigation and isolated worktree.
- [x] Evidence/comparison implementation; exact Decimal arithmetic and explicit
  completeness, identity, currency and tax mapping constraints.
- [x] Bounded Framework and NetSuite readers, backed by read-only live evidence.
- [x] Durable scheduled/chat investigation implementation and targeted tests.
- [ ] Human review and execution.
- [ ] Recovery, verification and Celigo resolution.
- [ ] Rendered product verification and T2 gates.
- [ ] PR delivery.

The integrated backend full suite passes 7,399 tests (2 skipped), including
PostgreSQL, RLS, approval, chat, worker and scheduler tests. The review and setup
UI passes 1,027 frontend tests, TypeScript, lint and the production build.
Intercepted browser QA passes
manual run creation, immutable approval, expired evidence, unknown outcomes,
empty/gated states and mobile layout. Setup was also verified in a rendered, intercepted browser run; screenshots were inspected.

The exact Celigo error reader/resolver and a single-use dispatch reservation
are implemented and tested. The live source and NetSuite reader modules were
exercised together with eight provider reads: an exact matching EUR transaction
correctly remains in gather-evidence state because statutory tax metadata is
incomplete. Source/target setup and schedule controls are implemented.

The correction/resolution execution slice now connects findings to immutable
proposals, authenticated approval to a minute worker, fresh source/NetSuite/guard
revalidation, single-use dispatch, and independent outcome verification. Unknown
operations have one separate read-only recovery run with persisted budgets;
recovery completion and its outcome are committed atomically. Generic queue
redelivery routes a recovery run into the read-only path. All 449 transaction
operations tests pass; the full backend suite also passes (7,399 tests, 2 skipped). No external
customer-data write has been performed.

The NetSuite guard ships as an isolated SDF package with writes disabled. Its
48 guard tests plus packaging and existing SuiteApp tests total 57 passing.
Client-side SDF validation passes. Server validation could not complete because
the saved sandbox SDK authentication had expired; the waiting authentication
process was stopped. Deployment and live write validation have not occurred.

Remaining implementation is substantive: missing-order create preparation and
guarded execution; explicit handling of Framework's legacy tax allocation
profiles; seeded-tenant
HTTP end-to-end and actual process-death drills; complete independent T2 and
shipping gates. An approved UI state alone never means an external change was executed
or verified. The child agents remain usage-limited; root is continuing their
remaining work, and their failed turns are not counted as reviews.

## Ownership for the execution slice

- Root: normalization/comparison, integrated state, planner/executor, API wiring,
  end-to-end tests, operational setup and delivery.
- Source agent in an isolated worktree: `netsuite_actions.py`, its tests, and a
  new SuiteScript transaction guard plus SDF metadata/tests. No existing files
  outside those declared dependencies without coordination.
- Celigo agent in an isolated worktree: `celigo_actions.py` and its tests.
- UI agent in an isolated worktree: transaction-operations routes/components,
  hooks, navigation and frontend tests/rendering.

Direct REST GET then PATCH has no verified NetSuite conditional-write contract.
Corrections therefore require a versioned server-side record guard that checks
approved before values and uses NetSuite's record-save optimistic locking.
The guard must explicitly recheck the accounting period: sales orders are
non-posting, so NetSuite itself may allow their edit in closed periods. A period
and a sales order are separate records; no unsupported atomic cross-record
guarantee will be claimed. Missing-order creation must not use an upsert.

External mutations require a concrete human-approved action in the product.
The user's authorization to build the feature is not approval to change a
customer transaction while developing or testing it.

## Missing-order creation evidence (September 5)

Current Inc and BV Celigo imports both map the complete Framework `number` to
NetSuite `externalid`. Creation must retain that shared identity. Oracle documents
external IDs as unique across the transaction record group, including sales orders
and invoices, not merely per subsidiary or sales-order record type:
https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N3436356.html
An account-wide external-ID conflict is therefore a human-review condition.
Never change the key or use an upsert to bypass a conflict.

REST sales-order writes do not support legacy taxation (Oracle's sales-order REST
documentation). The conditional SuiteScript guard remains the intended narrow
write boundary. Customer, item, currency/precision, period, addresses, tax-code
mapping, shipping and date must be explicit before a missing-order proposal.
Existing Celigo import invoke is not an upstream flow dry run: its mapping/hooks
run, but export transformation, branch routing and customer lookup are not proven
by that invocation alone. No raw Solidus order will be submitted based on that
assumption.


## Next execution slices

1. Known-no-write retries are implemented: the original attempt and rejection
   remain immutable, and at most one further pending proposal is allowed for
   identical economic work after fresh evidence. It requires a new human
   decision. An unknown attempt never qualifies. Targeted retry/state/dispatch
   tests pass (48 tests), and the complete backend suite passes 7,399 tests.
2. Missing-order create: resolve one active customer by exact source identity and
   subsidiary, exact item SKUs with no ambiguous/bundle expansion, currency and
   precision, open period, transaction date, shipping, explicit addresses, and
   any configured location/form/terms. Preserve `number` as external ID and
   check account-wide transaction conflicts. Reject unsupported promotions,
   inventory allocations or ambiguous routing as visible findings. The guard
   rechecks reference absence and original entity/item/currency evidence before
   one standard-mode save; no upsert, customer create, fulfillment, charge or
   broad Celigo flow execution. Independently find and verify the created order.
3. Legacy tax profiles: keep statutory source calculation separate from observed
   NetSuite allocations. Explicit Inc aggregate-header and BV line-tax-amount
   profiles may validate reported tax amounts without pretending a generic
   SOLIDUS effective header rate is statutory. Native tax fields and custom VAT
   fields must both be guarded and verified. This requires additional executable
   validation; current strict unknown-tax behavior remains in place meanwhile.
4. Seeded HTTP/worker coverage and the actual killed-process drill now run in CI.
   Complete rendered missing-order controls and the full independent T2 review.
   The current Claude CLI reviews are supplementary, not that full gate.


The independent Claude CLI execution review completed against the code later
committed as `86905811`. It was tool-less and supplementary, not the T2 gate.
It identified the known-no-write retry issue (now fixed), transient recovery
stranding, proof/termination exception handling, Celigo worst-case call capacity,
large-evidence run failure, and several lower-confidence guard/scheduling issues.
The reproduced failure paths now have regression tests: explicit human read-only
rechecks after inconclusive recovery; proof recorded only after finding persistence;
Celigo's 18-call worst-case evidence budget; bounded, explicitly incomplete findings
for oversized orders; canonical account aliases and source decimals/timestamps;
ECMAScript millisecond UTC approval expiry; a separate dispatch timeout still capped
by the operation deadline; and resumed recovery without counting its order twice.
The recheck UI passed all 1,031 frontend tests, production compilation/type/lint
checks, and mocked desktop/mobile browser QA. Rendered request and queued states
were viewed; the browser made no unexpected network requests. The complete backend
suite passed 7,423 tests (2 skipped) including the final database-recovery fix. A subsequent supplementary
independent review identified the recovery evidence-size gap; it now has a reproduced
regression test and fix. Automatic recovery also reuses an already queued human
check, and API coverage includes an authenticated request from another real seeded
tenant. Recovery now also rolls back a failed database transaction before recording
the error, with a real PostgreSQL division-by-zero regression test proving that
committed spending remains. Ruff and whitespace checks also pass.

The seeded HTTP/worker interruption harness is implemented and passed. It starts
an investigation via the authenticated API, invokes the real workers, denies an
unapproved execution, approves the exact proposal over HTTP, then kills the child
worker with `SIGKILL` after a loopback provider stub records one save. Recovery
independently verifies that outcome, retains the original 21-call spend and send
reservation, and duplicate delivery causes no second write. The harness checked
zero residue after deleting its exact temporary tenant and closing its processes.
This is executable process recovery evidence with simulated provider behavior,
not a live NetSuite mutation or the full T2 gate.

All 22 focused recovery/recheck/e2e tests pass. The e2e harness has an atomic cleanup
journal before the first seed commit, bounded local database calls, SIGTERM cleanup,
and a CI supervisor that owns and terminates its process group. A second real-kill
test kills the drill parent and uses the journal to remove its exact tenant and
temporary directory. Database-fence cases accept the existing CI test database
and reject remote hosts, unrelated databases and other ports.

The full scope still requires missing-order creation, validated legacy tax profiles,
and the blocking T2 review. Lower-confidence review items
remain explicit: live guard field/date shape and period-lock verification behavior,
disabled-scope expired-ledger cleanup, and the resolved-queue search depth. A manual
recheck does not claim that unavailable provider evidence has become conclusive.


### September 5 legacy tax implementation

Explicit account/subsidiary/code profiles now normalize Inc aggregate-header
and BV native-line tax amounts as observed allocations. Source statutory
components remain separate and must calculate correctly before aggregation.
The planner and transport bind the selected profile, and the guard compares
native tax fields, custom VAT, taxability and header source tax before one save.
BV preserves its native tax rate while writing native/custom amounts together;
Inc derives a seven-place rate from the proven taxable net line subtotal. Fresh independent
verification includes the native tax fields. Source rate omissions remain
incomplete evidence; the newly inspected active BigQuery connection's Avalara
mirror contains only stale IDs/timestamps, not a current calculation breakdown.

The bounded live checks confirmed current Inc line/header shapes and the source
adjustment omissions. A reproduced period-date eligibility failure is fixed by
requesting ISO dates in SuiteQL; the live read returned the open September 2026
period in the requested format. Focused backend tests passed 223 cases before
that final date fix, and all 78 SuiteApp tests pass. A complete backend run is
in progress. The supplementary independent tax-normalization/preparation review
found no arithmetic or equality bypass; it raised a latent identifier-delimiter
concern. Actual repair IDs are already numeric at the action boundary, so no
reported live exploit was established. The separate native guard integration
review is still in progress. Neither review substitutes for the blocking T2 gate.


The native-guard supplementary review identified an unsuitable rate denominator
for nonzero shipping and a weaker immediate post-save check. Reproduced tests now
reject aggregate corrections with shipping on either side, retain the net native
basis even for tax-inclusive USD source prices, and require configured NetSuite
rounding to reproduce the exact source tax at the currency's precision. That
rounding policy is part of the immutable proposal fingerprint. The guard also
independently validates the seven-place header rate with bounded integer arithmetic
and compares the entire native profile after save; a native override remains
unknown with one save only. The missing `tax_details` concern was not a bug: the
reader always emits explicit `None` for a complete legacy record, while an omitted
key is incomplete evidence. No fallback from an omitted key was added.

All 88 SuiteApp tests and 77 focused backend review-fix tests pass
against the final patch. The full backend suite previously passed 7,483 tests
before these final review fixes. The fresh complete backend run now passes
7,489 tests (2 skipped); Ruff and whitespace checks also pass. The isolated
SDF artifact passed installed Oracle client-side metadata validation. Server
validation remains unavailable with the expired sandbox SDK authentication;
no script was deployed or live transaction changed.


### September 5 complete source endpoint

The active Celigo source export uses `GET sync/orders/{{number}}`. A fresh
read-only preview proved that this endpoint supplies detailed lines, shipments,
addresses, business entity and review holds in one response. Both inspected
Framework Admin and Prod2 connections support it. The reader now uses this fixed
endpoint, preserving its two-call/40-second bound and its prohibition on executing
saved export scripts, mappings, hooks or delta state. No cross-request merge or
routing inference is needed. The existing list endpoint remains paginated.

Normalization requires `requires_review` to be explicitly false before treating
an order as confirmed or fulfilled. An omitted business entity no longer matches
an explicit null/legacy subsidiary mapping. Private create preparation retains
actual single-field address names, company/country/state fields, batch IDs,
inventory-unit IDs and shipment IDs, stock-location names and shipping-method
codes. Unknown fields, credentials and serial numbers are still excluded; normal
public evidence does not acquire private contact or inventory routing fields.

The exact local projection passed against a current live order with 11 lines,
one shipment and complete address names. All 156 focused backend tests and 50 API/normalization tests pass, including
reproduced missing-data and private-projection cases. The final complete backend
suite passes 7,502 tests (2 skipped). The supplementary review reproduced a literal
`legacy` entity collision; that sentinel is now reserved for explicit null only.
Optional projection fields remain unknown when absent; future create preparation
must require its inputs at the action boundary. A live execution of the exact local
projection and collectors supplies evidence beyond the synthetic test fixture. Missing-order creation
and dynamic tax-assessment evidence remain separate outstanding work; this change
does not claim either of those execution paths is complete.
