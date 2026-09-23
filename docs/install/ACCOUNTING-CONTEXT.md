# Versioned accounting context

Accounting context distinguishes **observed configuration**, **company policy** and
**inference**. This is an advisory evidence register. It does not enable financial
adapters, edit company instructions, populate soul files, or approve transactions.
Existing accounting profiles and financial approval controls remain authoritative
for supported execution. Nothing is imported or approved automatically.

A company administrator with `connections.manage` can submit a context revision
through `POST /api/v1/transaction-ops/configs/{config_id}/accounting-context`.
The config supplies the company, source, NetSuite connection/account and subsidiary.
The body additionally requires an exact native accounting book ID, ISO currency
and native posting period ID. There are no wildcard scopes or cross-subsidiary
fallbacks. Both transaction-operation feature flags must be enabled for this API.

Example with synthetic identifiers (dates must be appropriate to the actual review):

```json
{
  "expected_version": 0,
  "key": "revenue",
  "topic": "revenue",
  "kind": "company_policy",
  "scope": {"accounting_book_id": "1", "currency": "USD", "posting_period_id": "100"},
  "statement": "Synthetic example for review; replace with authorized company evidence.",
  "owner": "Accounting policy owner",
  "sources": [{"reference": "synthetic:policy-1", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "observed_at": "2026-09-23T00:00:00Z"}],
  "effective_from": "2026-09-23T00:00:00Z",
  "review_by": "2026-10-23T00:00:00Z"
}
```

Sources are references and content hashes supplied by the human. The register does
not fetch them, certify their contents, or turn an ERP observation into company
policy. Use private evidence references; do not put credentials in statements or
references. Keep original instructions and knowledge in their existing locations.

`POST .../accounting-context/resolve` takes the three scope fields and returns the
current version, audit ID, configuration binding hash and entry revisions, owners,
sources, effective dates and reviews. Reading requires `recon.run`. Company and
connection access is rechecked. `GET .../accounting-context/history` returns immutable
historical snapshots with `limit` (1–100) and `before_version`; follow
`next_before_version` until null. History is a record of past decisions, not current
permission. Retain these audit records with the deployment's database backups.

Review uses `POST .../accounting-context/{key}/review` with `expected_version`,
`content_sha256`, `decision` (`approve` or `invalidate`), `reason` and
`authority_reference`. The server records the authenticated human, not an actor ID
from the body. A review of company policy requires the human's actual policy
authority; a capability to submit this endpoint is not a grant of business authority.
An observed configuration is marked `verified`, a company policy `approved`.
An inference cannot be approved or reclassified in place. A separately attributed
policy proposal and explicit review are required to establish policy.

Every edit clears that entry's approval, increments its revision and appends a new
register version. Concurrent updates/reviews must use the current version and exact
content hash; stale requests return 409. The existing config row serializes writes.
Changes to source/configuration, connection authentication type, or configured
accounting treatments require a new revision and review. A revoked/disabled
connection withholds current context; elapsed `review_by` dates are `stale`.
Future effective dates cannot yet be approved. Explicitly invalidated entries also
require a new revision. Conflicting reviewed claims for the same topic and exact
scope block approval until the obsolete claim is explicitly invalidated.
There is a maximum of 100 named entries per configuration.

The existing agent accounting-context tool exposes this provenance under `policies`.
Supply `context_config_id`, `accounting_book_id`, `currency` and `posting_period_id`
together to inspect matching content. Without all four it returns metadata only.
Wrong scopes never return another scope's statement or mark it usable as policy.
The read result identifies the exact context audit/version and binding used.
Investigations include a metadata receipt in their accounting context and save it
with accounting-evidence audit records. A sales order does not establish a book or
posting period, so these reads do not silently select a policy. Runtime skill/run
binding beyond this existing accounting surface remains separate work.

No automatic source refresh or policy discovery is claimed. When source evidence
changes, submit a new revision; for an urgent withdrawal, invalidate the entry.
Before any financial action, independently revalidate native evidence, permissions,
period locks and the concrete approved operation through the existing controls.
