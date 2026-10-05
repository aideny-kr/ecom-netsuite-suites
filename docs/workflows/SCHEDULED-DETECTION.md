# Scheduled order discrepancy detection

Scheduled transaction investigations reuse the existing opt-in configuration,
bounded collector, worker, provider readers and persistent cases. The supported
pilot is an exact Framework/Solidus order reference compared with a NetSuite
sales order in the configured account and subsidiary. The source mapping and
currency remain explicit. No fuzzy identity matching or cross-currency sum is
introduced.

Each scheduled finding includes a server-generated `scheduled_detection`
receipt. It identifies the run, configuration creator, immutable configuration
key, mapping digest, detector contract, available accounting skill version and
accounting-context revisions. Raw policy statements and provider payloads are
not copied into this receipt. The underlying finding retains its original
record references, source observations, comparison and completeness limits.

The detector is deterministic. It does not invoke a language model or claim
that the available skill was applied. An order timestamp cannot select an
accounting book or posting period: context defaults to requiring scope, with
`policy_applied: false`. An observed amount difference needs review; it is not a
newly approved accounting treatment.

| Outcome | Meaning |
| --- | --- |
| `observed_missing` | A fresh, authoritative, complete exact lookup found no destination record. This is an observation, not a breached synchronization SLA or permission to backfill. |
| `timing_difference` | The destination observation predates the observed source version. Collect a new destination observation before deciding whether the record is missing or incorrect. Native modification clocks alone do not prove timing or sync delay. |
| `no_discrepancy` | Fresh scoped order-total, tax and completed-refund evidence agrees under the existing case-verification contract. Detail and repair limitations still apply. |
| `needs_review` | Multiple exact matches or observed financial differences require investigation. |
| `incomplete_evidence` | Scope, identity, freshness, currency or coverage could not be established. Unknown values remain unknown. |

A timing or incomplete receipt cannot reconcile an existing case. Case and
observation identities retain the existing uniqueness constraints: retrying a
finding does not duplicate its history, and later observations update the same
business case. Execution finishing and financial reconciliation remain separate.

The collector checks access before creating runs. Scheduled source refreshes carry an exact sponsoring configuration through worker continuations and recheck it before provider reads and persistence. Legacy queued scheduled refreshes without a sponsor must be requeued.

The worker rechecks the configuration creator's current company membership,
`recon.run` and `connections.view` permissions, active company, opt-in state and
connector bindings at claim, before each reserved provider read and before
publishing a finding. Revocation stops the run with `stall` and the persisted
reason `scheduled_detection_access_revoked`. An in-flight read may finish; its
result cannot subsequently be published under revoked authority.

Use `detect_only` for the pilot. No correction, human decision, policy approval
or external financial write is produced by detection. Existing action proposals
and execution retain their separate approval, idempotency and audit contracts.
Testing uses synthetic provider responses with real PostgreSQL state and worker
execution. Enabling a real company schedule or deploying this revision requires
the established operational and release gates.


Receipt outcomes also constrain the existing balance status used by run counters,
period filters and exports. Timing and incomplete observations become `incomplete`;
amounts remain unchanged and `observed_balance_status` retains the initial comparison.
The workspace displays the timing verdict explicitly. Complete matching requires
configured refund readers with complete results (including a proven zero).

Before release, inventory every enabled scheduled configuration and verify its
creator is a current same-company human with `recon.run` and `connections.view`,
an active company, and matching active connectors. Existing support-owned or
revoked schedules must be explicitly reassigned/recreated by an authorized owner;
do not silently substitute a principal. Run the existing T2 live smoke after deployment.

Full accounting-policy diagnosis is still outside this foundation: a chosen
book/period and approved scoped context must be bound and used before claiming
an actual accounting error. Older destination versions indicate incomparable
versions, not proof of a breached synchronization SLA.

An optional `mapping_json.scheduled_context` pins a reviewed advisory entry by
`key`, `revision`, `content_sha256`, and explicit `scope` (accounting book ID,
currency and posting period ID). The current manifest must still approve this
exact entry in the selected scope with the unchanged connector/configuration
binding. Unavailable, draft, invalidated, stale, conflicting or revised context
and currency disagreement produce `incomplete_evidence`; they cannot clear a
case. The receipt records `selection_current` only when these checks pass, without
copying policy prose. Omitted selection retains the evidence-only pilot.

Selected book/period IDs are reviewed human scope, not native GL verification.
Sales orders are non-posting: `native_posting_scope_verified` and
`policy_applied` remain false. No generic evaluator turns free-form policy into
accounting treatment or declares a posted accounting error. That acceptance
still requires supported posting evidence and an approved executable contract.

Receipt schema/detector version 2 records independent native-clock semantics.
An older destination modification time does not invalidate fresh matching
order metrics, and cannot turn a financial difference into proven sync lag.
Legacy schema-1 receipts remain historical evidence; detection does not rewrite
prior case observations.

A pinned advisory selection expires at its reviewed `review_by` deadline.
Plan renewal before that date: pause the schedule, create a successor config
with the intended new scope/revision/content hash, propose and review its
context there, then enable the reviewed successor. Existing config mappings
are immutable, and context is keyed by config ID; approval does not transfer.
Until renewed, each observation remains incomplete and can open a case even
when amounts agree. No selection is enabled by this feature. A dedicated
readiness alert/preflight remains a release requirement before real selection.

The current native readers observe the source before the destination. Their
fresh-read path therefore does not normally produce historical observation
timing; seeded timing tests exercise an explicitly older destination snapshot.
A fresh empty lookup means observed absence, not proof of breached sync SLA.
Do not advertise automatic sync-delay diagnosis from this adapter.
