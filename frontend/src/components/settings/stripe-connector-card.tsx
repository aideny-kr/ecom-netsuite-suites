"use client";

import { useState } from "react";
import {
  useStripeStatus,
  useTestStripeConnection,
  useConnectStripe,
  useDisconnectStripe,
  useTriggerStripeSync,
} from "@/hooks/use-stripe-connector";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import {
  Zap,
  RefreshCw,
  FlaskConical,
  Trash2,
  Loader2,
  CheckCircle2,
  AlertCircle,
  XCircle,
} from "lucide-react";

function StatusDot({ status }: { status: string }) {
  const color =
    status === "online"
      ? "bg-green-500"
      : status === "needs_reauth"
        ? "bg-orange-500"
        : status === "offline"
          ? "bg-red-500"
          : "bg-gray-400";
  return <span className={`inline-block h-2 w-2 rounded-full ${color}`} />;
}

function StatusLabel({ status }: { status: string }) {
  const labels: Record<string, string> = {
    online: "Connected",
    offline: "Offline",
    needs_reauth: "Needs Reauth",
    not_configured: "Not configured",
  };
  return <span className="text-[13px] text-muted-foreground">{labels[status] || status}</span>;
}

function StatBox({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border bg-muted/30 px-3 py-2 text-center">
      <div className="text-[13px] text-muted-foreground">{label}</div>
      <div className="text-[15px] font-medium">{value}</div>
    </div>
  );
}

