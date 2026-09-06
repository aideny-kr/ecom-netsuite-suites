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

When REST omits `handlingCost`, correction preparation can use `handlingcost`
from the independently read native guard. The complete guard snapshot must
match the REST record identity, version, customer, subsidiary, currency, FX,
period and all line values. The transport has already checked its account and
the planner checks freshness. An explicit REST amount is never overwritten;
missing native evidence remains unknown and a nonzero handling charge remains
unsupported. Execution repeats these checks before reserving its one send.

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

## Exact inventory line identity

For current Framework imports, set `mapping_json.line_identity_mode` to
`inventory_units`. Every source line must supply a nonempty set of unique
inventory-unit IDs and an exact SKU. Each native line must contain that exact
set in `custcol_fw_inventory_unit_ids` and the same
`custcol_fw_original_ecom_sku`. Shared IDs, partial sets, missing SKUs and
ambiguous ownership make line evidence incomplete. Currency, position and SKU
alone never establish a match. Quantity differences remain visible and prevent
amount-only corrections.

Investigations, execution and read-only recovery request the private sync
projection for this profile. Canonical evidence and immutable approvals retain
the inventory IDs and SKU; no serial numbers or customer contacts are added.
The native guard receives `line_identity_mode=inventory_units`, snapshots those
actual native fields, and compares them again before its single save. It does
not require or write the unused `custcol_fw_solidus_line_id`. A post-save
inventory replacement remains unknown pending independent verification.

The default `source_line_id` profile remains available for integrations that
actually populate that field. Changing an identity policy requires fresh
proposals and human approval; existing approval evidence cannot silently change
profiles.

## Explicit legacy tax profiles

`mapping_json.netsuite_legacy_tax` supports version 1 profiles with an exact
`account_id`, `subsidiary_id`, numeric `tax_code_id`, and a `mode` of
`aggregate_header` or `line_tax_amount`. Account aliases are canonicalized.
The profile is explicit configuration; currency or a tax item's display name
never selects it. Native SuiteTax detail takes precedence when present.

The aggregate profile describes the inspected Framework Inc import: native
header tax item/rate, explicit line taxability, custom line VAT and the custom
header source tax amount must agree. It does not require nonexistent native
line tax codes/rates. The line amount profile describes the BV import: each
native `tax1amt` must equal the corresponding custom line VAT, with the exact
configured native tax code. The BV profile does not require the line taxability
checkbox, which the live BV record API omits; the aggregate profile still
requires explicit header and line taxability. Both profiles require allocations to reconcile to
the native header tax total. Taxed shipping still requires more evidence.

Reported destination allocations have no claimed statutory rate. Under the
default `statutory_rate` source policy, components retain their actual configured
rates, rounding and adjustment IDs; each component must validate before multiple
components can be compared with one destination allocation. A complete empty
source adjustment list can prove zero tax. Missing rates remain incomplete under
this policy.

An explicit `source_assessment` policy instead uses finalized Framework tax
adjustments. It requires a matching legacy native tax profile and configured
included/additional basis and native tax code for each source tax ID. Each
component retains its exact positive source-tax and adjustment IDs, unique owner,
literal finalized flag and timezone-aware update clock. The adjustment cannot be
newer than the source version or observation. Source headers, lines, shipping and
included/additional totals must still reconcile exactly. The collector,
comparison and action boundary independently reject incomplete or changed proof;
the final monetary values must fit the verified currency precision.

This policy does not independently verify statutory rates and never invents a
rate or rounding rule. Setup, comparison evidence and the frozen human approval
show that limitation. The setup selector applies the chosen policy to every tax
rule and clears incompatible rates and rounding. Changed assessment identities,
timestamps or finalized flags invalidate an approved correction. Recovery also
requires the unchanged proof before verifying an outcome.

A finalized shipping assessment with both zero basis and zero tax may be absent
from native reported allocations only when both complete orders explicitly have
zero shipping and shipping tax. Its proof remains in the source evidence and
approval fingerprint. Positive amounts, unknown amounts and other tax policies
do not receive this exception.

