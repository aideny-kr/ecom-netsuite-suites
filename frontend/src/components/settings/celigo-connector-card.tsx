"use client";

import { useState } from "react";
import {
  useCeligoStatus,
  useCeligoTest,
  useCeligoConnect,
  useCeligoDisconnect,
} from "@/hooks/use-celigo";
import { usePermissions } from "@/hooks/use-permissions";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Plug,
  AlertTriangle,
  FlaskConical,
  Loader2,
  CheckCircle2,
  XCircle,
  ShieldCheck,
  Trash2,
} from "lucide-react";

const REGION_LABELS: Record<string, string> = {
  us: "United States",
  eu: "Europe",
};

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : "Unknown error";
}

export default function CeligoConnectorCard() {
  const { data: status, isLoading } = useCeligoStatus();
  const testMutation = useCeligoTest();
  const connectMutation = useCeligoConnect();
  const disconnectMutation = useCeligoDisconnect();
  const { hasPermission } = usePermissions();
  const { toast } = useToast();

  const canManage = hasPermission("connections.manage");

  const [token, setToken] = useState("");
  const [agentToken, setAgentToken] = useState("");
  const [region, setRegion] = useState<"us" | "eu">("us");
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    account_name?: string | null;
    error?: string | null;
  } | null>(null);

  const connected = !!status?.connected;

  async function handleTest() {
    try {
      const result = await testMutation.mutateAsync({ token, region });
      setTestResult(result);
      if (result.ok) {
        toast({
          title: "Connection successful",
          description: result.account_name ? `Connected to ${result.account_name}` : undefined,
        });
      } else {
        toast({
          title: "Connection failed",
          description: result.error ?? "Unknown error",
          variant: "destructive",
        });
      }
    } catch (err) {
      // Never surface the token itself — apiClient error messages come from
      // the backend's `detail` string, which never echoes the credential.
      toast({ title: "Test failed", description: errorMessage(err), variant: "destructive" });
    }
  }

  async function handleConnect() {
    try {
      const trimmedAgentToken = agentToken.trim();
      await connectMutation.mutateAsync({
        token,
        region,
        label: "Celigo",
        ...(trimmedAgentToken ? { agent_token: trimmedAgentToken } : {}),
      });
      setToken("");
      setAgentToken("");
      setTestResult(null);
      toast({ title: "Celigo connected" });
    } catch (err) {
      toast({ title: "Failed to connect", description: errorMessage(err), variant: "destructive" });
    }
  }

  async function handleDisconnect() {
    try {
      await disconnectMutation.mutateAsync();
      toast({ title: "Celigo disconnected" });
    } catch (err) {
      toast({
        title: "Failed to disconnect",
        description: errorMessage(err),
        variant: "destructive",
      });
    }
  }

  if (isLoading) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft animate-pulse">
        <div className="h-6 w-40 bg-muted rounded" />
        <div className="mt-3 h-4 w-64 bg-muted rounded" />
      </div>
    );
  }

  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Plug className="h-4 w-4 text-violet-500" />
          <h3 className="text-[15px] font-semibold">Connect Celigo</h3>
        </div>
        <Badge
          variant="outline"
          className={
            connected
              ? "text-[11px] border-green-500/50 bg-green-500/10 text-green-700 dark:text-green-400"
              : "text-[11px] text-muted-foreground"
          }
        >
          {connected ? "Connected" : "Not connected"}
        </Badge>
      </div>

      {connected ? (
        <>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                Account
              </p>
              <p className="mt-1 text-[13px] text-foreground">{status?.account_name ?? "—"}</p>
            </div>
            <div>
              <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                Region
              </p>
              <p className="mt-1 text-[13px] text-foreground">
                {(status?.region && REGION_LABELS[status.region]) || status?.region || "—"}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
            <ShieldCheck className="h-3.5 w-3.5 text-green-600" />
            Read-only connection — the agent cannot edit, run, or delete anything in Celigo.
          </div>

          <div className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
            {status?.agent_access ? (
              <>
                <CheckCircle2 className="h-3.5 w-3.5 text-green-600" />
                Agent access enabled — the assistant can read your Celigo flows and scripts.
              </>
            ) : (
              <>
                <XCircle className="h-3.5 w-3.5 text-muted-foreground" />
                Agent access not enabled — reconnect with an agent token to let the assistant
                read Celigo.
              </>
            )}
          </div>

          <div className="flex items-center justify-end">
            <Button
              variant="ghost"
              size="sm"
              className="text-destructive hover:text-destructive"
              onClick={handleDisconnect}
              disabled={disconnectMutation.isPending || !canManage}
            >
              {disconnectMutation.isPending ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <Trash2 className="mr-1.5 h-3.5 w-3.5" />
              )}
              Disconnect
            </Button>
          </div>
        </>
      ) : (
        <>
          <div className="flex gap-2 rounded-lg border border-amber-400/60 bg-amber-500/[0.05] p-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
            <p className="text-[12px] text-amber-800 dark:text-amber-300">
              Create a service token in Celigo with a Custom scope limited to read access.
              Personal access tokens expire after 90 days and will stop the sync without
              warning — service tokens don&apos;t expire.
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="celigo-token" className="text-[13px]">
              API Token
            </Label>
            <Input
              id="celigo-token"
              type="password"
              placeholder="celigo_pat_..."
              autoComplete="off"
              value={token}
              onChange={(e) => {
                setToken(e.target.value);
                setTestResult(null);
              }}
              className="font-mono text-[13px]"
            />
            <p className="text-[12px] text-muted-foreground">
              Stored encrypted. Used to read integrations, flows, and scripts.
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="celigo-region" className="text-[13px]">
              Region
            </Label>
            <select
              id="celigo-region"
              value={region}
              onChange={(e) => setRegion(e.target.value as "us" | "eu")}
              className="w-full rounded-lg border bg-background px-3 py-2 text-[13px] focus:outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="us">United States — api.integrator.io</option>
              <option value="eu">Europe — api.eu.integrator.io</option>
            </select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="celigo-agent-token" className="text-[13px]">
              Agent access <span className="text-muted-foreground font-normal">(optional)</span>
            </Label>
            <Input
              id="celigo-agent-token"
              type="password"
              placeholder="celigo_pat_..."
              autoComplete="off"
              value={agentToken}
              onChange={(e) => setAgentToken(e.target.value)}
              className="font-mono text-[13px]"
            />
            <p className="text-[12px] text-muted-foreground">
              Lets the assistant answer questions about your flows. Read-only — it cannot edit,
              run, or delete anything in Celigo.
            </p>
          </div>

          {testResult && (
            <div
              className={`rounded-lg border px-3 py-2 ${
                testResult.ok
                  ? "border-green-500/50 bg-green-500/5"
                  : "border-destructive/50 bg-destructive/5"
              }`}
            >
              <div className="flex items-center gap-1.5">
                {testResult.ok ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-green-600" />
                ) : (
                  <XCircle className="h-3.5 w-3.5 text-destructive" />
                )}
                <span className="text-[12px] font-medium">
                  {testResult.ok
                    ? testResult.account_name
                      ? `Connected to ${testResult.account_name}`
                      : "Connection successful"
                    : testResult.error ?? "Connection failed"}
                </span>
              </div>
            </div>
          )}

          <div className="flex items-center gap-2">
            <Button
              size="sm"
              onClick={handleConnect}
              disabled={!token.trim() || connectMutation.isPending || !canManage}
            >
              {connectMutation.isPending && (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              )}
              Connect
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={handleTest}
              disabled={!token.trim() || testMutation.isPending || !canManage}
            >
              {testMutation.isPending ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <FlaskConical className="mr-1.5 h-3.5 w-3.5" />
              )}
              Test connection
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
