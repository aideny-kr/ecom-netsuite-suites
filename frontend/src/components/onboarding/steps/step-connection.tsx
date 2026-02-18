"use client";

import { useState, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiClient } from "@/lib/api-client";
import {
  CheckCircle2,
  Loader2,
  AlertCircle,
  Link2,
  KeyRound,
  Database,
} from "lucide-react";

type PhaseStatus = "idle" | "connecting" | "connected" | "error";

interface StepConnectionProps {
  onStepComplete: () => void;
}

export function StepConnection({ onStepComplete }: StepConnectionProps) {
  const [mcpStatus, setMcpStatus] = useState<PhaseStatus>("idle");
  const [oauthStatus, setOauthStatus] = useState<PhaseStatus>("idle");
  const [accountId, setAccountId] = useState("");
  const [clientId, setClientId] = useState("");
  const [label, setLabel] = useState("");
  const [isDiscovering, setIsDiscovering] = useState(false);
  const [metadataStatus, setMetadataStatus] = useState<
    "idle" | "running" | "done" | "error"
  >("idle");
  const [errorMessage, setErrorMessage] = useState("");
  const [isChecking, setIsChecking] = useState(true);

  // Check existing connections on mount
  useEffect(() => {
    async function checkExisting() {
      setIsChecking(true);
      try {
        // Check for existing MCP connector
        const connectors = await apiClient.get<
          Array<{ provider: string; status: string; is_enabled: boolean }>
        >("/api/v1/mcp-connectors");
        const hasActiveMcp = connectors.some(
          (c) =>
            c.provider === "netsuite_mcp" &&
            c.status === "active" &&
            c.is_enabled,
        );
        if (hasActiveMcp) setMcpStatus("connected");

        // Check for existing OAuth connection via validation
        const validation = await apiClient.get<{
          step_key: string;
          valid: boolean;
          reason?: string;
        }>("/api/v1/onboarding/checklist/connection/validate");
        if (validation.valid) {
          setMcpStatus("connected");
          setOauthStatus("connected");
        } else if (hasActiveMcp && validation.reason) {
          // MCP connected but OAuth missing
          if (!validation.reason.includes("MCP")) {
            setOauthStatus("idle");
          }
        }
      } catch {
        // Ignore — user just hasn't connected yet
      } finally {
        setIsChecking(false);
      }
    }
    checkExisting();
  }, []);

  // Listen for OAuth popup messages
  const handleMessage = useCallback((event: MessageEvent) => {
    const apiOrigin =
      process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
    if (
      event.origin !== window.location.origin &&
      event.origin !== apiOrigin
    )
      return;

    if (event.data?.type === "NETSUITE_MCP_AUTH_SUCCESS") {
      setMcpStatus("connected");
      setErrorMessage("");
    } else if (event.data?.type === "NETSUITE_MCP_AUTH_ERROR") {
      setMcpStatus("error");
      setErrorMessage(event.data.error || "MCP authentication failed");
    } else if (event.data?.type === "NETSUITE_AUTH_SUCCESS") {
      setOauthStatus("connected");
      setErrorMessage("");
    } else if (event.data?.type === "NETSUITE_AUTH_ERROR") {
      setOauthStatus("error");
      setErrorMessage(event.data.error || "OAuth authentication failed");
    }
  }, []);

  useEffect(() => {
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [handleMessage]);

  function openOAuthPopup(url: string, name: string) {
    const width = 600;
    const height = 700;
    const left = window.screenX + (window.innerWidth - width) / 2;
    const top = window.screenY + (window.innerHeight - height) / 2;
    window.open(
      url,
      name,
      `width=${width},height=${height},left=${left},top=${top},popup=yes`,
    );
  }

  async function handleConnectMcp() {
    if (!accountId || !clientId) return;
    setMcpStatus("connecting");
    setErrorMessage("");
    try {
      const params = new URLSearchParams({
        account_id: accountId,
        client_id: clientId,
        label: label || `NetSuite MCP ${accountId}`,
      });
      const data = await apiClient.get<{
        authorize_url: string;
        state: string;
      }>(`/api/v1/onboarding/netsuite-mcp/authorize?${params}`);
      openOAuthPopup(data.authorize_url, "netsuite_mcp_oauth");
    } catch (err) {
      setMcpStatus("error");
      setErrorMessage(
        err instanceof Error ? err.message : "Failed to start MCP authorization",
      );
    }
  }

  async function handleConnectOauth() {
    if (!accountId) return;
    setOauthStatus("connecting");
    setErrorMessage("");
    try {
      const params = new URLSearchParams({ account_id: accountId });
      const data = await apiClient.get<{
        authorize_url: string;
        state: string;
      }>(`/api/v1/onboarding/netsuite-oauth/authorize?${params}`);
      openOAuthPopup(data.authorize_url, "netsuite_oauth");
    } catch (err) {
      setOauthStatus("error");
      setErrorMessage(
        err instanceof Error
          ? err.message
          : "Failed to start OAuth authorization",
      );
    }
  }

  async function handleComplete() {
    try {
      setIsDiscovering(true);
      setErrorMessage("");

      // Step 1: Run table/schema discovery (existing)
      const discovery = await apiClient.post<{
        status: string;
        summary?: Record<string, unknown>;
        snapshot_profile_id?: string;
        snapshot_version?: number;
      }>("/api/v1/onboarding/discover");

      if (discovery.status !== "completed") {
        throw new Error("NetSuite discovery did not complete successfully");
      }

      // Step 2: Kick off metadata discovery (custom fields, org hierarchy)
      // This runs in the background via Celery — don't block on it
      setMetadataStatus("running");
      try {
        await apiClient.post("/api/v1/netsuite/metadata/discover");
        setMetadataStatus("done");
      } catch {
        // Non-blocking — metadata discovery is a nice-to-have during onboarding
        // It will also be triggered by confirm_profile on the backend
        setMetadataStatus("error");
      }

      await apiClient.post("/api/v1/onboarding/checklist/connection/complete", {
        metadata: {
          discovery_status: discovery.status,
          summary: discovery.summary || null,
          snapshot_profile_id: discovery.snapshot_profile_id || null,
          snapshot_version: discovery.snapshot_version || null,
        },
      });
      onStepComplete();
    } catch (err: unknown) {
      setErrorMessage(
        err instanceof Error ? err.message : "Failed to complete step",
      );
    } finally {
      setIsDiscovering(false);
    }
  }

  const bothConnected =
    mcpStatus === "connected" && oauthStatus === "connected";

  if (isChecking) {
    return (
      <div className="space-y-6 p-6">
        <div className="rounded-lg border bg-muted/30 p-6 text-center">
          <Loader2 className="h-12 w-12 text-muted-foreground mx-auto mb-3 animate-spin" />
          <h3 className="font-medium">Checking connections...</h3>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4 p-6">
      {/* Phase A: MCP Connector */}
      <div className="rounded-lg border p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Link2 className="h-4 w-4 text-muted-foreground" />
            <h4 className="font-medium text-sm">Phase A: MCP Connector</h4>
          </div>
          <StatusBadge status={mcpStatus} />
        </div>
        {mcpStatus !== "connected" && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label htmlFor="ob-account-id" className="text-xs">
                  Account ID
                </Label>
                <Input
                  id="ob-account-id"
                  placeholder="e.g., TSTDRV1234567"
                  value={accountId}
                  onChange={(e) => setAccountId(e.target.value)}
                  className="h-8 text-xs"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="ob-client-id" className="text-xs">
                  Client ID
                </Label>
                <Input
                  id="ob-client-id"
                  placeholder="OAuth 2.0 Client ID"
                  value={clientId}
                  onChange={(e) => setClientId(e.target.value)}
                  className="h-8 text-xs"
                />
              </div>
            </div>
            <div className="space-y-1">
              <Label htmlFor="ob-label" className="text-xs">
                Label (optional)
              </Label>
              <Input
                id="ob-label"
                placeholder="e.g., Production NetSuite"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                className="h-8 text-xs"
              />
            </div>
            <Button
              size="sm"
              onClick={handleConnectMcp}
              disabled={!accountId || !clientId || mcpStatus === "connecting"}
            >
              {mcpStatus === "connecting" ? (
                <>
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                  Authorizing...
                </>
              ) : (
                "Connect MCP via OAuth"
              )}
            </Button>
          </div>
        )}
      </div>

      {/* Phase B: OAuth API Tokens */}
      <div className="rounded-lg border p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-muted-foreground" />
            <h4 className="font-medium text-sm">
              Phase B: OAuth API Tokens
            </h4>
          </div>
          <StatusBadge status={oauthStatus} />
        </div>
        {oauthStatus !== "connected" && (
          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="ob-oauth-account" className="text-xs">
                Account ID
              </Label>
              <Input
                id="ob-oauth-account"
                placeholder="Pre-filled from Phase A"
                value={accountId}
                onChange={(e) => setAccountId(e.target.value)}
                className="h-8 text-xs"
              />
              <p className="text-[10px] text-muted-foreground">
                Uses the global NetSuite OAuth client configured for this
                platform.
              </p>
            </div>
            <Button
              size="sm"
              onClick={handleConnectOauth}
              disabled={!accountId || oauthStatus === "connecting"}
            >
              {oauthStatus === "connecting" ? (
                <>
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                  Authorizing...
                </>
              ) : (
                "Connect OAuth API"
              )}
            </Button>
          </div>
        )}
      </div>

      {/* Error message */}
      {errorMessage && (
        <div className="flex items-start gap-2 rounded-md bg-destructive/10 px-3 py-2 text-destructive text-xs">
          <AlertCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          <span>{errorMessage}</span>
        </div>
      )}

      {/* Continue button */}
      <Button
        onClick={handleComplete}
        disabled={!bothConnected || isDiscovering}
        className="w-full"
      >
        {isDiscovering ? (
          <>
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            Running Discovery...
          </>
        ) : (
          "Run Discovery & Continue"
        )}
      </Button>

      {!bothConnected && (
        <p className="text-xs text-muted-foreground text-center">
          Both MCP and OAuth connections are required to continue.
        </p>
      )}

      {/* Metadata discovery status indicator */}
      {metadataStatus === "running" && (
        <div className="flex items-center gap-2 rounded-md bg-blue-50 px-3 py-2 text-xs text-blue-700">
          <Database className="h-3.5 w-3.5 animate-pulse" />
          Discovering custom fields and org hierarchy in the background...
        </div>
      )}
      {metadataStatus === "done" && (
        <div className="flex items-center gap-2 rounded-md bg-green-50 px-3 py-2 text-xs text-green-700">
          <Database className="h-3.5 w-3.5" />
          Metadata discovery queued — custom fields will be available in chat
          shortly.
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: PhaseStatus }) {
  switch (status) {
    case "connected":
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-700">
          <CheckCircle2 className="h-3 w-3" />
          Connected
        </span>
      );
    case "connecting":
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-700">
          <Loader2 className="h-3 w-3 animate-spin" />
          Connecting
        </span>
      );
    case "error":
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700">
          <AlertCircle className="h-3 w-3" />
          Error
        </span>
      );
    default:
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
          Pending
        </span>
      );
  }
}
