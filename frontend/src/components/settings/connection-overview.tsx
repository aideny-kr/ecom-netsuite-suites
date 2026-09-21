"use client";
import { ConnectionGroup } from "@/components/settings/connection-group";

import { MetabaseConnectButton } from "@/components/metabase-connect-button";
import { useState, type ReactNode } from "react";
import { useConnectionHealth, type ConnectionHealthItem } from "@/hooks/use-connection-health";
import { accessState, systemKey } from "@/lib/connection-health";
import { ConnectionUsage } from "./connection-usage";
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
  health?: ConnectionHealthItem; system: string;
  metabase?: boolean; authorizationRequired?: boolean; verificationPending?: boolean;
}

export function ConnectionOverview({ setup = {} }: { setup?: Record<string, ReactNode> }) {
  const { user } = useAuth();
  return <ConnectionsContent key={user?.tenant_id} setup={setup} />;
}

function ConnectionsContent({ setup }: { setup: Record<string, ReactNode> }) {
  const connections = useConnections(), mcp = useMcpConnectors();
  const health = useConnectionHealth();
  const [usage, setUsage] = useState<string | null>(null);
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
    ...(connections.data || []).filter((item) => !["revoked", "superseded"].includes(item.status) && item.provider !== "celigo").map((item) => ({
      id: item.id, label: item.label, provider: item.provider, status: item.status, kind: "api" as const,
      system: systemKey(item.provider), health: health.data?.connections.find((h) => h.id === item.id),
      detail: typeof item.metadata_json?.base_url === "string" ? item.metadata_json.base_url : undefined, error: item.error_reason,
    })),
    ...(mcp.data || []).filter((item) => !["revoked", "superseded"].includes(item.status) && item.provider !== "celigo_mcp").map((item) => ({
      id: item.id, label: item.label, provider: item.provider, status: item.is_enabled === false ? "disabled" : item.status, kind: "mcp" as const,
      system: systemKey(item.provider, item.server_url), health: health.data?.mcp_connectors.find((h) => h.id === item.id),
      detail: `${item.discovered_tools?.length || 0} tools · ${item.server_url}`, error: item.error_reason,
      metabase: item.provider === "custom" && item.auth_type === "oauth2" && systemKey(item.provider, item.server_url).startsWith("metabase:"),
      authorizationRequired: !["connected", "verification_pending"].includes(String(item.metadata_json?.setup_state)),
      verificationPending: item.metadata_json?.setup_state === "verification_pending",
    })),
  ];
  const groups = Array.from(new Set([...cards.map((card) => card.system), ...Object.keys(setup)]));
  const names: Record<string, string> = { netsuite: "NetSuite", bigquery: "BigQuery", shopify: "Shopify", stripe: "Stripe", solidus: "Solidus", api: "Custom API", google_sheets: "Google Sheets & Drive" };
  function name(key: string) { return names[key] || (key.startsWith("metabase:") ? `Metabase · ${key.slice(9)}` : key.startsWith("custom:") ? `MCP · ${key.slice(7)}` : key); }
  async function test(item: ConnectorCard) {
    setTesting(item.id);
    try {
      const result = await (item.kind === "api" ? testApi.mutateAsync(item.id) : testMcp.mutateAsync(item.id));
      const title = result.status === "ok" ? "Connection verified"
        : result.status === "partial" ? "Verification incomplete"
        : result.status === "unsupported" ? "Test unavailable" : "Connection needs attention";
      toast({ title, description: result.message, variant: ["ok", "partial", "unsupported"].includes(result.status) ? "default" : "destructive" });
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
        <div><h2 className="text-lg font-medium tracking-tight">Connected systems</h2><p className="mt-1 text-[13px] text-muted-foreground">Manage each system and its authorized access methods.</p></div>
        {canManage && <div className="flex flex-wrap gap-2"><AddConnectionDialog /><AddMcpConnectorDialog /></div>}
      </div>
      {!canManage && <p className="text-[13px] text-muted-foreground">You can view connections. A connection manager can add, test, or delete them.</p>}
      {(connections.isError || mcp.isError) && <div role="alert" className="rounded-lg border p-4 text-[13px]">Some connections could not be loaded. <Button variant="outline" size="sm" onClick={() => { void connections.refetch(); void mcp.refetch(); }}>Reload connections</Button></div>}
      {health.isError && <p role="alert" className="text-[13px] text-destructive">Health details could not be loaded. Saved connection status is shown; use Test to verify access.</p>}
      {connections.isLoading || mcp.isLoading ? <Skeleton className="h-40 rounded-xl" /> : (
        <div className="space-y-5">
          {groups.map((group) => <section key={group} aria-label={`${name(group)} access`} className="rounded-xl border bg-card p-5 shadow-soft space-y-4">
            <h3 className="text-lg font-semibold">{name(group)}</h3>
            {group === "netsuite" && <p className="text-[13px] text-muted-foreground">REST API and MCP use separate authorizations. Check the method your work requires.</p>}
            <div className="grid gap-4 lg:grid-cols-2">
              {cards.filter((card) => card.system === group).map((item) => {
                const key = `${item.kind}:${item.id}`;
                const h = item.health;
                const state = item.metabase && item.authorizationRequired ? "Authorization required" : item.metabase && item.verificationPending ? "Verification pending" : accessState(h || { status: item.status });
                return <article id={`connection-${item.kind}-${item.id}`} key={key} className="min-w-0 space-y-3 rounded-lg border bg-muted/10 p-4 scroll-mt-6" tabIndex={-1}>
                  <div className="flex flex-wrap items-start justify-between gap-2"><div className="min-w-0"><h4 className="break-words text-[15px] font-medium">{item.label}</h4><p className="text-[13px] text-muted-foreground">{item.kind === "api" ? "REST API" : "MCP / agent access"}</p></div><Badge variant={["Needs attention", "Authorization expired", "Last test failed"].includes(state) ? "destructive" : "secondary"}>{state}</Badge></div>
                  {item.detail && <p className="break-all text-[13px] text-muted-foreground">{item.detail}</p>}
                  <dl className="space-y-1 text-[13px] text-muted-foreground">
                    <div><dt className="inline">Account: </dt><dd className="inline break-words">{h?.account_identity || "Not reported"}</dd></div>
                    <div><dt className="inline">Scope / role: </dt><dd className="inline break-words">{[h?.access_scope, h?.role].filter(Boolean).join(" · ") || "Not reported; verify with the provider"}</dd></div>
                    <div><dt className="inline">Last check: </dt><dd className="inline">{h?.last_health_check ? <time dateTime={h.last_health_check}>{new Date(h.last_health_check).toLocaleString()}</time> : "No recorded check"}</dd></div>
                  </dl>
                  {h?.status === "refresh_required" && <p className="text-[13px]">The saved access token has expired. Test this method to attempt its supported refresh.</p>}
                  {item.metabase && item.authorizationRequired && <p className="text-[13px] text-muted-foreground">Sign in to Metabase to allow read access for investigations.</p>}
                  {(h?.error_reason || item.error) && !(item.metabase && item.authorizationRequired) && <p className="text-[13px] text-destructive">{h?.error_reason || item.error}</p>}
                  <div className="flex flex-wrap gap-3 text-[13px]"><a className="text-primary underline" href={`#connection-${item.kind}-${item.id}`}>Link to this method</a><button className="text-primary underline" aria-expanded={usage === key} onClick={() => setUsage(usage === key ? null : key)}>Dependent work</button></div>
                  {usage === key && <ConnectionUsage kind={item.kind} id={item.id} />}
                  {canManage && <div className="flex flex-wrap gap-2 border-t pt-3">
                    {item.metabase && <MetabaseConnectButton connectorId={item.id} reconnect={!item.authorizationRequired} disabled={removing || testing !== null} />}
                    {!(item.metabase && item.authorizationRequired) && <Button variant="outline" size="sm" onClick={() => void test(item)} disabled={testing !== null || removing}><FlaskConical className="mr-2 h-4 w-4" />{testing === item.id ? "Testing…" : "Test"}</Button>}
                    {setup[group] && <Button variant="outline" size="sm" asChild><a href={group === "netsuite" ? `#connection-settings-${item.kind}-${item.id}` : `#connection-setup-${group}`}>Connection setup</a></Button>}
                    <Button variant="ghost" size="sm" onClick={() => setDeleting(item)} disabled={removing || testing !== null} aria-label={`Delete ${item.label}`}><Trash2 className="mr-2 h-4 w-4" />Delete</Button>
                  </div>}
                </article>;
              })}
              {group === "netsuite" && (["api", "mcp"] as const).filter((kind) => !cards.some((card) => card.system === group && card.kind === kind)).map((kind) => <div key={kind} className="rounded-lg border border-dashed p-4 text-[13px]"><p>{kind === "api" ? "REST API" : "MCP / agent access"}: Not configured</p>{setup.netsuite ? <a className="mt-2 inline-block text-primary underline" href="#connection-setup-netsuite">Set up this access method</a> : <p className="mt-2 text-muted-foreground">Ask an administrator to configure this method.</p>}</div>)}
            </div>
            {setup[group] && <ConnectionGroup title="Connection setup" description="Existing authorization, source selection and configuration."><div id={`connection-setup-${group}`} tabIndex={-1}>{setup[group]}</div></ConnectionGroup>}
          </section>)}
        </div>
      )}
      {!connections.isLoading && !mcp.isLoading && !connections.isError && !mcp.isError && !cards.length && <div className="rounded-xl border border-dashed p-8 text-center"><Plug className="mx-auto mb-3 h-6 w-6 text-muted-foreground" /><p className="text-[15px]">No API or MCP connections yet</p><p className="mt-2 text-[13px] text-muted-foreground">Add Solidus, another platform, a custom API, or an MCP server above.</p></div>}
      {showCeligo && <ConnectionGroup title="Celigo" description="Integration inventory and optional agent access."><CeligoConnectorCard /></ConnectionGroup>}
      <Dialog open={!!deleting} onOpenChange={(open) => { if (!open && !removing) setDeleting(null); }}><DialogContent><DialogHeader><DialogTitle>Delete {deleting?.label}?</DialogTitle><DialogDescription>This stops future access through this connection. Saved investigation and audit records are retained. Scopes using it will need a new connection.</DialogDescription></DialogHeader><div className="max-h-64 overflow-auto">{deleting && <ConnectionUsage key={`${deleting.kind}:${deleting.id}`} kind={deleting.kind} id={deleting.id} />}</div><DialogFooter><Button variant="outline" disabled={removing} onClick={() => setDeleting(null)}>Cancel</Button><Button variant="destructive" disabled={removing} onClick={() => void remove()}>{removing ? "Deleting…" : "Delete connection"}</Button></DialogFooter></DialogContent></Dialog>
    </div>
  );
}
