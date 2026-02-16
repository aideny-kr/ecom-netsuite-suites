"use client";

import { useConnections, useDeleteConnection } from "@/hooks/use-connections";
import { AddConnectionDialog } from "@/components/add-connection-dialog";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import { Trash2 } from "lucide-react";

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
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-3xl font-bold tracking-tight">Connections</h2>
          <p className="text-muted-foreground">
            Manage your platform integrations
          </p>
        </div>
        <AddConnectionDialog />
      </div>

      {isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-40" />
          ))}
        </div>
      ) : !connections?.length ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12">
            <p className="mb-4 text-muted-foreground">
              No connections yet. Add your first integration to get started.
            </p>
            <AddConnectionDialog />
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {connections.map((conn) => (
            <Card key={conn.id}>
              <CardHeader className="flex flex-row items-start justify-between space-y-0">
                <div>
                  <CardTitle className="text-base">{conn.label}</CardTitle>
                  <CardDescription className="capitalize">
                    {conn.provider}
                  </CardDescription>
                </div>
                <Badge
                  variant={
                    conn.status === "active"
                      ? "default"
                      : conn.status === "error"
                        ? "destructive"
                        : "secondary"
                  }
                >
                  {conn.status}
                </Badge>
              </CardHeader>
              <CardContent>
                <div className="flex items-center justify-between">
                  <p className="text-xs text-muted-foreground">
                    {conn.last_sync_at
                      ? `Last sync: ${new Date(conn.last_sync_at).toLocaleString()}`
                      : "Never synced"}
                  </p>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => handleDelete(conn.id)}
                    disabled={deleteConnection.isPending}
                  >
                    <Trash2 className="h-4 w-4 text-muted-foreground" />
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
