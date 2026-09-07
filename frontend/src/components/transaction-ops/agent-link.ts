export function investigationChatLink(runId: string) {
  const compose = `Help me investigate transaction run ${runId}. Read its current evidence with transaction_ops.status, including any continuing investigation. Explain the order total, VAT/tax, and completed refund findings and what evidence is still missing. Work with me to resolve the issue, and link to supported proposed solutions for my exact-change approval. Never treat this message as approval to execute a financial write.`;
  return `/chat?${new URLSearchParams({ compose, new_session: "true" }).toString()}`;
}
