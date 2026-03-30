"use client";

import { useReconDataStatus, useTriggerReconSync, isStale } from "@/hooks/use-recon-data-status";
import { RefreshCw, Loader2, AlertCircle, CheckCircle2, AlertTriangle, Info } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";

function formatRelativeTime(iso: string | null): string {
  if (!iso) return "Never";
  const ms = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function DataFreshnessBanner() {
  const { data: status, isLoading } = useReconDataStatus();
  const syncMutation = useTriggerReconSync();
  const { toast } = useToast();

  if (isLoading || !status) return null;

  const stripeConnected = status.stripe.connected;
  const netsuiteConnected = status.netsuite.connected;
  const neitherConnected = !stripeConnected && !netsuiteConnected;
  const stripeStale = isStale(status.stripe.last_sync);
  const netsuiteStale = isStale(status.netsuite.last_sync);
  const anyStale = (stripeConnected && stripeStale) || (netsuiteConnected && netsuiteStale);
  const hasError = status.stripe.status === "error" || status.netsuite.status === "error";
  const neverSynced = stripeConnected && !status.stripe.last_sync;

  const handleSync = async () => {
    try {
      await syncMutation.mutateAsync();
      toast({ title: "Sync triggered", description: "Data sync is running." });
    } catch (e: unknown) {
      toast({
        title: "Sync failed",
        description: e instanceof Error ? e.message : "Unknown error",
        variant: "destructive",
      });
    }
  };

  // No connectors
  if (neitherConnected) {
    return (
      <div className="flex items-start gap-2.5 rounded-xl border border-muted-foreground/20 bg-muted/30 p-4">
        <Info className="h-4 w-4 text-muted-foreground mt-0.5 shrink-0" />
        <div className="text-[13px] text-muted-foreground">
          No data sources configured. Ask your admin to connect Stripe or NetSuite in{" "}
          <span className="font-medium text-foreground">Settings → Data Source Connectors</span>.
        </div>
      </div>
    );
  }

  // Connector error
  if (hasError) {
    return (
      <div className="flex items-start gap-2.5 rounded-xl border border-red-500/30 bg-red-500/5 p-4">
        <AlertCircle className="h-4 w-4 text-red-500 mt-0.5 shrink-0" />
        <div className="flex-1 text-[13px]">
          <span className="text-red-600">
            {status.stripe.error || status.netsuite.status === "error"
              ? "Connection error — contact your admin."
              : "Data source error"}
          </span>
        </div>
      </div>
    );
  }

  // Connected, never synced
  if (neverSynced) {
    return (
      <div className="flex items-center justify-between rounded-xl border border-blue-500/30 bg-blue-500/5 p-4">
        <div className="flex items-start gap-2.5">
          <Info className="h-4 w-4 text-blue-500 mt-0.5 shrink-0" />
          <div className="text-[13px] text-foreground">
            Stripe is connected but has not synced yet. Pull latest data to get started.
          </div>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={handleSync}
          disabled={syncMutation.isPending}
        >
          {syncMutation.isPending ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
          )}
          Sync Now
        </Button>
      </div>
    );
  }

  // Connected and synced — show freshness
  const staleWarning = anyStale;

  return (
    <div
      className={`flex items-center justify-between rounded-xl border p-4 ${
        staleWarning
          ? "border-amber-500/30 bg-amber-500/5"
          : "border-emerald-500/20 bg-emerald-500/5"
      }`}
    >
      <div className="flex items-start gap-2.5">
        {staleWarning ? (
          <AlertTriangle className="h-4 w-4 text-amber-500 mt-0.5 shrink-0" />
        ) : (
          <CheckCircle2 className="h-4 w-4 text-emerald-500 mt-0.5 shrink-0" />
        )}
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[13px]">
          {stripeConnected && (
            <span className="text-foreground">
              Stripe: {status.stripe.payout_count?.toLocaleString() || 0} payouts
              <span className="text-muted-foreground">
                {" "}· {formatRelativeTime(status.stripe.last_sync)}
              </span>
            </span>
          )}
          {netsuiteConnected && (
            <span className="text-foreground">
              NetSuite: {status.netsuite.deposit_count?.toLocaleString() || 0} deposits
              <span className="text-muted-foreground">
                {" "}· {formatRelativeTime(status.netsuite.last_sync)}
              </span>
            </span>
          )}
          {staleWarning && (
            <span className="text-amber-600 text-[12px]">Data is over 24 hours old</span>
          )}
        </div>
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={handleSync}
        disabled={syncMutation.isPending}
      >
        {syncMutation.isPending ? (
          <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
        ) : (
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
        )}
        Refresh Data
      </Button>
    </div>
  );
}
