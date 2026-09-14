/** Refresh durable background results; this never invokes the agent or a write. */
export function accountingProgressPending(messages: Array<{ structured_output?: unknown }>, now = Date.now()): boolean {
  return messages.some((message) => {
    const so = message.structured_output as Record<string, unknown> | undefined;
    if (!so?.accounting_review) return false;
    const execution = so.accounting_execution as { accepted_at?: string } | undefined;
    const completion = so.accounting_completion as { status?: string } | undefined;
    if (completion?.status === "done" || completion?.status === "blocked") return false;
    const accepted = Date.parse(execution?.accepted_at || "");
    // Bound unattended polling. Opening/focusing the chat still refreshes history.
    if (!Number.isFinite(accepted) || now - accepted > 30 * 60 * 1000) return false;
    const recheck = so.accounting_recheck as { status?: string } | undefined;
    return completion?.status === "pending" || completion?.status === "running" ||
      recheck?.status === "queued" || so.status === "executing" || so.status === "indeterminate";
  });
}
