import type { WriteConfirmationData } from "@/lib/types";

export const creditCard: WriteConfirmationData = {
  type: "write_confirmation", mutation_type: "create", record_type: "creditmemo", record_id: null,
  proposed_fields: { entity: { id: "70" }, externalId: "stable", autoApply: false,
    item: { items: [{ item: { id: "50" }, amount: 5, isTaxable: false }] },
    apply: { items: [{ doc: { id: "20" }, apply: true, amount: 5 }] }, tranDate: "2026-09-10" },
  current_record: null, tool_name: "native_create", tool_input: {}, confirmation_token: "signed-credit", status: "pending",
  target_environment: "PRODUCTION",
  accounting_review: {
    kind: "sales_adjustment_credit", order_reference: "R123456789", record_id: "20", case_id: "case",
    before: { total: "106.00", tranId: "INV20", subsidiary: { refName: "Example company" } },
    source: { total: "101.00" }, proposed_fields: { tranDate: "2026-09-10", autoApply: false },
    expected_after: { credit_total: "5.00", credit_tax: "0.00", net_invoice_total: "101.00", invoice_total: "106.00", invoice_tax: "6.00", invoice_remaining: "0.00", remaining_variance: "0.00" },
    profile: { currency: "USD", item_id: "50", adjustment_account_id: "500", source_adjustment_label: "Reseller Adjustment 5%" },
    scope: { netsuite_account_id: "123", subsidiary_id: "1" }, period: { id: "171", periodName: "Sep 2026" },
    ar_account: "100", sales_adjustment_account: "500", accounting_book: "1", approval_basis: "Finance confirms the non-taxable treatment and current posting date.",
  },
};
