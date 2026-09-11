---
Name: Accounting Operations
Description: Investigate transaction exceptions, evaluate supported accounting treatments, prepare exact approvals, and verify outcomes.
Triggers:
  - /accounting-operations
  - investigate transaction case
---

# Accounting operations

Resolve the existing case/group scope through the available transaction tools. Reuse its source and ERP connections; do not ask an analytics routing question. All reads and changes remain subject to current tenant permissions.

1. Establish the observed discrepancy and evidence completeness. Unknown is not zero. Separate gross, tax and refunds; do not add tax variance to a gross variance that already includes tax. Keep every nonzero variance, including one cent.
2. Inspect the related subledger before selecting a treatment. Load the `netsuite_subledger` skill when record relationships or GL impact need investigation. Reuse scoped native evidence already collected and request only missing or changed facts.
3. Evaluate alternatives using the `accounting_treatments` skill. State the observed cause, applicable configured policy, eligibility, alternatives ruled out and unresolved facts. A plausible explanation is not a proven cause. An existing difference does not authorize a write.
4. Use the available accounting reference tool when product behavior is uncertain. Consult maintained knowledge and authoritative documentation, record the source and scope, then verify it against this connected account. Never send customer identifiers, transaction amounts, credentials or private evidence to public search. Documentation cannot override permissions, approval, tax policy or a closed period.
5. Prepare only a supported exact proposal through the existing approval workflow. Explain the record, before/after amounts, debit/credit effect, tax, period, applications and expected verification. Do not replace a missing adapter with a generic journal, guessed write payload or repeated create.
6. After actual human approval, execution must refresh eligibility and use the guarded adapter. Load `accounting_verification` for outcome and recovery requirements. Report proposed, approved, posted, verified and reconciled as distinct states.

For groups, identify shared causes and reuse configuration/reference research, but validate each member's current evidence. Partition by treatment, account, subsidiary, currency, book, accounts, period and policy. An ineligible member must remain visible; never approve the whole group by similarity alone.

Use tools purposefully: state the missing fact each read resolves, batch compatible reads, retain evidence provenance and stop repeating an unchanged failing call. If a supported solution cannot be established, report the precise missing evidence or capability and the next useful investigation. Do not claim a solution, approval or posting that did not occur.
