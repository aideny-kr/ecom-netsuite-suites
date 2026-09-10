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
- [x] Human review and execution, including native missing-order creation.
- [x] Recovery, verification and exact Celigo false-alarm resolution.
- [x] Rendered setup, run and frozen approval verification.
- [x] Seeded HTTP/worker e2e and real process-death recovery for correction and creation.
- [ ] Independent blocking T2 review and deployed sandbox smoke before merge/release.
- [x] Draft PR delivery to both repositories.

The implementation is complete through creation execution and recovery. Current
validation is recorded in the final entries below; earlier entries preserve the
evidence and remaining work at each intermediate commit. No financial write or
deployment has been performed in a live NetSuite account. Both native write
switches default off. Draft PR delivery does not certify the outstanding T2 gate
or account-specific save automation.

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


### Latest-main integration and live native identity evidence

The branch integrates origin/main through `b5ff89cd` without conflicts. The merged
backend passes 7,626 tests (2 skipped), and the frontend passes 1,061 tests plus
its production build. Framework main remains an ancestor eight commits behind
origin main; no remote main branch has been changed by this task.

Fresh read-only samples in USD, EUR and CZK show that the current import does not
populate `custcol_fw_solidus_line_id`. It stores `custcol_fw_inventory_unit_ids`
and `custcol_fw_original_ecom_sku`. All 23 sampled native lines match one complete,
unique source inventory-unit set, with matching original SKU and quantity. The explicit `inventory_units` profile now binds these exact sets and original
SKUs throughout collection, comparison, approval, guarded correction, execution
and read-only recovery; SKU-only or positional matching is not sufficient proof.
The B.V. API also omits `isTaxable` on lines while supplying native `tax1Amt`,
custom VAT and tax code 4059. The BV adapter and guard use those native amounts without requiring the
unavailable checkbox. Aggregate tax profiles still require explicit taxability.

The current CZK source adjustments use tax source ID 7 and are finalized with
update clocks. A bounded read of `tax_rates/7` through the Admin connection is
unavailable (Celigo HTTP 422). The inspected 61 Celigo connections contain no
Avalara or Metabase connection. Neither this absence nor an effective rate proves
a statutory calculation; dynamic assessment policy remains explicit outstanding
work alongside missing-order creation.

### Native identity validation

A fresh live EUR sample exercised the local collectors and normalizer in memory,
without deploying code or issuing financial writes. All seven source and native
lines bound by exact inventory sets and SKU; native currency ID 4 resolved to
EUR with precision 2, and native tax allocations were complete. Source tax
remained incomplete because no statutory-rate policy was configured. A matching
native allocation is not evidence of a statutory rate.

The focused backend suite passes 286 tests, including changed inventory after
approval, private source reads on both executor passes, and recovery that refuses
replacement units without resending. All 107 SuiteApp tests pass; client-only SDF
validation reports no errors. The full backend suite and supplementary review
are recorded below when complete. No deployment or remote main changes occurred.

The live REST sample also omits `handlingCost`. Correction preparation currently
preserves that as unknown; the native guard's independently observed zero may
supply this optional projection later only with matching record identity and
version. Do not infer an omitted amount as zero.

The scope setup now exposes explicit line-identity and legacy-tax selectors,
binding the selected profile to the selected native account/subsidiary. Its 23
focused tests and all 1,071 frontend tests pass, as does the production build.
The actual React component with local fixture hooks was inspected at desktop,
tablet and mobile widths, and a fixture submission retained the exact profile
while actions and scheduling stayed disabled. No console errors were observed.
The separate assessment-policy mock is a design preview only; that policy has
not yet been implemented.

Final native-identity regression verification: 7,670 backend tests passed,
2 skipped (196 warnings; 475.01 seconds). All changed Python files pass Ruff
and format checks. The required T2 pre-merge multi-angle workflow remains
unverified because the independent Codex agents are quota-limited; a
supplementary tool-less CLI review is separate from that gate. Temporary
compiled UI and isolated SDF validation directories have been removed.

### Explicit finalized source assessments

The source-assessment policy is now implemented separately from the default
statutory-rate policy. It requires finalized Framework adjustments with exact
owner, source-tax and adjustment identities, unique ownership, valid update
clocks and complete header/line reconciliation. No statutory rate is inferred.
The matching native legacy profile remains explicit. Fresh execution and
read-only recovery reject altered proof; final money must fit the verified
currency precision. Tests cover malformed flags and clocks, duplicate identities,
wrong owners, mixed incompatible included-tax bases, missing profiles, stale
approvals and unchanged-versus-changed recovery evidence.

