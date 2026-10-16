---
name: unbooked-solidus-adjustment
version: 1
status: approved
approved_by: Aiden
approved_at: "2026-10-06"
cause: An order adjustment in Solidus never reached NetSuite, so NetSuite bills more than Solidus charged.
diagnosis: needs_credit_memo
action: create
checks:
  - solidus_below_netsuite
  - adjustment_equals_difference
  - chain_complete
  - one_invoice_from_the_order
  - no_credit_from_the_invoice
change:
  record_type: creditMemo
  created_from: invoice
  lines:
    - item: "1471"  # Sales Adjustments, which posts to 40050 Sales adjustments
      amount: difference
  memo: "{order} {adjustment_label}"
verify:
  - The credit memo exists, was created from the invoice, and its total equals the difference.
  - The case's order-total difference is zero on the next scan.
evidence:
  - CM11788 for R000227174 (2026-09-14) — created from INV363632; line item 1471 Sales Adjustments → 40050 Sales adjustments, 4.82; memo "R000227174 Fix Order Status"; the invoice's shipping and tax lines carried over at zero.
  - CM11948 for R434156410 (2026-09-25) — created from INV370292; line item 1471 Sales Adjustments → 40050 Sales adjustments, 262.64; memo "R434156410 Reseller Discount".
---
Use this when Solidus shows an order adjustment that NetSuite never received: the
adjustment equals the order-total difference, NetSuite is higher, and nothing has been
credited against the invoice yet.

The credit memo is created from the order's invoice (so it carries the customer,
subsidiary and currency), with one line on item 1471 for the difference. Lines the
transform copies from the invoice stay at zero. The memo is the order number and the
adjustment's label, as the team books it.

Do not use it when a credit memo already exists against the invoice (the order may
already be right: explain and close), when several adjustments could explain the
difference, or when the chain could not be read completely.
