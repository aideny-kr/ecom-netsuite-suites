---
Name: Existing Credit Reallocation
Description: Correct an existing credit memo whose lines post to the wrong account, such as refunded tax booked as a sales return. You propose the lines; the server accepts them only when the order then equals the source.
Triggers:
  - /credit-reallocation
---

# Existing credit reallocation

Use this when the gross amount and the refunds reconcile but net and tax do not, and an existing credit already represents the refund. The usual cause is that the tax part of a refund was booked to a sales-return or allowance account instead of the tax account. Posted net is then too low and posted tax too high, by the same amount.

1. **Read the evidence.** Use the accounting evidence to identify the order's invoices and every credit and refund, including each credit's lines and GL. Confirm the refund is paid and applied. Never propose a new credit, an invoice change or a journal for an amount an existing credit has already refunded: that credits the customer twice.
2. **Get the tax part of the refund from the source.** It is how much the source's tax fell below what the order still posts. Use source figures only. Never compute tax from a rate, and never round.
   - Tax added on top of the price, such as US sales tax: a tax-only refund leaves net unchanged, so the whole credit is tax.
   - Tax included in the price, such as VAT or GST: the refund contains both. The tax part is the source's change in included tax; the rest is net.
3. **Choose items.** Keep the credit's own item for the net part. Use the subsidiary's configured tax-refund item for the tax part. The tool refuses any other item and names the allowed ones. That item is account configuration, not a prior example. Recent credits that used it are useful corroboration.
4. **Propose the complete lines** with `transaction_ops_propose_credit_reallocation`.
   - Include every existing line, by its line number, plus any new line.
   - The lines must add up to the unchanged credit total.
   - For a tax-only refund on one line, change that line's item.
   - For a split, reduce the existing line to the net part and add one line for the tax part.
5. **If it is refused, act on the code.** `outcome_does_not_match_source` returns the required net and tax: correct the lines and propose again. Report configuration refusals to the user; do not work around them. These include no configured tax-refund item, a locked period and foreign currency.
6. **When it is accepted, display the approval card.** Call the returned tool with its exact params. Explain the change in words:
   - the credit now debits the tax account instead of the return account;
   - the refund, applications, invoice and sales order are unchanged.

   The card shows the exact amounts, so do not restate them.
7. **Report the outcome from the server.** After approval, the server re-reads the credit's saved lines and GL and rechecks the order. Report the correction as verified only when the server says so. Proposed, approved, executed and verified are separate states.

Do not use this for amounts that do not reconcile in gross, missing refunds, or tax charged in error on an unpaid invoice. Those need their own treatment.
