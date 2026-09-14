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
