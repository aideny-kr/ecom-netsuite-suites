# Verified Jev resolution cascade

Jev proposes typed actions for planner abstentions. In live mode a confident,
verified proposal skips the larger model. Uncertainty, service failure, or a veto
uses the existing model once; its proposal passes the same deterministic verifier.
Neither model posts to NetSuite. Existing human approval, closed-period, tenant,
idempotency, and proposal compare-and-set controls remain in force.

## Evidence requirements

- The matched result's actual deposit is loaded by ID and tenant independently of
  fuzzy candidates. Its order reference, subsidiary, base/transaction currency,
  amount, and the linked charge/settlement line must agree.
- A fee must equal the signed charge-minus-deposit difference exactly, with a
  positive fee and consistent charge/fee/net arithmetic. The former fifty-cent
  tolerance is removed. Tiny unrelated residuals do not establish a fee.
- Cross-currency or missing transaction-currency evidence is held for FX/basis
  verification before fees or write-offs, including below materiality. This also
  applies before the deterministic planner's fee/application/FX branches. Materiality
  settings are unchanged.
- Washout classification rechecks cached same-order charge/refund events and the
  existing seven-day net-zero rule. Multiple charges, truncated evidence, unknown
  event dates, conflicting subsidiary/currency, or a partial refund fail closed.
  It proves charge/refund netting, not completeness of all ERP ledger dependencies.
- Model-proposed deposit creation/application remains held: the required canonical
  source-coverage/application-state proof is not available. Deterministic fee
  proposals require the same exact monetary proof; washouts recheck canonical
  events during normal planning, before a permanent carry-forward is proposed.

No fresh upstream data pull is required. Jev still receives only code-derived
boolean/null/allowlisted categorical facts; no customer text, amounts or identifiers.

## Activation and rollback

Jev is on by default (decided 2026-09-24). For each tenant, `services/typesafe/access.py`
picks the key: the tenant's own TypeSafe key from its Jev card (Settings → Connections,
stored encrypted as a `typesafe` connection), else the deployment's `TYPESAFE_API_KEY`.
It also picks the mode the tenant chose on that card: live (default), shadow or off. With
no key anywhere, the existing model path runs unchanged, so self-hosted deployments need
no TypeSafe account. `JEV_RECON_RESOLUTION_MODE` caps every tenant: `live` (default, no
cap), `shadow`, or `off` (kill switch). A tenant key that cannot be decrypted turns Jev off
for that tenant instead of falling back to the platform key.

Rollback: switch the tenant's Jev card to Shadow or Off, or set
`JEV_RECON_RESOLUTION_MODE=off` for the whole deployment.

Measure actual fallback rate, guard vetoes, applied actions, and latency in
`recon.jev_comparison`; confident human-review classifications are not newly
resolved accounting cases. Classifier speed does not measure ERP extraction speed.

Regression coverage: `test_resolution_verified_hybrid.py` contains the five FX
holds and verified washout, adversarial evidence, and scoped activation. Existing
resolution-worker, proposal lifecycle, rollback/RLS and seeded end-to-end checks
exercise the shared validator through both classifier paths.
