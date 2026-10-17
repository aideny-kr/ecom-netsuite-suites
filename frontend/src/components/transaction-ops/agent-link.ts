export function investigationChatLink(runId: string) {
  const compose = `Help me resolve the issues in this investigation: /transaction-operations/runs/${runId}. Explain the order total, VAT/tax, and refund findings, identify missing evidence, and propose supported next steps for my approval.`;
  return `/chat?${new URLSearchParams({ compose, new_session: "true" }).toString()}`;
}

// One order from the orders page, bound to that order: its own saved case when it has one,
// otherwise the investigation just started for it through the order's endpoint (staging
// 2026-10-16; packet review round 2 on #413, F5/F6). Never a lookup by number alone.
export function orderChatLink(
  orderNumber: string,
  ref: { caseId: string } | { runId: string },
) {
  const scope =
    "caseId" in ref
      ? `Use its saved reconciliation case ${ref.caseId}`
      : `A fresh investigation of it has started: /transaction-operations/runs/${ref.runId}. Use the case it reports for this order`;
  const compose = `Help me resolve order ${orderNumber}. ${scope}: explain the order total, VAT/tax, and refund differences, identify the cause and any missing evidence, and propose a supported fix for my approval.`;
  return `/chat?${new URLSearchParams({ compose, new_session: "true" }).toString()}`;
}