A fresh read-only run of the exact local collectors against a seven-line EUR
order produced complete source and native evidence, zero differences and
`no_action`. Eight source assessments included a finalized zero-basis, zero-tax
shipping component for which NetSuite has no row. Comparison permits only this
explicit zero-shipping case against complete reported native allocations; it
retains the original component in the evidence fingerprint.

The actual setup and frozen approval components were rendered and inspected at
desktop and mobile sizes after a design mock. A fixture submission retained the
explicit policy, inventory identity and account/subsidiary-bound native profile,
with scheduling and actions disabled. The approval states that statutory rates
are not independently verified. No browser console errors were observed. All
1,077 frontend tests and the production build pass. The full backend suite passes
7,704 tests with 2 skipped (197 warnings; 444.62 seconds). Ruff, formatting and
diff checks pass. Missing-order creation and the native observation bridge for
omitted REST handling cost remain outstanding. The compiled UI preview was
removed after visual verification; no deployment or financial writes occurred.

### Native observation for an omitted REST handling charge

Correction preparation now accepts the native guard's handling charge only when
REST omits it and the entire native projection matches the remaining REST
evidence. It copies the header locally, never mutates collected evidence or
overrides an explicit REST amount. The planner supplies its fresh account-scoped
guard observation. Unknown, nonzero, stale, foreign or changed evidence blocks
the proposal or execution. Twenty failing regression cases were reproduced
before implementation; 101 action/planner tests and 56 lifecycle/observation
tests now pass, including one human-approved save with omitted handling on both
independent REST reads.
The complete backend run passed 7,726 tests with 2 skipped (198 warnings;
446.86 seconds). Ruff, formatting and diff checks passed. No native deployment
or financial write was performed.

### Exact missing-order inputs

Read-only creation preparation now binds financial source evidence and a separate
private-input fingerprint. Its explicit maps cover source/native SKUs and quantity
multipliers, stock locations, inventory-owning subsidiaries, shipping methods and
the transaction timezone. It retains every financial line, parent identity and
inventory unit, verifies exact native unit rates and rejects unsupported funding,
holds, stale or incomplete input. The current supported source state is a paid,
ready consumer marketplace order funded by completed Stripe-source payments,
without credit, store credit or deposits. It does not create or move payments.

The native routing read confirmed BV sales subsidiary 2 uses inventory subsidiary
1 and location 30; this distinction is explicit configuration, never inferred
from the tax profile or currency. A fresh seven-line EUR input was successfully
prepared using exact mappings observed from its existing native record. Source
and native quantities, complete addresses and both fingerprints were retained.
The live shipment's owner field uses the full order reference rather than the
numeric source ID. Exact numeric owners and omitted redundant owner fields are
also supported, with conflicting references and suffixes rejected. The probe
performed no absence claim, native draft preview or save; its mappings lived only
in memory.

All 113 focused input/assessment/normalization tests pass, including zero-, two-
and three-decimal currencies, nonterminating unit rates, cross-currency payment
evidence and explicit cross-subsidiary inventory. The full backend suite passes
7,774 tests with 2 skipped (197 warnings; 447.31 seconds). Ruff, formatting and
diff checks also pass. Native draft construction, guarded create dispatch, independent create
verification, recovery and the create-review UI remain outstanding. The temporary
input implementation checkout was removed after a byte-for-byte verified copy
into the main task worktree.


### Native missing-order draft and single-save guard

The native library now constructs a bounded unsaved draft from the exact creation
input, resolves existing customer/currency/item/unit/location/shipping metadata
and verifies the actual native fields. The parent guard compares the human-frozen
preview, attributes the order with its work key, repeats absence/period/deadline
checks and permits one save into pending approval. The separate creation flag
defaults off. Read-only attributed snapshots support later independent platform
verification.

Adversarial tests reproduced and fixed header tax-code and taxability changes
that preserved amounts. All 141 SuiteApp tests now pass, including aggregate and
cross-subsidiary routing, duplicate races, held customers, currency/unit mismatch,
closed periods, missing attribution, late expiry and unknown save-time changes.
Client-side SDF validation reports no errors for the isolated package containing
the libraries, work-key field and disabled deployment. Eight owned files were
ported with byte-for-byte checks. No native deployment or live write occurred.
Platform preview/dispatch/verification/recovery, create review UI and create crash
drill remain outstanding.


### Missing-order setup and frozen review UI

