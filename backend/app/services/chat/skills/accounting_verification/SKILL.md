---
Name: Accounting Execution Verification
Description: Verify approved changes, recover uncertain outcomes without reposting, and retain auditable resolution evidence.
Triggers:
  - /accounting-verification
---

# Accounting execution and verification

Use the existing signed human-approval and guarded execution path. Re-read source, native record, period, item/account configuration, applications and duplicates before writing. Changed evidence invalidates the old proposal; do not reuse its approval for different fields or records.

After execution, read the actual returned record identity and exact amounts, tax, items, classifications, posting period and GL impact in the correct book/currency. Verify applications and remaining balances. A successful HTTP response or tool receipt alone is not verification. Run complete order/tax/refund reconciliation and identify any residual exception. Do not label cash settlement successful without independent processor/bank evidence.

For timeout, interruption or uncertain outcome, use bounded read-only recovery by the original record/external identity. Never blindly resubmit a create or update. Preserve the execution claim, original approver, before/after evidence and failure state. If verification remains incomplete, keep the case open and explain the exact uncertainty.

Audit the assessment and references, exact approved fields, approver identity/time, preflight, execution receipt, independent verification and reconciliation. Keep original failed attempts. Historical verified resolutions may inform another investigation only when their scope and eligibility match; they cannot grant approval or silently become financial policy. Failed and unverified attempts are not successful playbooks.

Treat resolution as an order-level plan: correct and verify the posting transaction, check the sales order against the finalized source, then reconcile total, tax and refunds. A nonposting sales order still needs consistent source data. Read the plan and completion receipt in accounting history before preparing another operation. After verified execution the bounded completion worker records the result and can prepare the next supported correction from fresh evidence; that dependent correction requires its own exact human approval. Group stages retain the same per-order controls and run at most three independent corrections concurrently. Do not repeat a recorded operation, assume approval carries to another step, or treat a native verification as full reconciliation. Return the verified record links, remaining work and original approver/audit reference. Automatic approval and scheduled financial writes remain disabled until an explicit account policy exists.
