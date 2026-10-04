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
accounting book or posting period: context is recorded as requiring scope, with
`policy_applied: false`. An observed amount difference needs review; it is not a
newly approved accounting treatment.

| Outcome | Meaning |
| --- | --- |
| `observed_missing` | A fresh, authoritative, complete exact lookup found no destination record. This is an observation, not a breached synchronization SLA or permission to backfill. |
| `timing_difference` | The destination observation predates the observed source version. Collect comparable evidence before deciding whether the record is missing or incorrect. |
| `no_discrepancy` | Fresh scoped order-total, tax and completed-refund evidence agrees under the existing case-verification contract. Detail and repair limitations still apply. |
| `needs_review` | Multiple exact matches or observed financial differences require investigation. |
| `incomplete_evidence` | Scope, identity, freshness, currency or coverage could not be established. Unknown values remain unknown. |

A timing or incomplete receipt cannot reconcile an existing case. Case and
observation identities retain the existing uniqueness constraints: retrying a
finding does not duplicate its history, and later observations update the same
business case. Execution finishing and financial reconciliation remain separate.

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
