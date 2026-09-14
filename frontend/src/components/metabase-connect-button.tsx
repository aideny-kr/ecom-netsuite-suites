"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { Button } from "@/components/ui/button";

const API_ORIGIN = new URL(process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").origin;
type Attempt = { popup: Window; state?: string; cleanup: () => void; finishing: boolean };

export function MetabaseConnectButton({ connectorId, reconnect = false, disabled = false }: {
  connectorId: string; reconnect?: boolean; disabled?: boolean;
}) {
  const queryClient = useQueryClient();
  const active = useRef<Attempt | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => () => {
    active.current?.cleanup();
    active.current?.popup.close();
    active.current = null;
  }, [connectorId]);

  async function connect() {
    if (active.current) return;
    setError(null);
    // Open during the click, before awaiting the authenticated request.
    const popup = window.open("about:blank", "", "width=600,height=720,popup=yes");
    if (!popup) { setError("Allow popups for this site, then connect with Metabase again."); return; }
    setBusy(true);
    const attempt: Attempt = { popup, cleanup: () => {}, finishing: false };
    active.current = attempt;
    function end(message?: string) {
      if (active.current !== attempt) return;
      attempt.cleanup();
      popup!.close();
      active.current = null;
      setBusy(false);
      if (message) setError(message);
    }
    async function receive(event: MessageEvent) {
      if (active.current !== attempt || attempt.finishing || event.origin !== API_ORIGIN
        || event.source !== popup || !attempt.state || event.data?.state !== attempt.state
        || event.data?.type !== "METABASE_OAUTH_RESULT") return;
      attempt.finishing = true;
      attempt.cleanup();
      popup!.close();
      try {
        const result = await apiClient.post<{ status: string; message: string }>(
          `/api/v1/mcp-connectors/${connectorId}/metabase/complete`,
          { state: attempt.state, code: event.data.code, error: event.data.error },
        );
        if (active.current !== attempt) return;
        await queryClient.invalidateQueries({ queryKey: ["mcp-connectors"] });
        end(result.status === "ok" ? undefined : result.message);
      } catch (err) { end(err instanceof Error ? err.message : "Could not finish sign-in. Try again."); }
    }
    window.addEventListener("message", receive);
    const closed = window.setInterval(() => {
      if (popup.closed && !attempt.finishing) end("Sign-in window closed. Connect with Metabase to try again.");
    }, 500);
    const expired = window.setTimeout(() => end("Sign-in expired. Connect with Metabase again."), 600_000);
    attempt.cleanup = () => {
      window.removeEventListener("message", receive);
      window.clearInterval(closed);
      window.clearTimeout(expired);
    };
    try {
      const result = await apiClient.post<{ state: string; authorize_url: string }>(
        `/api/v1/mcp-connectors/${connectorId}/metabase/authorize`, { app_origin: window.location.origin },
      );
      if (active.current !== attempt) return;
      attempt.state = result.state;
      popup.location.href = result.authorize_url;
    } catch (err) { end(err instanceof Error ? err.message : "Could not start sign-in. Try again."); }
  }

  return <div className="space-y-2">
    <Button size="sm" variant={reconnect ? "outline" : "default"} disabled={busy || disabled} onClick={() => void connect()}>
      {busy ? "Connecting…" : reconnect ? "Reconnect Metabase" : "Connect with Metabase"}
    </Button>
    {busy && <p role="status" className="text-[13px] text-muted-foreground">Finish sign-in in the Metabase window.</p>}
    {error && <p role="alert" className="text-[13px] text-destructive">{error}</p>}
  </div>;
}