function formatDate(iso: string | null): string {
  if (!iso) return "Never";
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function StripeConnectorCard() {
  const { data: status, isLoading } = useStripeStatus();
  const testMutation = useTestStripeConnection();
  const connectMutation = useConnectStripe();
  const disconnectMutation = useDisconnectStripe();
  const syncMutation = useTriggerStripeSync();
  const { toast } = useToast();

  const [apiKey, setApiKey] = useState("");
  const [showDialog, setShowDialog] = useState(false);
  const [testResult, setTestResult] = useState<{
    success: boolean;
    name?: string;
    error?: string;
  } | null>(null);

  if (isLoading) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft animate-pulse">
        <div className="h-6 w-48 bg-muted rounded" />
        <div className="mt-3 h-4 w-64 bg-muted rounded" />
      </div>
    );
  }

  // Not connected state
  if (!status?.connected) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Zap className="h-4 w-4 text-violet-500" />
            <h3 className="text-[15px] font-semibold">Stripe Connector</h3>
          </div>
          <span className="text-[13px] text-muted-foreground">Not connected</span>
        </div>
        <p className="mt-2 text-[13px] text-muted-foreground">
          Connect your Stripe account to sync payout data for bank reconciliation.
        </p>
        <AlertDialog open={showDialog} onOpenChange={setShowDialog}>
          <AlertDialogTrigger asChild>
            <Button variant="outline" size="sm" className="mt-3">
              + Connect Stripe
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Connect Stripe</AlertDialogTitle>
              <AlertDialogDescription>
                Enter your Stripe Secret Key to sync payout data. The key is encrypted and stored
                securely per-tenant.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <div className="space-y-3 py-2">
              <div>
                <Label className="text-[13px]">Stripe Secret Key</Label>
                <Input
                  type="password"
                  placeholder="sk_live_..."
                  value={apiKey}
                  onChange={(e) => {
                    setApiKey(e.target.value);
                    setTestResult(null);
                  }}
                  className="mt-1 font-mono text-[13px]"
                />
              </div>
              {testResult && (
                <div
                  className={`flex items-center gap-2 rounded-md p-2 text-[13px] ${
                    testResult.success
                      ? "bg-green-50 text-green-700"
                      : "bg-red-50 text-red-700"
                  }`}
                >
                  {testResult.success ? (
                    <>
                      <CheckCircle2 className="h-3.5 w-3.5" />
                      Connected to {testResult.name || "Stripe account"}
                    </>
                  ) : (
                    <>
                      <XCircle className="h-3.5 w-3.5" />
                      {testResult.error || "Connection failed"}
                    </>
                  )}
                </div>
              )}
            </div>
            <AlertDialogFooter>
              <AlertDialogCancel onClick={() => { setApiKey(""); setTestResult(null); }}>
                Cancel
              </AlertDialogCancel>
              <Button
                variant="outline"
                size="sm"
                onClick={async () => {
                  const r = await testMutation.mutateAsync({ api_key: apiKey });
                  setTestResult({
                    success: r.success,
                    name: r.account_name || undefined,
                    error: r.error || undefined,
                  });
                }}
                disabled={!apiKey || testMutation.isPending}
              >
                {testMutation.isPending ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <FlaskConical className="mr-1.5 h-3.5 w-3.5" />
                )}
                Test
              </Button>
              <Button
                size="sm"
                onClick={async () => {
                  try {
                    await connectMutation.mutateAsync({ api_key: apiKey });
                    setShowDialog(false);
                    setApiKey("");
                    setTestResult(null);
                    toast({ title: "Stripe connected", description: "Initial sync triggered." });
                  } catch (e: unknown) {
                    toast({
                      title: "Connection failed",
                      description: e instanceof Error ? e.message : "Unknown error",
                      variant: "destructive",
                    });
                  }
                }}
                disabled={!apiKey || connectMutation.isPending}
              >
                {connectMutation.isPending && (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                )}
                Test & Save
              </Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    );
  }

  // Connected state
  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Zap className="h-4 w-4 text-violet-500" />
          <h3 className="text-[15px] font-semibold">Stripe Connector</h3>
        </div>
        <div className="flex items-center gap-1.5">
          <StatusDot status={status.status} />
          <StatusLabel status={status.status} />
        </div>
      </div>

      {/* API key hint */}
      <div className="mt-3 flex items-center gap-2 text-[13px] text-muted-foreground">
        <span>API Key</span>
        <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[12px]">
          sk_live_{status.api_key_hint}
        </code>
      </div>

      {status.error_message && (
        <div className="mt-2 flex items-center gap-1.5 text-[13px] text-red-600">
          <AlertCircle className="h-3.5 w-3.5" />
          {status.error_message}
        </div>
      )}

      {/* Stats */}
      <div className="mt-3 grid grid-cols-3 gap-2">
        <StatBox label="Payouts Synced" value={status.payouts_count.toLocaleString()} />
        <StatBox label="Last Sync" value={formatDate(status.last_sync_at)} />
        <StatBox label="Payout Lines" value={status.payout_lines_count.toLocaleString()} />
      </div>

      {/* Actions */}
      <div className="mt-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={async () => {
              if (!status.connection_id) return;
              try {
                await syncMutation.mutateAsync(status.connection_id);
                toast({ title: "Sync triggered", description: "Stripe sync is running." });
              } catch {
                toast({ title: "Sync failed", variant: "destructive" });
              }
            }}
            disabled={syncMutation.isPending}
          >
            {syncMutation.isPending ? (
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            )}
            Sync Now
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={async () => {
              const r = await testMutation.mutateAsync({
                api_key: "", // backend uses stored key for status check
              });
              toast({
                title: r.success ? "Connection healthy" : "Connection issue",
                description: r.success
                  ? `Connected to ${r.account_name || "Stripe"}`
                  : r.error || "Test failed",
                variant: r.success ? "default" : "destructive",
              });
            }}
            disabled={testMutation.isPending}
          >
            {testMutation.isPending ? (
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
            ) : (
              <FlaskConical className="mr-1.5 h-3.5 w-3.5" />
            )}
            Test
          </Button>
        </div>
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button variant="ghost" size="sm" className="text-red-500 hover:text-red-600">
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Remove Stripe Connection</AlertDialogTitle>
              <AlertDialogDescription>
                This will remove the Stripe API key. Synced payout data will be preserved.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                className="bg-red-600 hover:bg-red-700"
                onClick={async () => {
                  await disconnectMutation.mutateAsync();
                  toast({ title: "Stripe disconnected" });
                }}
              >
                Remove
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </div>
  );
}