The guard snapshot request adds `tax_mode` and `tax_code_id`. Its response must
contain the same exact profile and native tax fields. Inc corrections require zero shipping on both sides and an explicit
`netsuite_tax_rounding` policy (`half_up` or `half_even`). Their seven-place
header rate is source tax divided by the proven taxable net line subtotal,
multiplied by 100. The rounded native calculation must reproduce the exact
source tax at the verified currency precision. The guard independently checks
the seven-place rate using bounded integer arithmetic. This avoids copying the
import's currency-dependent approximation when its denominator is unsuitable.
BV corrections update
native `tax1amt` and custom VAT together while retaining native `taxrate1`.
The custom header source tax amount is guarded and updated in both modes.
Mode-incompatible writes and positive tax rounded into a zero native rate are
rejected. After save, the guard checks the complete native profile again; any
changed native field leaves its receipt unknown. Independent platform
verification still includes all these fields and the unchanged source.

Live read-only checks on September 5 confirmed the current Inc native shape and
that recent source adjustments omit a rate breakdown. An Avalara reporting
mirror was inspected through the existing active BigQuery connector; its table
was last modified in 2022 and contains only transaction/order IDs and timestamps.
It is not current tax-calculation evidence. No source rate is inferred from it.
NetSuite accounting-period dates are requested explicitly as ISO strings; the
live API returned the open September 2026 period with `2026-09-01` and
`2026-09-30`, independent of its default display-date locale.

## Missing-order input preparation

Missing-order input preparation is implemented separately from native preview and
dispatch. `mapping_json.netsuite_create` requires version 1, an empty
`external_id_prefix`, `tax_mode=legacy_tax_codes`, an explicit
`transaction_timezone`, and these exact mappings:

| Setting | Meaning |
| --- | --- |
| `sku_rules` | Source SKU to `netsuite_sku` and positive integer `quantity_multiplier` |
| `stock_location_ids` | Source stock-location name to native location ID |
| `inventory_subsidiary_ids` | The same stock-location name to its native inventory owner |
| `shipping_method_ids` | Source shipping-method ID to native shipping-method ID |
| `inventory_mode` | `line_location` or explicit `cross_subsidiary` routing |

Inventory ownership is independent of the sales subsidiary and currency. Native
location mode requires the same subsidiary; cross-subsidiary mode preserves the
explicit inventory owner. The live BV import uses Inc-owned inventory. Creation
also requires the exact inventory identity profile and a matching legacy tax
profile. Aggregate tax needs zero shipping and an explicit native rounding rule.

The current input contract supports complete, paid, ready consumer marketplace
orders with no review hold, credit sale, store credit, deposit or unsupported
adjustment. Completed Stripe-source payments must reconcile to the source total;
explicit conflicting currency or FX evidence blocks preparation. No payment is
created or moved. Source line quantities, native quantity multipliers, exact unit
prices, tax allocations, parent identities, inventory and shipment relationships
must all reconcile. Native unit prices are never rounded to fit. Input is bounded
to 100 lines and 64 KiB.

Every source SKU needs a mapping, including unchanged SKUs. Shipments may identify
their owner by the full order reference or numeric source ID; a conflicting owner
is rejected. The verified individual sync response also supports shipments nested
under their order without a redundant owner field. Addresses and the private sync
projection receive a separate immutable fingerprint. These prepared inputs are
read-only and do not themselves establish native absence, grant approval or save
an order. Native preview, guarded save and platform integration remain subsequent
steps.

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


Individual Framework reads use the live flow's `sync/orders/{number}` endpoint,
which includes routing and review-hold fields together with detailed lines and
addresses. An omitted review flag is unknown, and an omitted business entity does
not select a legacy subsidiary. The `legacy` mapping key is reserved for explicit
null entities; a literal entity with that name cannot use it. Each read still spends at most two Celigo calls
under one 40-second deadline. The private projection for create preparation keeps
single-field address names and explicit inventory/shipping identities; the public
evidence API omits those private fields.