The isolated frontend slice owns the setup input/page, proposal card and new
creation review component, their tests and one illustrative native-contract
fixture. It adds opt-in timezone, SKU multiplier, stock-location/inventory-owner
and shipping-method mappings. Every mapping is explicit, and selecting creation
does not enable actions or schedules. The review shows exact transaction-currency
amounts, source/native quantities, routing, existing customer, addresses, native
FX and pending-approval state. The open confirmation retains its frozen input
when a refreshed proposal changes.

An illustrative HTML mock preceded implementation. Actual desktop/mobile setup
and approval components were rendered and inspected. Visual inspection found
and fixed horizontal overflow in the confirmation; its 390px mobile viewport now
contains a 358px dialog with no horizontal overflow. All financial rows remain
scrollable and the bottom decision controls are reachable. A local fixture
submission retained every selected mapping with actions and scheduling off. No
browser console errors occurred. All 1,095 frontend tests and the production
build pass after the final change.

A shared dependency installation was emptied during a repeat build (the deleting
actor is unconfirmed). A fresh isolated npm-ci installation from the lockfile
restored both the complete test run and production build. Eight owned UI files
were copied into the task checkout with byte-for-byte checks; complete locked
dependencies were transferred into its verified empty dependency directory.
Native creation executor, independent verification/recovery and the create crash
drill remain outstanding. No deployment or live financial write occurred.

### Native creation execution and independent recovery

The platform now validates the native unsaved draft, binds private source inputs
into the immutable proposal and reconstructs that exact proposal before execution.
The transport repeats the native preflight and commits one send reservation before
dispatch. Verification uses a fresh exact-reference lookup, the native work key,
matching native/REST versions and the full approved projection. It preserves the
raw pending-approval state and native quantities while separately proving the
explicit source-unit conversion. Changed payment identity, address, source
version, native FX, duplicate matches, tax, quantities or totals cannot become
verified. Recovery uses only reads and preserves the original spend/deadline.

The loopback seeded HTTP drill now covers both correction and creation. A real
child worker is killed after the provider stub records its save. Recovery verifies
the attributed pending order, and duplicate worker delivery still produces one
save with zero temporary-tenant residue. The backend/native shared JSON fixture
also prevents the two preview contracts from drifting.

Two independent read-only reviews inspected the creation pipeline and native
guard. The pipeline reviewer reproduced a 50-line proof that exceeded the 64 KiB
ledger limit after a valid save. The fix verifies complete evidence first, then
retains an explicitly identified compact proof with raw headers/states/quantities,
lookup authority, actual source/private fingerprints, immutable approval identity
and matching approved/observed native projection digests. Recovery verifies the
full report before separately limiting its displayed finding. The reviewer
rechecked the exact fix through 75 lines: the result fits in 8,770 bytes even
when its raw report exceeds 64 KiB. No remaining actionable issue was found in
that bounded review. Native review independently passed 133 creation/correction
tests and reported no confirmed defect. These reviews do not replace T2.

The targeted pipeline/recovery/e2e suite passes 52 tests, including 50/75-line
execution and recovery, a large post-save amount mismatch and both actual-kill
drills. The final full backend run passes 7,850 tests with 2 skipped and 196
warnings in 577.65 seconds. Coverage is 77.16%, above CI's 60% requirement. All 142
SuiteApp tests pass. Both remotes now have the same `main` at `9c0de616`, which
was merged into this branch. The combined frontend passes 1,105 tests and its
production build. Whole-backend Ruff and formatting checks pass.

Ship metadata uses `0.1.0.0`; no root release version previously existed and both
application packages are `0.1.0`. The queue helper is unavailable in this skill
installation, so local minor-version arithmetic is used; neither remote has an
open PR claiming a version. Native sandbox deployment and the blocking T2
workflow remain unverified. No live financial write or remote-main mutation
was performed.

### PR delivery and required unit-test jobs

Draft PRs are open in [origin #225](https://github.com/aideny-kr/ecom-netsuite-suites/pull/225)
and [Framework #3](https://github.com/FrameworkComputer/ai-den/pull/3). The tested
runtime code is identical in both. CI inspection found that the existing workflow
did not execute either frontend unit tests or SuiteApp tests. Both suites now have
dedicated required jobs using the committed lockfiles; native tests use mocks
without downloading the SDK or accessing a NetSuite account. The existing
Playwright smoke job remains advisory and is not counted as full-stack evidence.
The seeded backend HTTP/worker interruption tests are required through the backend
test job. T2 and native sandbox validation remain open as stated in both PRs.
