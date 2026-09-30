# Saved-evidence period reviews

`POST /transaction-ops/configs/{id}/review` accepts `evidence_mode: "saved"`.
Omitting it preserves the existing standard review, existing work keys and daily
schedules. The Transactions period selector exposes the new mode explicitly.

An uncovered saved review still runs source, refund and destination/dependency
discovery. The resulting candidate population is therefore not inferred from
retained findings. Before order-detail collection, a bounded query selects the
latest retained finding for each discovered reference from the current config or
its direct predecessor. The existing deterministic policy-equivalence proof must
establish a complete numeric result under either the identical mapping or the
supported refund-reason-only change. Other policy/scope/contract differences,
missing evidence, and changes newer than the observation use normal fresh reads.
Deletion candidates require an observation after their deletion-page read.

Reusable prefixes of up to 100 members are persisted with their cursor in one
transaction. Findings carry original read times, original finding ID and payload
hash, evaluation time and an explicit historical basis. The writer checks the
lease, enabled config, tenant feature permissions, contiguous cursor and original
payload hash. It does not write cases, approvals, operations or JEV judgments.
Existing case links permit navigation; they are not write authority. Remaining
members use the normal collector and existing classification/verification path.

No TTL is extended. A saved review cannot establish standard-review or scheduled
daily coverage, or refresh their result selection. A completed saved review can
serve another saved report without a provider scan. Reports, API rows and Excel
preserve the original cached observation date. Review progress reports reuse
counts separately; discovery completion remains distinct from financial signoff.

This is not a general current-state change-data-capture cache. It does not prove
that providers have not changed since the saved observation. It does not skip an
uncovered discovery window, reconstruct missing old discoveries from findings,
modify the active review's requested evidence mode, or automatically convert a
running standard review into a saved review. Unknown legacy extraction contracts
continue to require fresh reads. Evidence from cached copies is not recursively
used to bypass the original-provider-observation requirement.

## Acceptance and release checks

- Cached member: zero provider/JEV calls; same amounts and original read time.
- Affected, missing, incompatible, future or older-than-known-change proof: fresh path.
- Discovery remains responsible for members missing from retained findings.
- Lost lease, disabled config/feature, changed payload and interrupted commit:
  no cursor advancement or partial persisted batch.
- Mode remains bound across HTTP retries, continuation and result selection.
- Saved reviews cannot advance daily coverage or overwrite fresh observations.
- Existing standard review work-key serialization is unchanged.
- UI explicitly identifies saved observations; export dates agree with API rows.
- T2 independent pre-merge review, seeded lifecycle CI and safe live smoke required.

Implementation model: GPT-6 (Astra). Record actual independent reviewer metadata,
reviewed source revision, CI and live measurements in the release handoff.
