import type { ColumnDef } from "@tanstack/react-table";
import { transactionAmount, transactionDate } from "./format";

type Field = readonly [string, string, ("money" | "date")?, string?];
const fields: Record<string, readonly Field[]> = {
  orders: [["order_number", "Order number"], ["source_created_at", "Order date (UTC)", "date"],
    ["currency", "Currency"], ["total_amount", "Order total", "money"], ["tax_amount", "VAT / tax", "money"], ["status", "Order status"]],
  payments: [["source_id", "Payment"], ["currency", "Currency"], ["amount", "Amount", "money"],
    ["payment_method", "Method"], ["status", "Status"]],
  refunds: [["source_id", "Refund"], ["currency", "Currency"], ["amount", "Amount", "money"],
    ["reason", "Reason"], ["status", "Status"]],
  payouts: [["source_id", "Payout"], ["arrival_date", "Arrival date", "date"], ["currency", "Currency"],
    ["amount", "Gross", "money"], ["fee_amount", "Fees", "money"], ["net_amount", "Net", "money"], ["status", "Status"]],
  payout_lines: [["source_id", "Transaction"], ["related_order_id", "Order reference"], ["line_type", "Type"],
    ["currency", "Currency"], ["amount", "Gross", "money"], ["fee", "Fees", "money"], ["net", "Net", "money"]],
  disputes: [["source_id", "Dispute"], ["related_order_id", "Order reference"], ["currency", "Currency"],
    ["amount", "Amount", "money"], ["reason", "Reason"], ["status", "Status"]],
  netsuite_postings: [["netsuite_internal_id", "NetSuite record"], ["transaction_date", "Date", "date"],
    ["record_type", "Type"], ["currency", "Base currency"], ["amount", "Base amount", "money"],
    ["transaction_currency", "Transaction currency"], ["foreign_amount", "Transaction amount", "money", "transaction_currency"]],
};

export function transactionColumns(tableName: string): ColumnDef<Record<string, unknown>, unknown>[] {
  return (fields[tableName] || []).map(([key, label, format, currencyKey]) => ({
    id: key, accessorKey: key,
    header: format === "money" ? () => <span className="block text-right">{label}</span> : label,
    cell: ({ getValue, row }) => {
      const value = getValue();
      if (format === "money") return <span className="block text-right font-mono tabular-nums">{transactionAmount(value, row.original[currencyKey || "currency"])}</span>;
      if (format === "date") return transactionDate(value);
      return <span className="block max-w-[22rem] truncate" title={value == null ? undefined : String(value)}>{value == null ? "—" : String(value)}</span>;
    },
    enableSorting: true,
  }));
}
