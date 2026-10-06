export const TRANSACTION_VIEWS = ["records", "reconcile", "cases", "approvals", "history"] as const;
export type TransactionView = typeof TRANSACTION_VIEWS[number];

export function parseTransactionView(value: string | null): TransactionView | undefined {
  return TRANSACTION_VIEWS.find(view => view === value);
}

export function isTransactionPath(path: string) {
  return path === "/transactions" || path.startsWith("/tables/") || path === "/reconciliation" || path === "/transaction-operations" || path.startsWith("/transaction-operations/");
}
