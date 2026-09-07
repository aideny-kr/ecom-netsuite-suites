export function investigationChatLink(runId: string) {
  const compose = `Help me resolve the issues in this investigation: /transaction-operations/runs/${runId}. Explain the order total, VAT/tax, and refund findings, identify missing evidence, and propose supported next steps for my approval.`;
  return `/chat?${new URLSearchParams({ compose, new_session: "true" }).toString()}`;
}
