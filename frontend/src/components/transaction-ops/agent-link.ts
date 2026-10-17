export function investigationChatLink(runId: string) {
  const compose = `Help me resolve the issues in this investigation: /transaction-operations/runs/${runId}. Explain the order total, VAT/tax, and refund findings, identify missing evidence, and propose supported next steps for my approval.`;
  return `/chat?${new URLSearchParams({ compose, new_session: "true" }).toString()}`;
}

// One order from the orders page: the agent finds its saved case from the order number
// (transaction_ops.status order_reference), as a person or Claude with NetSuite MCP would.
export function orderChatLink(orderNumber: string) {
  const compose = `Help me resolve order ${orderNumber}. Find its saved reconciliation case, explain the order total, VAT/tax, and refund differences, identify the cause and any missing evidence, and propose a supported fix for my approval.`;
  return `/chat?${new URLSearchParams({ compose, new_session: "true" }).toString()}`;
}
