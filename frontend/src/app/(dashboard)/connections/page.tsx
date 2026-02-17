"use client";

import { useConnections, useDeleteConnection } from "@/hooks/use-connections";
import { AddConnectionDialog } from "@/components/add-connection-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import { Trash2, Plug, ShoppingBag, CreditCard, FileSpreadsheet } from "lucide-react";

const providerMeta: Record<string, { icon: typeof Plug; color: string; bg: string }> = {
  shopify: { icon: ShoppingBag, color: "text-green-600", bg: "bg-green-50" },
  stripe: { icon: CreditCard, color: "text-violet-600", bg: "bg-violet-50" },
  netsuite: { icon: FileSpreadsheet, color: "text-blue-600", bg: "bg-blue-50" },
};

export default function ConnectionsPage() {
  const { data: connections, isLoading } = useConnections();
  const deleteConnection = useDeleteConnection();
  const { toast } = useToast();

  async function handleDelete(id: string) {
    try {
      await deleteConnection.mutateAsync(id);
      toast({ title: "Connection deleted" });
    } catch (err) {
      toast({
        title: "Failed to delete connection",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">Connections</h2>
          <p className="mt-1 text-[15px] text-muted-foreground">
            Manage your platform integrations
          </p>
        </div>
        <AddConnectionDialog />
      </div>

      {isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-[140px] rounded-xl" />
          ))}
        </div>
      ) : !connections?.length ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed bg-card py-16">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-muted">
            <Plug className="h-6 w-6 text-muted-foreground" />
          </div>
          <p className="mt-4 text-[15px] font-medium text-foreground">
            No connections yet
          </p>
          <p className="mt-1 mb-5 text-[13px] text-muted-foreground">
            Add your first integration to get started.
          </p>
          <AddConnectionDialog />
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {connections.map((conn) => {
            const meta = providerMeta[conn.provider] || {
              icon: Plug,
              color: "text-muted-foreground",
              bg: "bg-muted",
            };
            const ProviderIcon = meta.icon;

            return (
              <div
                key={conn.id}
                className="group rounded-xl border bg-card p-5 shadow-soft transition-all duration-200 hover:shadow-soft-md"
              >
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-3">
                    <div
                      className={`flex h-10 w-10 items-center justify-center rounded-lg ${meta.bg}`}
                    >
                      <ProviderIcon className={`h-5 w-5 ${meta.color}`} />
                    </div>
                    <div>
                      <p className="text-[15px] font-semibold text-foreground">
                        {conn.label}
                      </p>
                      <p className="text-[13px] capitalize text-muted-foreground">
                        {conn.provider}
                      </p>
                    </div>
                  </div>
                  <Badge
                    variant={
                      conn.status === "active"
                        ? "default"
                        : conn.status === "error"
                          ? "destructive"
                          : "secondary"
                    }
                    className="text-[11px]"
                  >
                    {conn.status}
                  </Badge>
                </div>
                <div className="mt-4 flex items-center justify-between border-t pt-3">
                  <p className="text-[12px] text-muted-foreground">
                    {conn.last_sync_at
                      ? `Last sync: ${new Date(conn.last_sync_at).toLocaleString()}`
                      : "Never synced"}
                  </p>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 opacity-0 transition-opacity group-hover:opacity-100"
                    onClick={() => handleDelete(conn.id)}
                    disabled={deleteConnection.isPending}
                  >
                    <Trash2 className="h-4 w-4 text-muted-foreground hover:text-destructive" />
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
