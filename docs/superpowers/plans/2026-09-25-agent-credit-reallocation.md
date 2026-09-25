# Agent-authored credit reallocation, checked by outcome (2026-09-25)

ClickUp: [86bc7eebh](https://app.clickup.com/t/86bc7eebh) · Branch `feat/agent-credit-reallocation` · Tier **T2**
(mutates customer data, HITL invariant, prompt surface).

## Problem

Framework's refund credit memos book the tax part of a refund to `40100 Sales Returns & Allowances`
(item 1603) instead of the subsidiary's tax account. Reconciliation then shows a tax split: net too low,
tax too high, gross matched. The in-app agent diagnoses this correctly but cannot fix it:

1. `orchestrator.py` refuses any agent-authored card on a transaction case (`accounting_adapter_required`).
   Only hard-coded treatments can produce an accepted card.
2. The accounting skills tell it to stop: "use only a verified supported adapter", "more reads cannot
   implement a missing executor", "do not manufacture a policy from a prior example".
3. It does not know the tenant's tax-refund items. Without them, the only reversal it can see is the tax
   engine, which REST cannot edit on legacy-tax credits, so it concludes "impossible". Worse, it then
   proposes a new credit, which would double-credit a customer who has already been refunded.

Evidence, from session 70fb3697 on 09-24: R979773019 / CM11710. The agent argued against the user's
correct "switch the tax account at the CM".

## Verified examples (read-only, Framework production, 2026-09-25)

| Order | Sub | Credit | Booked | Solidus says | Correct allocation |
|---|---|---|---|---|---|
| R094649369 | 1 US Inc, USD | CM11702 (15788939) 2.80 | 1603 → 40100 2.80 | net 799.00, tax 0.00 (was 2.80); refund reason 36 "System bug" | 5005 → 210: 2.80 |
| R600526599 | 2 BV, EUR | CM11578 (15777793) 225.00 | 1603 → 40100 225.00 | total 2594.00, included VAT 450.20 (invoice 489.25) | 1603: 185.95 · 4699 → 846: 39.05 |

In both cases, invoice minus the corrected credit equals Solidus in net and tax, to the cent.

## Subsidiary rules

| | US (Framework Inc, USD) | Non-US (BV / UK / AU) |
|---|---|---|
| Tax basis in source | added on top of the price (`additional_tax_total`) | included in the price (`included_tax_total`) |
| Typical defect | tax-only refund booked entirely as a sales return | a refund that includes VAT, booked entirely as a sales return |
| Correction | move the tax part of the credit to the tax-refund item | split the credit: net stays on the credit's own item, VAT goes to the tax-refund item |
| Tax-refund item (tenant config) | 5005 → 210 | BV 4699 → 846 · UK 8769 → 882 · AU 9582 → 795 |

Items and accounts are **tenant configuration** (`refund_adjustments.tax_item_accounts` / `tax_accounts` on
the case's active config). They never appear in prompts or skills.

Active configs as of 09-25:
- Inc `1fbd034b` has 5005 → 210.
- BV `2e28cc19` has 4699 → 846.
- UK `d103451a` and AU `506653d5` have none. Their proposals are refused until the owner adds them.

Framework Inc's non-USD orders (CAD GST/HST 866, CHF VAT 905) have no configured item. They are refused.

## Design: the agent decides the treatment, the server checks the result

### Tool `transaction_ops.propose_credit_reallocation`

Inputs:
- `case_id`
- `credit_memo_id`
- `lines`: the credit's complete item lines after the change, each `{line?, item_id, amount}`. `line` is set
  for an existing line and omitted for a new one.
- `reason`: the observed cause, in one sentence.

The agent chooses which credit, which items and how much. The server reads fresh evidence and accepts
the proposal only if all of the following hold. Each refusal returns a precise code the agent can act on.

1. **Scope.** The credit belongs to the case's order: same subsidiary, customer and currency as the
   invoice, reached through the order's refund graph. The period is open: not closed, not AR-locked.
2. **Invariants.**
   - Lines sum to the credit's current total.
   - Amounts are positive at currency precision.
   - Items are either the credit's existing items or the subsidiary's configured tax-refund item. They
     must be active, and a tax item's income account must be one of the subsidiary's tax accounts.
   - Header, applications, customer refund and exchange rate are untouched: only the item sublist is sent.
   - New lines copy the tax code of the existing line. The credit's tax-engine tax stays 0.
3. **Outcome.** The order's invoices minus all of its credits, with this proposal applied, equal the
   finalized Solidus net and tax exactly. Credit lines are classified as tax by account. The source must
   be finalized and paid, and every source refund must be owned by a credit.
4. **Card.** A registered treatment `credit_line_reallocation` (family `amendment`, transport
   `mcp_record_api`) sets `accounting_correction_candidate`. The agent calls the returned exact
   `ns_updateRecord` params, and `review_for_card` binds them.
5. **Approval** runs the same checks on fresh reads (`validate_approved`).
6. **After the write,** the GL readback must equal the expected ledger (`verify_after`); the case is rechecked.

Numbers are never trusted from the model. They are either equal to the server's own arithmetic, or the
proposal is refused.

### Skill changes (`app/services/chat/skills/`)

- New `credit_reallocation` skill. The method:
  1. Read the source refund and tax basis.
  2. Read the credit's lines and GL.
  3. Compare the tax part of the refund with what the credit posted.
  4. Look up the subsidiary's configured tax-refund item.
  5. Propose the smallest amendment of the **existing** credit (never a new credit when the refund is
     already paid).
  6. Follow approval, then readback.

  It includes the US vs non-US rules above, stated generically.
- `accounting_operations` / `accounting_treatments`: where an existing credit's allocation is wrong, the next
  step is this tool, not "missing executor".

### Group (after one approved and verified correction)

- The group card re-derives each member's lines from that member's own source numbers: same shape, the
  server's arithmetic, no model call.
- Every member runs the same verifier. Ineligible members stay visible with their refusal code.
- One parent approval. The existing dispatcher applies them: 3 concurrent, stopping on the first `needs_review`.

## Unknowns to settle before any live write

- **The ns_updateRecord sublist semantics on an applied credit memo.** Can a keyed line change its item or
  amount? Is a new line appended or does it replace the sublist? Settle on the Framework sandbox (FW SB1 MCP)
  before a production card.
- UK/AU config values (from `reference_framework_refund_adjustment_profiles`) need the owner's OK to apply.

## Verification

- Unit tests with the two verified cases as fixtures:
  - exact acceptance;
  - each invariant refused;
  - a model-rounded amount refused;
  - a new-credit attempt refused.
- The existing card / approval / kernel tests are extended to the new kind.
- Agent benchmark: R094649369, R979773019 and R600526599 must produce accepted proposals with the table's
  lines (staging, real model).
- T2 multi-angle gate before merge. One sandbox write and readback, then one approved production correction,
  before any group.

## Not in scope

- Pennies and rounding. Same mechanism later, with a configured rounding item or account (account not
  chosen yet).
- Tax-engine line edits and SuiteTax.
- Framework Inc CAD/CHF.
