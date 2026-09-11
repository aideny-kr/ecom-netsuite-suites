---
Name: Accounting Treatment Assessment
Description: Compare eligible resolutions using subledger evidence, configured policy and supported execution capabilities.
Triggers:
  - /accounting-treatments
---

# Accounting treatment assessment

Start with the cause and complete current evidence. Account configuration and approved company policy determine eligibility; general documentation alone does not establish a best treatment. Do not manufacture a policy or tax conclusion from a prior example.

Consider these alternatives, then distinguish a supported exact proposal from further investigation:

- Already corrected or timing: re-read linked adjustments and full reconciliation. Do not post again to clear a stale observation.
- Existing credit or deposit: verify identity, unallocated balance, customer, currency, subsidiary, AR and intended application before considering an application. Existing credit should be investigated before creating another. Applying a record and issuing cash are distinct operations.
- Fully unpaid invoice: a finalized commercial source adjustment may fit the configured invoice-discount adapter. Current support requires its exact eligibility, including no applications/existing discounts, zero tax/shipping, a posting Discount item and the original current open period. Unpaid status alone does not authorize editing an issued invoice. Unsupported unpaid cases must not silently become new credits.
- Paid or partially paid invoice: inspect the full application and refund chain and company policy. Evaluate a credit or another supported treatment without rewriting settled history. Never infer that every paid invoice needs a credit or that every partial payment fits an existing recipe.
- Tax/shipping/rounding: determine the finalized basis and native allocation, jurisdiction/configuration and period implications. Keep penny differences visible. Use only a verified supported adapter; do not invent an exemption, tax code, statutory rate or compensating amount.
- Missing order/refund: prove absence with complete scoped identity searches and inspect integration execution/idempotency. Distinguish replaying an accounting record from issuing money again. Use an approved supported sync adapter only when its exact semantics are known.
- Closed or restricted period: preserve restrictions. Do not reopen periods, change role permissions or invent a current-period adjustment. Establish an approved supported treatment and its period policy.

Credit creation from an invoice may inherit fields through a native transform, but a REST transform can save the new record; it is not a preview/read. Use it only if an implemented guarded adapter supports the exact human-approved operation. Never duplicate a reference credit's customer, tax, applications or classifications without current evidence.

Oracle distinguishes posting/nonposting Discount items and credit applications:
- https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N2248474.html
- https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N1312521.html
These references explain product mechanics. They do not authorize a change or certify company policy.
