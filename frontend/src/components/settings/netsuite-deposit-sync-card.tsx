"use client";

import {
  useDepositSyncStatus,
  useTriggerDepositSync,
} from "@/hooks/use-deposit-sync";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import {
  FileText,
  RefreshCw,
  Loader2,
  AlertCircle,
  Link2,
} from "lucide-react";

function StatusDot({ status }: { status: string }) {
  const color =
    status === "active"
      ? "bg-green-500"
      : status === "sync_failed"
        ? "bg-red-500"
        : "bg-gray-400";
  return <span className={`inline-block h-2 w-2 rounded-full ${color}`} />;
}

function StatusLabel({ status }: { status: string }) {
  const labels: Record<string, string> = {
    active: "Active",
    no_connection: "No Connection",
    sync_failed: "Sync Failed",
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

export function NetSuiteDepositSyncCard() {
  const { data: status, isLoading } = useDepositSyncStatus();
  const syncMutation = useTriggerDepositSync();
  const { toast } = useToast();

  if (isLoading) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft animate-pulse">
        <div className="h-6 w-48 bg-muted rounded" />
        <div className="mt-3 h-4 w-64 bg-muted rounded" />
      </div>
    );
  }

  // No NetSuite connection
  if (!status?.active) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <FileText className="h-4 w-4 text-blue-500" />
            <h3 className="text-[15px] font-semibold">NetSuite Deposit Sync</h3>
          </div>
          <div className="flex items-center gap-1.5">
            <StatusDot status="no_connection" />
            <StatusLabel status="no_connection" />
          </div>
        </div>
        <p className="mt-2 text-[13px] text-muted-foreground">
          Connect a NetSuite account via OAuth (above) to sync bank deposits for reconciliation.
        </p>
      </div>
    );
  }

  // Active state
  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <FileText className="h-4 w-4 text-blue-500" />
          <h3 className="text-[15px] font-semibold">NetSuite Deposit Sync</h3>
        </div>
        <div className="flex items-center gap-1.5">
          <StatusDot status={status.status} />
          <StatusLabel status={status.status} />
        </div>
      </div>

      {/* Connection info */}
      <div className="mt-2 flex items-center gap-1.5 text-[13px] text-muted-foreground">
        <Link2 className="h-3 w-3" />
        Using: {status.netsuite_connection_label || "NetSuite REST"}
      </div>

      <div className="mt-1 text-[13px] text-muted-foreground">
        Record Types: Deposit, Customer Deposit
      </div>

      {status.error_message && (
        <div className="mt-2 flex items-center gap-1.5 text-[13px] text-red-600">
          <AlertCircle className="h-3.5 w-3.5" />
          {status.error_message}
        </div>
      )}

      {/* Stats */}
      <div className="mt-3 grid grid-cols-3 gap-2">
        <StatBox label="Deposits Found" value={status.deposits_count.toLocaleString()} />
        <StatBox label="Last Sync" value={formatDate(status.last_sync_at)} />
        <StatBox label="Status" value={status.status === "active" ? "Ready" : status.status} />
      </div>

      {/* Actions */}
      <div className="mt-3">
        <Button
          variant="outline"
          size="sm"
          onClick={async () => {
            try {
              const result = await syncMutation.mutateAsync();
              toast({
                title: "Deposit sync complete",
                description: `${result.records_synced} deposits synced (${result.records_new} new)`,
              });
            } catch (e: unknown) {
              toast({
                title: "Sync failed",
                description: e instanceof Error ? e.message : "Unknown error",
                variant: "destructive",
              });
            }
          }}
          disabled={syncMutation.isPending}
        >
          {syncMutation.isPending ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
          )}
          {syncMutation.isPending ? "Syncing..." : "Sync Now"}
        </Button>
      </div>
    </div>
  );
}
