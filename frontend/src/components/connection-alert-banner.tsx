"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, X } from "lucide-react";
import { apiClient } from "@/lib/api-client";
import { useConnectionAlerts } from "@/hooks/use-connection-alerts";

export function ConnectionAlertBanner() {
  const { data: alerts } = useConnectionAlerts();
  const queryClient = useQueryClient();

  const dismiss = useMutation({
    mutationFn: (id: string) =>
      apiClient.post(`/api/v1/connection-alerts/${id}/dismiss`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connection-alerts"] });
    },
  });

  if (!alerts?.length) return null;

  const alert = alerts[0];
  const label = alert.connection_type === "mcp" ? "MCP" : "NetSuite";

  return (
    <div className="bg-destructive/10 border-b border-destructive/20 px-4 py-3">
      <div className="flex items-center gap-3 max-w-screen-xl mx-auto">
        <AlertTriangle className="h-4 w-4 text-destructive shrink-0" />
        <p className="text-[13px] text-destructive flex-1">
          {label} connection lost — OAuth token refresh failed. Re-authorize in
          Settings → Connections.
          {alerts.length > 1 && (
            <span className="text-destructive/70">
              {" "}
              (+{alerts.length - 1} more)
            </span>
          )}
        </p>
        <button
          onClick={() => dismiss.mutate(alert.id)}
          className="text-destructive/60 hover:text-destructive transition-colors"
          aria-label="Dismiss alert"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
