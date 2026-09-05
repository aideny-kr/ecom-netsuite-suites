# Framework transaction guard

This RESTlet provides a conditional save for an existing, unfulfilled sales
order. Its tests and SDF package are part of the transaction-operations feature.
The platform planner and executor use it for human-approved corrections.
Missing-order creation is still being implemented. Installing this artifact
alone does not configure or enable repairs.

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

## Platform execution and recovery

The selected active NetSuite connection needs
`metadata_json.transaction_ops_guard_url` set to the account's HTTPS RESTlet URL,
with `script=customscript_ecom_tx_ops_guard` and
`deploy=customdeploy_ecom_tx_ops_guard`. The transport accepts only those exact
script/deployment identifiers on that connection's account-specific NetSuite
RESTlet host. The configured currency, subsidiary, source endpoint and explicit
business mappings must match the collected evidence.

Detection-only configuration creates findings. With `action_mode` set to
`propose_actions`, complete eligible comparisons and a matching enabled guard
can produce pending human proposals. Approval authorizes the exact displayed
intent. A minute worker claims the persisted approval, rechecks the current
approver's permissions, spends a committed read budget, rereads the source and
destination, and rejects changed evidence. The adapter rereads the guard before
committing the one-use dispatch reservation. Each attempt has at most 96
provider calls and 300 seconds, further limited by approval expiry.
The final NetSuite dispatch has a separate 120-second transport allowance for
authentication, the last snapshot and the single write. The original operation
deadline still caps it; a slow response never extends approval or permits a retry.

Provider acknowledgements remain unverified until fresh independent source,
NetSuite and guard reads establish the desired state. A timeout or an
unverified result stays unknown. Recovery gets one separate read-only run with
at most 32 calls and 300 seconds; it cannot reset the attempt or acquire another
send permit. Recovery completion and its verified/unknown outcome are written
atomically. If evidence remains inconclusive, the outcome remains unknown and
blocks another operation on the order.

An authenticated reviewer with `recon.run` can select **Recheck outcome** for an
unknown operation. This creates a new read-only run with the same 32-call,
one-order, 300-second limits. The request uses a UUID evaluation key so an
unconfirmed HTTP response can be retried without buying another check. Concurrent
human checks are rejected while a prior check is queued or running. The run page
records the evidence and termination reason. Rechecks never reset the approval,
dispatch reservation, operation deadline or original read spend. A reclaimed
recovery lease spends again for each provider read and counts its fixed order
scope once. If a finding cannot be persisted, the operation cannot become verified.
Recovery rolls back a failed database transaction and restores tenant context
before recording its error; prior committed read spending is preserved.
An automatic collector racing a queued human check reuses that check and its
lease. It cannot add a second recovery budget. Oversized fresh recovery evidence
is recorded as an incomplete finding and remains unverified.

Oversized investigation findings retain the available header observations and
line counts, explicitly mark the omitted detail incomplete, and require more
evidence. The investigation can continue to later orders; incomplete evidence
cannot produce a repair approval. Account aliases with different case or
underscore/hyphen separators share the same unresolved-operation fence.

Celigo false-alarm proposals additionally bind the exact duplicate-create
error, retry envelope and live flow/import/script configuration. Resolution
sends only that error ID. Verification requires the exact resolved record and
a current matching Framework/NetSuite comparison; an empty open queue is not
proof of resolution.

A known pre-dispatch failure, or an explicit guard rejection before save, may
produce one further pending proposal after fresh evidence. It keeps the original
ledger and requires a new authenticated human decision. There are at most two
attempts for identical economic work. Rejection remains sticky, and uncertain
writes never qualify for this retry.

The local `scripts/uat/transaction_ops_crash_drill.py` harness covers authenticated
HTTP investigation/approval, the real worker and committed ledger, an actual
`SIGKILL` after a loopback provider stub records a save, read-only recovery, duplicate
delivery and exact tenant cleanup. The September 5 run passed with one stub write,
21 original operation calls retained, a verified recovery outcome and zero residue.
Provider behavior is simulated; no live NetSuite order was changed by this drill.
