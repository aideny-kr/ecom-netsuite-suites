---
Name: Accounting Operations
Description: Investigate transaction exceptions, evaluate supported accounting treatments, prepare exact approvals, and verify outcomes.
Triggers:
  - /accounting-operations
  - investigate transaction case
---

# Accounting operations

Resolve the existing case/group scope through the available transaction tools. Reuse its source and ERP connections; do not ask an analytics routing question. All reads and changes remain subject to current tenant permissions.

For reusable cross-connector investigation and evidence handling, the `evidence_led_operations` skill is available on demand.

1. Establish the observed discrepancy and evidence completeness. Unknown is not zero. Separate gross, tax and refunds; do not add tax variance to a gross variance that already includes tax. Keep every nonzero variance, including one cent.
2. Inspect the related subledger before selecting a treatment. Load the `netsuite_subledger` skill when record relationships or GL impact need investigation. Reuse scoped native evidence already collected and request only missing or changed facts.
3. Evaluate alternatives using the `accounting_treatments` skill. State the observed cause, applicable configured policy, eligibility, alternatives ruled out and unresolved facts. A plausible explanation is not a proven cause. An existing difference does not authorize a write.
4. Use the available accounting reference tool when product behavior is uncertain. Consult maintained knowledge and authoritative documentation, record the source and scope, then verify it against this connected account. Never send customer identifiers, transaction amounts, credentials or private evidence to public search. Documentation cannot override permissions, approval, tax policy or a closed period.
5. Prepare only a supported exact proposal through the existing approval workflow. Explain the record, before/after amounts, debit/credit effect, tax, period, applications and expected verification. Do not replace a missing adapter with a generic journal, guessed write payload or repeated create. When an existing credit already represents the refund but its lines post to the wrong account (typically refunded tax booked as a return), load `credit_reallocation` and propose the corrected lines with `transaction_ops_propose_credit_reallocation`. The server verifies the outcome against the source, so this is a supported exact proposal, not a guessed payload.
6. After actual human approval, execution must refresh eligibility and use the guarded adapter. Load `accounting_verification` for outcome and recovery requirements. Report proposed, approved, posted, verified and reconciled as distinct states.

For groups, identify shared causes and reuse configuration/reference research, but validate each member's current evidence. Partition by treatment, account, subsidiary, currency, book, accounts, period and policy. An ineligible member must remain visible; never approve the whole group by similarity alone.

Evidence may include ordered resolution intents and exact native preview requests. Explain the proposed accounting effect and dependent source-document alignment even when no executable card exists. These plans are observations, not approved changes. A preview calculates on an unsaved record; its matching totals do not prove save-time scripts, GL effects or settlement. Check connected preview capability before using it, and distinguish unavailable transport from missing accounting evidence.

Use tools purposefully: state the missing fact each read resolves, batch compatible reads, retain evidence provenance and stop repeating an unchanged failing call. If a supported solution cannot be established, report the precise missing evidence or capability and the next useful investigation. Do not claim a solution, approval or posting that did not occur.

When a source order changed after billing, compare actual order-line prices with ERP lines using source IDs and the recorded original ecommerce SKU. A current catalog price is not the order price. Custom refund requests may link an existing credit/refund that is not discoverable through invoice CreatedFrom alone. Examine those records before proposing another credit. Keep raw sales-order differences separate from net posted invoice/credit effects.

Reconcile gross value, posted tax, receivable applications and cash settlement separately. A matching gross credit/refund does not prove that tax was reversed. Never subtract the source tax difference from invoice tax as if it were the credit's observed tax effect. Use the credit's actual tax or correctly scoped GL evidence; an omitted tax total remains unknown. A fully applied credit/refund does not establish complete deposit history or bank settlement. Retain unresolved tax and sales-order differences even when the gross amounts reconcile, and distinguish a verified observation from an inferred historical cause.

A source adjustment's `finalized` flag can describe whether the commerce engine recalculates it. It does not by itself establish accounting approval or legal tax correctness. Check the integration/version and current source revision; preserve the eligibility rules of existing correction adapters. Distinguish an unsupported correction method from incomplete evidence, and identify each explicitly.

For credit tax or line changes, inspect the `credit_taxation` reference topic and the current connector's metadata. Oracle's REST credit-memo documentation distinguishes SuiteTax from legacy tax support. An advertised field or a successful invoice-header correction does not establish support for a credit's tax-line change. Establish the applicable feature and transport before preparing that operation; retain proven existing adapters within their verified scope. That restriction concerns tax-engine lines: moving a credit's amount between item lines (for example to the subsidiary's tax-refund item) changes no tax line and is the `credit_reallocation` treatment.

Choose the document from the economic event and original agreement, not current source equality alone. A later return or concession can correctly leave the original invoice and sales order unchanged. If an existing credit already represents that event but its allocation is wrong, correct that credit only (`credit_reallocation`); an invoice change or another credit would double-count the adjustment. A sales-order amendment requires independent evidence that the order itself is wrong and that changing it is appropriate for its lifecycle. Honor an explicit case resolution scope and its protected records.

Use the existing connected MCP/API record tools for supported changes through the signed approval workflow. A missing custom preview endpoint is not itself a requirement to install a script. Distinguish advertised field support from verified save behavior; explain the exact intended result and independently verify the approved save. Never claim an unsaved native preview occurred when only schema and arithmetic were checked.
