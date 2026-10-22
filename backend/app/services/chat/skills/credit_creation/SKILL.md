---
Name: New Credit
Description: Create a credit memo for an order whose NetSuite posting exceeds the finalized source and no existing credit covers the difference. You propose the lines; the server accepts them only when the order then equals the source.
Triggers:
  - /credit-creation
---

# New credit for an order NetSuite over-posts

Use this when NetSuite's invoice, less every credit already applied to it, is more than the finalized source charged, and nothing in NetSuite accounts for the difference yet. Typical causes:
- an order adjustment or discount that never reached NetSuite, for example a reseller discount or a price match;
- tax NetSuite charged that the source did not, which the credit reverses through the tax-refund item.

1. **Read the evidence.** Use the accounting evidence for the order's invoice, every credit and refund, and the source's adjustments. If a credit already covers the difference, NetSuite is right: explain that and propose nothing. Never create a second credit beside an existing one, except for the zero-total correction below.
2. **Get the amount from the source.** The credit is what the order's posted total exceeds the source total by. Its tax part, if any, is what posted tax exceeds source tax by. Use source figures only. Never compute tax from a rate, and never round.
3. **Choose items.** For a commercial adjustment, use the item the team books such adjustments with. Recent credits for the same cause are the evidence; Framework Inc uses Sales Adjustments (1471). For a tax part, use the subsidiary's configured tax-refund item. The tool refuses inventory items and any other item posting to a tax account, and names the reason.
4. **Propose** with `transaction_ops_propose_credit`: the lines (item and amount) and a short memo reason, such as the adjustment's label. The server puts the order number first in the memo, applies the credit to the order's one invoice, and stamps an idempotency key.
5. **If it is refused, act on the code.**
   - `outcome_does_not_match_source` returns the before and required figures: correct the lines and propose again.
   - `no_difference` means NetSuite is already right.
   - Report configuration and evidence refusals to the user, and do not work around them. These include a period lock, foreign currency, more than one invoice, and a credit naming the order that is neither applied to its invoice nor refunded through the order's own refunds.
6. **When it is accepted, display the approval card.** Call the returned tool with its exact params. Explain in words what the credit corrects and that the invoice and sales order are unchanged. The card shows the exact amounts, so do not restate them.
7. **Report the outcome from the server.** After approval, the server finds the credit by its idempotency key, compares its lines, GL, memo and application, and rechecks the order. Report the correction as verified only when the server says so.

**Credit for an invoice that is already paid.** When the invoice is paid in full and Solidus owes the customer the difference (its payment state is `credit_owed`, paid by ACH or wire, nothing refunded yet), propose the credit as usual. The server leaves it on the customer's account instead of applying it. Finance then contacts the customer, refunds by ACH or wire, and records the customer refund by hand. If the customer paid through Stripe, the server refuses (`invoice_paid_by_card`): Solidus must record the refund, and the credit and refund together are not supported yet. Report it.

**Zero-total correction when an existing credit's period is locked.** The gross amount and refunds already reconcile, but an existing credit booked the tax part of a refund to a sales-return account, and its period is closed, so it cannot be reallocated. Propose a credit whose lines add up to zero:
- one line on the configured tax-refund item, for the tax part;
- one equal negative line on the existing credit's item.

It posts in the open period, moves that amount from the return account to the tax account, and is applied to nothing. The server allows this shape only: positive lines on tax-refund items, negative lines on accounts the order's existing credits debited (never more than they debited there), and a total of exactly zero. It checks the outcome exactly as for any other credit.

Do not use this when NetSuite posts less than the source, for refunds that still need booking (a return or a customer refund), or when an existing credit posts to the wrong account in an open period (that is `credit_reallocation`).
