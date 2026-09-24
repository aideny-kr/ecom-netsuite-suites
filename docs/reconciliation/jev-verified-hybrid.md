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
  applies to the deterministic planner's amount-mismatch/FX branches. Materiality
  settings are unchanged.
- Washout classification rechecks cached same-order charge/refund events and the
  existing seven-day net-zero rule. Multiple charges, truncated evidence, unknown
  event dates, conflicting subsidiary/currency, or a partial refund fail closed.
  It proves charge/refund netting, not completeness of all ERP ledger dependencies.
- Model-proposed deposit creation/application remains held: the required canonical
  source-coverage/application-state proof is not available. Existing deterministic
  planner policies outside the amount-mismatch/FX branches are unchanged.

No fresh upstream data pull is required. Jev still receives only code-derived
boolean/null/allowlisted categorical facts; no customer text, amounts or identifiers.

## Scoped activation and rollback

`JEV_RECON_RESOLUTION_MODE=shadow` remains the global default for a shadow rollout.
`JEV_RECON_LIVE_TENANTS` is a comma-separated list of customer UUIDs promoted to live
while other tenants remain shadow. `JEV_TENANT_ALLOWLIST` and the configured API key
still gate every outbound call. Global `off` overrides the scoped list. Removing a
UUID returns it to shadow without disabling other tenants.

Measure actual fallback rate, guard vetoes, applied actions, and latency in
`recon.jev_comparison`; confident human-review classifications are not newly
resolved accounting cases. Classifier speed does not measure ERP extraction speed.

Regression coverage: `test_resolution_verified_hybrid.py` contains the five FX
holds and verified washout, adversarial evidence, and scoped activation. Existing
resolution-worker, proposal lifecycle, rollback/RLS and seeded end-to-end checks
exercise the shared validator through both classifier paths.
