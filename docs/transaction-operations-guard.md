# Framework transaction guard

This RESTlet provides a conditional save for an existing, unfulfilled sales
order. Its tests and SDF package are part of the transaction-operations feature.
The platform planner, executor and missing-order creation path are still being
integrated; installing this artifact alone does not enable end-to-end repairs.

## Contract

`GET action=snapshot&record_id=<internal-id>&reference_field=<configured-field>`
returns a bounded projection containing the exact order reference, record
version, customer/subsidiary/currency IDs, native amounts, original line IDs,
line fulfillment/billing state, tax values and open accounting-period ID.
The response excludes customer contact details and credentials. Unknown fields
or periods cause a rejection rather than guessed defaults.

`POST` accepts version 1, `correct_amounts`, the exact account, a work-key hash,
an unexpired approval deadline, the complete approved snapshot in `before`, and
`after` with `body_changes`, `line_changes` and `expected_totals`.
The platform must persist the authenticated human decision, reserve its durable
send permit, and revalidate the source before making this request. Possession
of a NetSuite OAuth credential is not evidence of a human decision.

The guard compares the approved projection against one newly loaded record.
It addresses lines by their original `line` ID, verifies the full line set,
and allows only explicit amount/rate/VAT changes and shipping/header source
total changes. It preserves customer, currency, subsidiary, FX, quantities,
line membership and lifecycle. The supported record state is pending
fulfillment (`B`), with no fulfilled, billed or closed line, handling charge,
discount, unallocated header VAT or shipping VAT. Existing email, fax or
payment-processing flags prevent a save.

Approved expected totals must agree with the exact line amounts, line VAT and
shipping. Decimal strings cross the SuiteScript numeric boundary only when
they round-trip without changing value; arithmetic uses bounded safe integers.
Values outside those precision limits remain for human review.

The guard checks the accounting period again immediately before a single
`record.save`, with mandatory-field checks enabled and sourcing disabled.
NetSuite standard records use optimistic locking. The period is a separate
record, so this does not provide an atomic lock spanning both records.
[Oracle locking documentation](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2877583.html)
and [numeric sublist field API](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4273166777.html).

After any attempted save, an exception or mismatched reread is `unknown`.
No retry or compensating write occurs. Even a successful `saved` receipt has
`verified: false`: the platform must independently reread Framework and
NetSuite, compare the expected outcome, and update the operation ledger.
Review account-specific sales-order workflows and user-event scripts when
enabling the guard, because NetSuite runs its configured save automation.

## Build and validate the isolated SDF artifact

From `suiteapp`, run:

```sh
npm ci --ignore-scripts
npm test -- --runInBand
node scripts/build-transaction-guard-project.js
```

The last command prints a newly created temporary project directory. It
contains the guard source and its deployment definition, together with a
dedicated manifest and deploy file. The packaging test checks the exact
source, scope, and disabled defaults. Delete the temporary directory after
validation or packaging.

Use an installed Oracle SDK to validate that directory with the intended
sandbox authentication ID. For the Java CLI the command is:

```sh
java -jar <installed-suitecloud-cli.jar> validate -project <printed-directory> -authid <sandbox-auth-id>
```

Add `-server` for account validation. Validation does not install the script.
The parent SuiteApp's older deployment descriptors contain unrelated schema
errors, so use this generated project for the transaction guard.

The deployment begins in `TESTING`, has no broad role audience, and its
`custscript_ecom_tx_ops_enabled` checkbox defaults to false. Configure the
specific integration role and validate the live snapshot/currency/date/custom
field contract in the target sandbox before enabling writes or releasing it.
The SDK validates SDF metadata; unit tests do not establish live record-save
behavior.

The platform connection's `transaction_ops_guard_url` must name the exact
account host and these script/deployment IDs:

```text
https://<account>.restlets.api.netsuite.com/app/site/hosting/restlet.nl?script=customscript_ecom_tx_ops_guard&deploy=customdeploy_ecom_tx_ops_guard
```

No source credentials, account tokens or browser authentication files belong
in the generated artifact.
