"use client";

import { useState } from "react";
import { useConnections, useDeleteConnection, useTestConnection } from "@/hooks/use-connections";
import { useMcpConnectors, useDeleteMcpConnector, useTestMcpConnector } from "@/hooks/use-mcp-connectors";
import { usePermissions } from "@/hooks/use-permissions";
import { useFeature } from "@/hooks/use-features";
import { useAuth } from "@/providers/auth-provider";
import { AddConnectionDialog } from "@/components/add-connection-dialog";
import { AddMcpConnectorDialog } from "@/components/add-mcp-connector-dialog";
import CeligoConnectorCard from "@/components/settings/celigo-connector-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { useToast } from "@/hooks/use-toast";
import { Trash2, Plug, FlaskConical } from "lucide-react";

interface ConnectorCard {
  id: string; label: string; provider: string; status: string;
  kind: "api" | "mcp"; detail?: string; error?: string | null;
}

export default function ConnectionsPage() {
  const { user } = useAuth();
  return <ConnectionsContent key={user?.tenant_id} />;
}

function ConnectionsContent() {
  const connections = useConnections(), mcp = useMcpConnectors();
  const removeApi = useDeleteConnection(), removeMcp = useDeleteMcpConnector();
  const testApi = useTestConnection(), testMcp = useTestMcpConnector();
  const { hasPermission } = usePermissions();
  const showCeligo = useFeature("celigo");
  const { toast } = useToast();
  const [deleting, setDeleting] = useState<ConnectorCard | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const canManage = hasPermission("connections.manage");
  const removing = removeApi.isPending || removeMcp.isPending;
  const cards: ConnectorCard[] = [
    ...(connections.data || []).filter((item) => item.status !== "revoked" && item.provider !== "celigo").map((item) => ({
      id: item.id, label: item.label, provider: item.provider, status: item.status, kind: "api" as const,
      detail: typeof item.metadata_json?.base_url === "string" ? item.metadata_json.base_url : undefined, error: item.error_reason,
    })),
    ...(mcp.data || []).filter((item) => item.status !== "revoked" && item.provider !== "celigo_mcp").map((item) => ({
      id: item.id, label: item.label, provider: item.provider === "custom" ? "Custom MCP" : item.provider.replaceAll("_", " "), status: item.status, kind: "mcp" as const,
      detail: `${item.discovered_tools?.length || 0} tools · ${item.server_url}`, error: item.error_reason,
    })),
  ];
  async function test(item: ConnectorCard) {
    setTesting(item.id);
    try {
      const result = await (item.kind === "api" ? testApi.mutateAsync(item.id) : testMcp.mutateAsync(item.id));
      toast({ title: result.status === "ok" ? "Read access verified" : "Connection needs attention", description: result.message, variant: result.status === "ok" ? "default" : "destructive" });
    } catch (error) {
      toast({ title: "Could not test connection", description: error instanceof Error ? error.message : "Try again", variant: "destructive" });
    } finally { setTesting(null); }
  }
  async function remove() {
    if (!deleting) return;
    try {
      await (deleting.kind === "api" ? removeApi.mutateAsync(deleting.id) : removeMcp.mutateAsync(deleting.id));
      setDeleting(null);
      toast({ title: "Connection deleted" });
    } catch (error) {
      toast({ title: "Could not delete connection", description: error instanceof Error ? error.message : "Try again", variant: "destructive" });
    }
  }
  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div><h2 className="text-2xl font-semibold tracking-tight">Connections</h2><p className="mt-1 text-[15px] text-muted-foreground">Connect your stores, APIs, and MCP tools.</p></div>
        {canManage && <div className="flex flex-wrap gap-2"><AddConnectionDialog /><AddMcpConnectorDialog /></div>}
      </div>
      {!canManage && <p className="text-[13px] text-muted-foreground">You can view connections. A connection manager can add, test, or delete them.</p>}
      {(connections.isError || mcp.isError) && <div role="alert" className="rounded-lg border p-4 text-[13px]">Some connections could not be loaded. <Button variant="outline" size="sm" onClick={() => { void connections.refetch(); void mcp.refetch(); }}>Reload connections</Button></div>}
      {connections.isLoading || mcp.isLoading ? <Skeleton className="h-40 rounded-xl" /> : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {cards.map((item) => <article key={`${item.kind}:${item.id}`} className="min-w-0 space-y-4 rounded-xl border bg-card p-5 shadow-soft">
            <div className="flex items-start justify-between gap-3"><div className="min-w-0"><p className="break-words text-[15px] font-semibold">{item.label}</p><p className="text-[13px] capitalize text-muted-foreground">{item.provider === "api" ? "Custom API" : item.provider}</p></div><Badge variant={item.status === "error" ? "destructive" : item.status === "active" || item.status === "healthy" ? "default" : "secondary"}>{item.status}</Badge></div>
            {item.detail && <p className="break-all text-[13px] text-muted-foreground">{item.detail}</p>}
            {item.status === "error" && item.error && <p className="text-[13px] text-destructive">{item.error}</p>}
            {canManage && <div className="flex flex-wrap gap-2 border-t pt-3">
              <Button variant="outline" size="sm" onClick={() => void test(item)} disabled={testing !== null || removing}><FlaskConical className="mr-2 h-4 w-4" />{testing === item.id ? "Testing…" : "Test"}</Button>
              <Button variant="ghost" size="sm" onClick={() => setDeleting(item)} disabled={removing || testing !== null} aria-label={`Delete ${item.label}`}><Trash2 className="mr-2 h-4 w-4" />Delete</Button>
            </div>}
          </article>)}
        </div>
      )}
      {!connections.isLoading && !mcp.isLoading && !connections.isError && !mcp.isError && !cards.length && <div className="rounded-xl border border-dashed p-8 text-center"><Plug className="mx-auto mb-3 h-6 w-6 text-muted-foreground" /><p className="text-[15px]">No API or MCP connections yet</p><p className="mt-2 text-[13px] text-muted-foreground">Add Solidus, another platform, a custom API, or an MCP server above.</p></div>}
      {showCeligo && <CeligoConnectorCard />}
      <Dialog open={!!deleting} onOpenChange={(open) => { if (!open && !removing) setDeleting(null); }}><DialogContent><DialogHeader><DialogTitle>Delete {deleting?.label}?</DialogTitle><DialogDescription>This stops future access through this connection. Saved investigation and audit records are retained. Scopes using it will need a new connection.</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" disabled={removing} onClick={() => setDeleting(null)}>Cancel</Button><Button variant="destructive" disabled={removing} onClick={() => void remove()}>{removing ? "Deleting…" : "Delete connection"}</Button></DialogFooter></DialogContent></Dialog>
    </div>
  );
}
