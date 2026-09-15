# Native accounting amendments

The application can prepare, approve, dispatch and independently verify two
bounded amendments: reallocate tax on an existing fully applied Sales Adjustments
credit without changing its gross amount, then align the related sales-order
lines and integration VAT. Each step requires its own persisted approval. The
existing invoice correction, invoice discount, credit creation and sales-order
header paths retain their contracts.

Read-only investigation also publishes an audited posting comparison when a
source price/tax revision, owned credit/refund chain and balanced ledgers prove
the relationship. It shows invoice less the existing credit, including separate
net and tax deltas. The original non-posting sales-order differences remain
visible and keep the case open. Arbitrary refunds are never subtracted from an
unchanged source order. An identified solution remains visible when account
configuration or unsaved preview is unavailable; it is not an approval card.

Transactions, case evidence and Excel exports reuse the same comparison. The
projection is bound to the exact original report, tenant and case; a subsequent
scan or a newer incomplete observation invalidates it. Reading a page performs
bounded local audit lookups, with no model or upstream accounting calls.

## Scope and installation status

This integration is disabled for an unconfigured tenant. It supports the existing
Solidus source adapter and NetSuite legacy tax; SuiteTax, other source adapters,
unknown line identities, unsupported currencies/precision and ambiguous accounting
evidence require a different supported treatment. Field mappings and GL account
allowlists are customer-specific configuration, with no Framework defaults in
the native execution path.

No NetSuite installation or live posting is implied by an application deployment.
The native artifact remains TESTING, with all-roles/all-employees access disabled
and the amendment-enable parameter false. Installing scripts, exposing a deployment,
changing a role or enabling writes is a separate account-administration action.

Build the isolated local SDF artifact with:

```sh
node suiteapp/scripts/build-accounting-amendment-project.js
```

The script prints a temporary directory containing only the amendment RESTlet,
its shared core, the operation-attribution field and SDF manifests. It performs
no deployment, credential access or remote call. Review existing ownership and
usage of `custbody_ecom_tx_ops_work_key` before an approved installation. Do not
deploy the full SuiteApp project merely to install these files.

## Customer configuration

An authorized account administrator must verify the connected account, execution
role, subsidiary, accounting book, existing AR/tax/adjustment accounts, source
adapter and these actual custom fields:

| Profile field | Meaning |
| --- | --- |
| `order_reference` | Transaction body field linking the order |
| `source_line_id` | Item column containing the stable source line ID |
| `original_sku` | Item column containing the original source SKU |
| `vat_amount` | Item column containing the integration VAT amount |

The installation-owned `custscript_ecom_acct_amend_profile` parameter accepts one
JSON object, or up to 20 unambiguous subsidiary profiles. Each includes
`schema_version: 1`, `account_id`, `subsidiary_id`, `role_id`,
`tax_regime: "legacy"` and the `fields` mapping above. All IDs are strings.
Missing profiles, mismatched roles and ambiguous subsidiaries are rejected.

The app's `PUT /api/v1/transaction-ops/configs/{config_id}/native-accounting-profile`
endpoint requires a current human with `connections.manage`. Its body is
`{"profile": {...}}`; null disables the profile. In addition to the native
fields above, the application profile requires `enabled`, `accounting_book_id`,
`source_adapter: "solidus"`,
`treatment: "restore_existing_source_tax_allocation"`, and explicit
`ar_account_ids`, `tax_account_ids`, `adjustment_account_ids` lists. Configuration
is bound to the selected tenant, connection and reconciliation scope; changes
are audited. Configuration never constitutes an approval to change a transaction.

The existing connection's OAuth transport uses the fixed account RESTlet host,
`customscript_ecom_acct_amend` and `customdeploy_ecom_acct_amend`. The read-only
capabilities handshake must match both configurations. The public transport
permits only capabilities, snapshot and unsaved preview operations. The internal
apply operation is excluded from model tool discovery.

## Approval, dispatch and verification

1. Read current source, linked records, applications, GL and role-visible
   accounting configuration. Match source lines by verified keys.
2. Read installed native capabilities and compare an unsaved native calculation
   against the exact proposed amounts and protected fields. Only supported,
   enabled capabilities can produce an executable card.
3. Bind the full proposal, source/profile revisions, native before/after proof
   and plan to the approval signature. Show exact before/after amounts and fields.
4. On approval, recheck the persisted actor, permissions, signature, current
   source and accounting evidence. Commit an operation-key audit reservation
   before making one native request. The native guard rechecks its before snapshot,
   expiry, period restrictions and protected fields before a single save.
5. Independently read the resulting record, GL, applications and unchanged related
   records. A successful API receipt alone is insufficient. Unknown results and
   worker interruption go through read-only recovery; they never blindly resend.
6. Publish a separate dependent sales-order approval only after the credit step
   is actually approved and verified. Recheck that predecessor again before the
   sales-order step. Full reconciliation and settlement follow independently.

Groups reuse the durable dispatcher: up to three independent orders per account
can execute concurrently; related invoice/credit/sales-order operations share
one document lock. Each child retains its signature, approver, reservation,
verification, record links and audit links. Partial or uncertain outcomes cannot
be reported as a completed financial reconciliation.

## Acceptance and operational limits

Local tests cover native calculation/preservation, a second customer's fields,
signature/tenant/actor isolation, one-send audit ordering, interrupted sends,
read-only recovery and existing correction regressions. A live native save,
role visibility, mandatory fields, native sourcing and concurrent-edit behavior
still require account-specific acceptance after installation and explicit approval.
Keep profiles disabled until that acceptance is scheduled. App-only deployment
does not demonstrate that an actual order has been corrected.
