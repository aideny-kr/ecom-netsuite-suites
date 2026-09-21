export interface AccessHealth {
  status: string;
  verification_status?: string | null;
  last_health_check?: string | null;
}

export function accessState(item: AccessHealth) {
  if (item.status === "disabled") return "Agent access disabled";
  if (item.status === "needs_reauth") return "Authorization expired";
  if (item.status === "refresh_required") return "Token refresh needed";
  if (item.status === "error") return item.verification_status === "partial" ? "Partially verified" : "Needs attention";
  if (item.status === "pending" || item.status === "inactive") return "Not yet verified";
  if (item.verification_status === "partial") return "Partially verified";
  if (item.verification_status === "unsupported") return "Test unavailable";
  if (item.verification_status === "error") return "Last test failed";
  if (item.verification_status === "ok") return "Verified at last test";
  return item.last_health_check ? "Active at last check" : "Not yet verified";
}

export function systemKey(provider: string, serverUrl?: string) {
  if (provider === "custom") {
    try {
      const url = new URL(serverUrl || "");
      return `${url.pathname.replace(/\/$/, "") === "/api/metabase-mcp" ? "metabase" : "custom"}:${url.host}`;
    } catch { return "custom"; }
  }
  return provider.replace(/_mcp$/, "");
}
