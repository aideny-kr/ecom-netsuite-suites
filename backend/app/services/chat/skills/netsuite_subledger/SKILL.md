---
Name: NetSuite Subledger Investigation
Description: Trace order, invoice, payment, deposit, credit and refund relationships with account-scoped GL evidence.
Triggers:
  - /netsuite-subledger
---

# NetSuite subledger investigation

Resolve the actual account/environment, selected connectors, customer, subsidiary, currency and accounting book from the case. Never substitute a default connector or infer a record from its display number alone. Prefer the existing scoped evidence tool before ad-hoc reads.

Trace source capture/update/refund events to the native sales order, posted invoices or cash sales, credit memos, customer payments, customer deposits and deposit applications, and refund records. Inspect relevant custom integration records when discovered. Record identity, status, dates, amounts, source links and application edges. Empty results prove absence only when the query, permissions, filters and pagination establish complete coverage.

Sales orders do not establish posted revenue. A customer deposit is not proof that an invoice has been paid; inspect its application. Credits, cash refunds and deposit applications are distinct records. Follow native relationships and explicit external identities; amount similarity alone is insufficient. Do not count a credit and its refund as two reductions of the sale.

Read GL lines in the correct book and subsidiary; preserve foreign/base currency and sign conventions. Header totals repeat after line joins: aggregate at the intended grain. Inspect AR, tax, sales adjustment/revenue, customer-deposit and cash/clearing accounts as relevant. Check account and item types, posting/nonposting discounts, subsidiary membership, classifications, tax settings and actual posting-period locks.

For SuiteQL, use the current connector's verified schema and pagination. Raw status codes differ from display labels. Do not use generic SQL LIMIT or cap input rows before a financial aggregation. Start narrow after a field/permission failure; do not keep widening or repeating failed queries. Retain completeness and observed timestamps.

Source payment completion, ERP receivable application and bank/processor settlement need separate evidence. Oracle documents deposit applications as applying a customer deposit against an invoice: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N3194216.html . This describes the product, not the facts of this account.
