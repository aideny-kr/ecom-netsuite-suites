// Display metadata for the per-tenant feature-flag panel.
// MUST stay in sync with backend DEFAULT_FLAGS (feature_flag_service.py):
// a flag missing here is silently un-togglable in the admin UI.
export const FLAG_LABELS: Record<string, { label: string; description: string }> = {
  chat: { label: "Chat", description: "AI chat assistant" },
  mcp_tools: { label: "MCP Tools", description: "External MCP server connections" },
  workspace: { label: "Workspace", description: "Dev workspace & file management" },
  reconciliation: { label: "Reconciliation", description: "Payment reconciliation engine" },
  byok_ai: { label: "BYOK AI", description: "Bring your own AI API key" },
  custom_branding: { label: "Custom Branding", description: "White-label branding" },
  custom_domain: { label: "Custom Domain", description: "Custom domain mapping" },
  analytics_export: { label: "Analytics Export", description: "CSV/data export" },
  drive_rag: { label: "Drive RAG", description: "Google Drive document retrieval for chat" },
  plan_mode_enabled: { label: "Plan Mode", description: "Chat clarify-before-run planning step" },
  recon_scheduled_runs: {
    label: "Scheduled Recon Runs",
    description: "Nightly automatic reconciliation runs (read + match only)",
  },
  autonomous_recon: {
    label: "Autonomy Envelope (dry-run)",
    description: "Nightly report-only evaluation of auto-approvable recon lines",
  },
};
