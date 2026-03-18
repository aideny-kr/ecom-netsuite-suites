"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useConnectionHealth,
  type ConnectionHealthItem,
} from "@/hooks/use-connection-health";
import {
  useConnections,
  useDeleteConnection,
  useReconnectConnection,
  useTestConnection,
  useUpdateClientId,
  useUpdateRestletUrl,
} from "@/hooks/use-connections";
import {
  useMcpConnectors,
  useDeleteMcpConnector,
  useReauthorizeMcpConnector,
  useTestMcpConnector,
  useUpdateMcpClientId,
} from "@/hooks/use-mcp-connectors";
import { usePermissions } from "@/hooks/use-permissions";
import { apiClient } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useToast } from "@/hooks/use-toast";
import {
  MoreVertical,
  RefreshCw,
  Trash2,
  FlaskConical,
  Loader2,
  Lock,
  Pencil,
  Check,
  X,
  Plus,
  Wifi,
  WifiOff,
  Key,
  Globe,
  Wrench,
} from "lucide-react";

// ---------------------------------------------------------------------------
// EditableField — inline editable text field
// ---------------------------------------------------------------------------

function EditableField({
  label,
  value,
  onSave,
  isSaving,
  icon: Icon,
}: {
  label: string;
  value: string;
  onSave: (val: string) => void;
  isSaving: boolean;
  icon?: React.ComponentType<{ className?: string }>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  const handleEdit = useCallback(() => {
    setDraft(value);
    setEditing(true);
  }, [value]);

  const handleCancel = useCallback(() => {
    setEditing(false);
    setDraft(value);
  }, [value]);

  const handleSave = useCallback(() => {
    if (draft.trim() && draft.trim() !== value) {
      onSave(draft.trim());
    }
    setEditing(false);
  }, [draft, value, onSave]);

  if (editing) {
    return (
      <div className="flex items-center gap-2 rounded-lg border bg-muted/30 px-3 py-2">
        {Icon && <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
        <span className="text-[12px] font-medium text-muted-foreground shrink-0">
          {label}
        </span>
        <input
          autoFocus
          className="flex-1 bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted-foreground/50"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleSave();
            if (e.key === "Escape") handleCancel();
          }}
        />
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6"
          onClick={handleSave}
          disabled={isSaving}
        >
          {isSaving ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Check className="h-3.5 w-3.5 text-green-600" />
          )}
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6"
          onClick={handleCancel}
          disabled={isSaving}
        >
          <X className="h-3.5 w-3.5 text-muted-foreground" />
        </Button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2 rounded-lg border border-transparent px-3 py-2 transition-colors hover:border-border hover:bg-muted/20">
      {Icon && <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
      <span className="text-[12px] font-medium text-muted-foreground shrink-0">
        {label}
      </span>
      {value ? (
        <span className="flex-1 truncate text-[13px] text-foreground">{value}</span>
      ) : (
        <span className="flex-1 text-[13px] italic text-muted-foreground/50">
          Not configured
        </span>
      )}
      <Button
        variant="ghost"
        size="sm"
        className="h-6 px-2 text-[11px] text-muted-foreground"
        onClick={handleEdit}
      >
        {value ? (
          <Pencil className="mr-1 h-3 w-3" />
        ) : null}
        {value ? "Edit" : "Set"}
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ConnectionRow — one connection with status dot, label, kebab menu
// ---------------------------------------------------------------------------

function statusColor(status: string, tokenExpired: boolean) {
  // Status takes priority — if active, it's green regardless of token_expired
  // (access tokens expire hourly but auto-refresh on use)
  switch (status) {
    case "active":
      return "bg-green-500";
    case "needs_reauth":
      return "bg-yellow-500 animate-pulse";
    case "pending":
    case "inactive":
      return "bg-muted-foreground/40";
    case "error":
      return "bg-red-500 animate-pulse";
    default:
      if (tokenExpired) return "bg-yellow-500";
      return "bg-muted-foreground/40";
  }
}

function statusLabel(status: string, tokenExpired: boolean) {
  switch (status) {
    case "active":
      return "Active";
    case "needs_reauth":
      return "Needs Re-auth";
    case "pending":
      return "Pending";
    case "inactive":
      return "Inactive";
    case "error":
      return "Error";
    default:
      return status;
  }
}

function ConnectionRow({
  label,
  authType,
  status,
  tokenExpired,
  toolCount,
  onReauth,
  onTest,
  onDelete,
  isReauthing,
  isTesting,
  isDeleting,
}: {
  label: string;
  authType: string | null;
  status: string;
  tokenExpired: boolean;
  toolCount?: number;
  onReauth: () => void;
  onTest: () => void;
  onDelete: () => void;
  isReauthing: boolean;
  isTesting: boolean;
  isDeleting: boolean;
}) {
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);

  return (
    <>
      <div className="flex items-center gap-3 rounded-lg border bg-muted/20 px-3 py-2.5">
        {/* Status dot */}
        <div
          className={`h-2.5 w-2.5 shrink-0 rounded-full ${statusColor(status, tokenExpired)}`}
        />

        {/* Label + auth type */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[14px] font-medium text-foreground truncate">
              {label}
            </span>
            {authType && (
              <Badge variant="secondary" className="text-[10px] px-1.5 py-0 h-4">
                {authType}
              </Badge>
            )}
            {typeof toolCount === "number" && toolCount > 0 && (
              <Badge variant="outline" className="text-[10px] px-1.5 py-0 h-4 gap-0.5">
                <Wrench className="h-2.5 w-2.5" />
                {toolCount}
              </Badge>
            )}
          </div>
          <span className="text-[11px] text-muted-foreground">
            {statusLabel(status, tokenExpired)}
          </span>
        </div>

        {/* Kebab menu */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0">
              <MoreVertical className="h-4 w-4 text-muted-foreground" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            <DropdownMenuItem
              onClick={onReauth}
              disabled={isReauthing}
            >
              <RefreshCw
                className={`mr-2 h-3.5 w-3.5 ${isReauthing ? "animate-spin" : ""}`}
              />
              {isReauthing ? "Authorizing..." : "Re-authenticate"}
            </DropdownMenuItem>
            <DropdownMenuItem
              onClick={onTest}
              disabled={isTesting}
            >
              <FlaskConical className="mr-2 h-3.5 w-3.5" />
              {isTesting ? "Testing..." : "Test Connection"}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              className="text-destructive focus:text-destructive"
              onClick={() => setShowDeleteDialog(true)}
              disabled={isDeleting}
            >
              <Trash2 className="mr-2 h-3.5 w-3.5" />
              Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* Delete confirmation */}
      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete connection</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete &ldquo;{label}&rdquo;? This action
              cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => {
                onDelete();
                setShowDeleteDialog(false);
              }}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

// ---------------------------------------------------------------------------
// NetSuiteConnectionsSection — main exported component
// ---------------------------------------------------------------------------

export function NetSuiteConnectionsSection() {
  const { isAdmin } = usePermissions();
  const { toast } = useToast();

  // Connect dialog state
  const [showConnectOAuth, setShowConnectOAuth] = useState(false);
  const [showConnectMcp, setShowConnectMcp] = useState(false);
  const [connectAccountId, setConnectAccountId] = useState("");
  const [connectClientId, setConnectClientId] = useState("");
  const [connectRestletUrl, setConnectRestletUrl] = useState("");
  const [isConnecting, setIsConnecting] = useState(false);

  // Refs for cleanup on unmount
  const popupIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const refreshTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (popupIntervalRef.current) clearInterval(popupIntervalRef.current);
      if (refreshTimeoutRef.current) clearTimeout(refreshTimeoutRef.current);
    };
  }, []);

  // Data
  const queryClient = useQueryClient();
  const { data: health } = useConnectionHealth();
  const { data: connections } = useConnections();
  const { data: mcpConnectors } = useMcpConnectors();

  // Mutations — OAuth
  const deleteConn = useDeleteConnection();
  const reconnectConn = useReconnectConnection();
  const testConn = useTestConnection();
  const updateClientId = useUpdateClientId();
  const updateRestletUrl = useUpdateRestletUrl();

  // Mutations — MCP
  const deleteMcp = useDeleteMcpConnector();
  const reauthorizeMcp = useReauthorizeMcpConnector();
  const testMcp = useTestMcpConnector();
  const updateMcpClientId = useUpdateMcpClientId();

  if (!isAdmin) return null;

  // Filter out revoked connections
  const oauthConns = (connections ?? []).filter(
    (c) => c.provider === "netsuite" && c.status !== "revoked",
  );
  const mcpConns = (mcpConnectors ?? []).filter(
    (c) => c.status !== "revoked",
  );

  // Derive client IDs and restlet URL from first active connection metadata
  const activeOAuth = oauthConns.find((c) => c.status === "active") ?? oauthConns[0];
  const activeMcp = mcpConns.find((c) => c.status === "active") ?? mcpConns[0];

  // Get Client IDs and RESTlet URL from health data (decrypted on server)
  const oauthHealthItem = health?.connections.find((h) => h.id === activeOAuth?.id);
  const mcpHealthItem = health?.mcp_connectors.find((h) => h.id === activeMcp?.id);

  const oauthClientId =
    oauthHealthItem?.client_id ?? (activeOAuth?.metadata_json?.client_id as string) ?? "";
  const restletUrl =
    oauthHealthItem?.restlet_url ?? (activeOAuth?.metadata_json?.restlet_url as string) ?? "";
  const mcpClientId =
    mcpHealthItem?.client_id ?? (activeMcp?.metadata_json?.client_id as string) ?? "";

  // ── Connect new OAuth connection ──
  async function handleConnectOAuth() {
    if (!connectAccountId.trim() || !connectClientId.trim()) {
      toast({ title: "Account ID and Client ID are required", variant: "destructive" });
      return;
    }
    setIsConnecting(true);
    try {
      const params = new URLSearchParams({
        account_id: connectAccountId.trim(),
        client_id: connectClientId.trim(),
        restlet_url: connectRestletUrl.trim(),
      });
      const result = await apiClient.get<{ authorize_url: string; state: string }>(
        `/api/v1/connections/netsuite/authorize?${params}`,
      );
      const width = 600;
      const height = 700;
      const left = window.screenX + (window.innerWidth - width) / 2;
      const top = window.screenY + (window.innerHeight - height) / 2;
      const popup = window.open(
        result.authorize_url,
        "netsuite_oauth_connect",
        `width=${width},height=${height},left=${left},top=${top},popup=yes`,
      );
      if (popup) {
        popupIntervalRef.current = setInterval(() => {
          if (popup.closed) {
            if (popupIntervalRef.current) clearInterval(popupIntervalRef.current);
            popupIntervalRef.current = null;
            refreshHealthAfterDelay();
            setShowConnectOAuth(false);
            setConnectAccountId("");
            setConnectClientId("");
            setConnectRestletUrl("");
          }
        }, 500);
      }
    } catch (err) {
      toast({
        title: "Connection failed",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setIsConnecting(false);
    }
  }

  // ── Connect new MCP connection ──
  async function handleConnectMcp() {
    if (!connectAccountId.trim() || !connectClientId.trim()) {
      toast({ title: "Account ID and Client ID are required", variant: "destructive" });
      return;
    }
    setIsConnecting(true);
    try {
      const params = new URLSearchParams({
        account_id: connectAccountId.trim(),
        client_id: connectClientId.trim(),
      });
      const result = await apiClient.get<{ authorize_url: string; state: string }>(
        `/api/v1/mcp-connectors/netsuite/authorize?${params}`,
      );
      const width = 600;
      const height = 700;
      const left = window.screenX + (window.innerWidth - width) / 2;
      const top = window.screenY + (window.innerHeight - height) / 2;
      const popup = window.open(
        result.authorize_url,
        "netsuite_mcp_connect",
        `width=${width},height=${height},left=${left},top=${top},popup=yes`,
      );
      if (popup) {
        popupIntervalRef.current = setInterval(() => {
          if (popup.closed) {
            if (popupIntervalRef.current) clearInterval(popupIntervalRef.current);
            popupIntervalRef.current = null;
            refreshHealthAfterDelay();
            setShowConnectMcp(false);
            setConnectAccountId("");
            setConnectClientId("");
          }
        }, 500);
      }
    } catch (err) {
      toast({
        title: "Connection failed",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setIsConnecting(false);
    }
  }

  // Handlers with toast feedback
  function refreshHealthAfterDelay() {
    // Refresh immediately + again after 3s (callback may still be processing)
    queryClient.invalidateQueries({ queryKey: ["connections"] });
    queryClient.invalidateQueries({ queryKey: ["mcp-connectors"] });
    queryClient.invalidateQueries({ queryKey: ["connection-health"] });
    refreshTimeoutRef.current = setTimeout(() => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
      queryClient.invalidateQueries({ queryKey: ["mcp-connectors"] });
      queryClient.invalidateQueries({ queryKey: ["connection-health"] });
    }, 3000);
  }

  async function handleOAuthReauth(id: string) {
    try {
      const result = await reconnectConn.mutateAsync(id);
      if (result && "authorize_url" in result) {
        const width = 600;
        const height = 700;
        const left = window.screenX + (window.innerWidth - width) / 2;
        const top = window.screenY + (window.innerHeight - height) / 2;
        const popup = window.open(
          result.authorize_url,
          "netsuite_oauth_reauth",
          `width=${width},height=${height},left=${left},top=${top},popup=yes`,
        );
        // Refresh data when popup closes
        if (popup) {
          popupIntervalRef.current = setInterval(() => {
            if (popup.closed) {
              if (popupIntervalRef.current) clearInterval(popupIntervalRef.current);
              popupIntervalRef.current = null;
              refreshHealthAfterDelay();
            }
          }, 500);
        }
      } else {
        toast({ title: "Connection refreshed" });
        refreshHealthAfterDelay();
      }
    } catch (err) {
      toast({
        title: "Re-authentication failed",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleOAuthTest(id: string) {
    try {
      const result = await testConn.mutateAsync(id);
      toast({
        title: result.status === "ok" ? "Connection successful" : "Connection failed",
        description: result.message,
        variant: result.status === "ok" ? "default" : "destructive",
      });
    } catch (err) {
      toast({
        title: "Test failed",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleOAuthDelete(id: string) {
    try {
      await deleteConn.mutateAsync(id);
      toast({ title: "Connection deleted" });
    } catch (err) {
      toast({
        title: "Failed to delete connection",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleMcpReauth(id: string) {
    try {
      await reauthorizeMcp.mutateAsync(id);
      toast({ title: "Re-authorization successful", description: "MCP connector tokens refreshed" });
      refreshHealthAfterDelay();
    } catch (err) {
      toast({
        title: "Re-authorization failed",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleMcpTest(id: string) {
    try {
      const result = await testMcp.mutateAsync(id);
      toast({
        title: result.status === "ok" ? "Connection successful" : "Connection failed",
        description: result.message,
        variant: result.status === "ok" ? "default" : "destructive",
      });
    } catch (err) {
      toast({
        title: "Test failed",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleMcpDelete(id: string) {
    try {
      await deleteMcp.mutateAsync(id);
      toast({ title: "MCP connector deleted" });
    } catch (err) {
      toast({
        title: "Failed to delete connector",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  function handleSaveOAuthClientId(val: string) {
    if (!activeOAuth) return;
    updateClientId.mutate(
      { id: activeOAuth.id, client_id: val },
      {
        onSuccess: () => toast({ title: "Client ID updated" }),
        onError: (err) =>
          toast({
            title: "Failed to update Client ID",
            description: err instanceof Error ? err.message : "Unknown error",
            variant: "destructive",
          }),
      },
    );
  }

  function handleSaveRestletUrl(val: string) {
    if (!activeOAuth) return;
    updateRestletUrl.mutate(
      { id: activeOAuth.id, restlet_url: val },
      {
        onSuccess: () => toast({ title: "RESTlet URL updated" }),
        onError: (err) =>
          toast({
            title: "Failed to update RESTlet URL",
            description: err instanceof Error ? err.message : "Unknown error",
            variant: "destructive",
          }),
      },
    );
  }

  function handleSaveMcpClientId(val: string) {
    if (!activeMcp) return;
    updateMcpClientId.mutate(
      { id: activeMcp.id, client_id: val },
      {
        onSuccess: () => toast({ title: "MCP Client ID updated" }),
        onError: (err) =>
          toast({
            title: "Failed to update MCP Client ID",
            description: err instanceof Error ? err.message : "Unknown error",
            variant: "destructive",
          }),
      },
    );
  }

  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft space-y-6">
      <div>
        <h3 className="text-lg font-semibold">NetSuite Connections</h3>
        <p className="mt-0.5 text-[13px] text-muted-foreground">
          Manage OAuth API and MCP tool connections
        </p>
      </div>

      {/* ── OAuth API Connections ── */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h4 className="text-[13px] font-semibold uppercase tracking-wider text-muted-foreground">
            OAuth API Connections
          </h4>
          {oauthConns.length > 0 && (
            <div className="flex items-center gap-1.5">
              <Wifi className="h-3.5 w-3.5 text-green-500" />
              <span className="text-[12px] text-muted-foreground">
                {oauthConns.filter((c) => c.status === "active").length} active
              </span>
            </div>
          )}
        </div>

        {/* Client ID */}
        {activeOAuth && (
          <EditableField
            label="Client ID"
            value={oauthClientId}
            onSave={handleSaveOAuthClientId}
            isSaving={updateClientId.isPending}
            icon={Key}
          />
        )}

        {/* Connection rows */}
        {oauthConns.map((conn) => {
          const healthItem = health?.connections.find((h) => h.id === conn.id);
          return (
            <ConnectionRow
              key={conn.id}
              label={conn.label || "NetSuite OAuth"}
              authType={conn.auth_type}
              status={healthItem?.status ?? conn.status}
              tokenExpired={healthItem?.token_expired ?? false}
              onReauth={() => handleOAuthReauth(conn.id)}
              onTest={() => handleOAuthTest(conn.id)}
              onDelete={() => handleOAuthDelete(conn.id)}
              isReauthing={reconnectConn.isPending}
              isTesting={testConn.isPending}
              isDeleting={deleteConn.isPending}
            />
          );
        })}

        {oauthConns.length === 0 && (
          <div className="flex items-center gap-3 rounded-lg border border-dashed px-3 py-4">
            <WifiOff className="h-4 w-4 text-muted-foreground/50" />
            <p className="text-[13px] text-muted-foreground">
              No OAuth API connections configured
            </p>
          </div>
        )}

        {/* RESTlet URL */}
        {activeOAuth && (
          <EditableField
            label="RESTlet URL"
            value={restletUrl}
            onSave={handleSaveRestletUrl}
            isSaving={updateRestletUrl.isPending}
            icon={Globe}
          />
        )}

        {/* Connect OAuth button + dialog */}
        <Button variant="outline" size="sm" onClick={() => { setShowConnectOAuth(true); setConnectAccountId(""); setConnectClientId(""); setConnectRestletUrl(""); }}>
          <Plus className="mr-1.5 h-3.5 w-3.5" />
          Connect NetSuite
        </Button>
        <AlertDialog open={showConnectOAuth} onOpenChange={setShowConnectOAuth}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Connect NetSuite (REST API)</AlertDialogTitle>
              <AlertDialogDescription>
                Enter your NetSuite Account ID, Client ID from your Integration Record (REST Web Services scope), and RESTlet URL.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <div className="space-y-3 py-2">
              <div>
                <label className="text-[13px] font-medium text-foreground">Account ID</label>
                <input
                  type="text"
                  value={connectAccountId}
                  onChange={(e) => setConnectAccountId(e.target.value)}
                  placeholder="e.g. 6738075"
                  className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-[14px]"
                />
              </div>
              <div>
                <label className="text-[13px] font-medium text-foreground">Client ID</label>
                <input
                  type="text"
                  value={connectClientId}
                  onChange={(e) => setConnectClientId(e.target.value)}
                  placeholder="From NetSuite Integration Record"
                  className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-[14px]"
                />
              </div>
              <div>
                <label className="text-[13px] font-medium text-foreground">RESTlet URL <span className="text-muted-foreground">(optional)</span></label>
                <input
                  type="text"
                  value={connectRestletUrl}
                  onChange={(e) => setConnectRestletUrl(e.target.value)}
                  placeholder="https://..."
                  className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-[14px]"
                />
              </div>
            </div>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <Button onClick={handleConnectOAuth} disabled={isConnecting || !connectAccountId.trim() || !connectClientId.trim()}>
                {isConnecting ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Lock className="mr-1.5 h-3.5 w-3.5" />}
                Authorize
              </Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>

      <div className="border-t" />

      {/* ── MCP Tool Connections ── */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h4 className="text-[13px] font-semibold uppercase tracking-wider text-muted-foreground">
            MCP Tool Connections
          </h4>
          {mcpConns.length > 0 && (
            <div className="flex items-center gap-1.5">
              <Wifi className="h-3.5 w-3.5 text-green-500" />
              <span className="text-[12px] text-muted-foreground">
                {mcpConns.filter((c) => c.status === "active").length} active
              </span>
            </div>
          )}
        </div>

        {/* MCP Client ID */}
        {activeMcp && (
          <EditableField
            label="Client ID"
            value={mcpClientId}
            onSave={handleSaveMcpClientId}
            isSaving={updateMcpClientId.isPending}
            icon={Key}
          />
        )}

        {/* MCP connection rows */}
        {mcpConns.map((mcp) => {
          const healthItem = health?.mcp_connectors.find((h) => h.id === mcp.id);
          return (
            <ConnectionRow
              key={mcp.id}
              label={mcp.label || "MCP Connector"}
              authType={mcp.auth_type}
              status={healthItem?.status ?? mcp.status}
              tokenExpired={healthItem?.token_expired ?? false}
              toolCount={healthItem?.tool_count ?? mcp.discovered_tools?.length ?? 0}
              onReauth={() => handleMcpReauth(mcp.id)}
              onTest={() => handleMcpTest(mcp.id)}
              onDelete={() => handleMcpDelete(mcp.id)}
              isReauthing={reauthorizeMcp.isPending}
              isTesting={testMcp.isPending}
              isDeleting={deleteMcp.isPending}
            />
          );
        })}

        {mcpConns.length === 0 && (
          <div className="flex items-center gap-3 rounded-lg border border-dashed px-3 py-4">
            <WifiOff className="h-4 w-4 text-muted-foreground/50" />
            <p className="text-[13px] text-muted-foreground">
              No MCP tool connections configured
            </p>
          </div>
        )}

        {/* Connect MCP button + dialog */}
        <Button variant="outline" size="sm" onClick={() => { setShowConnectMcp(true); setConnectAccountId(""); setConnectClientId(""); }}>
          <Plus className="mr-1.5 h-3.5 w-3.5" />
          Connect MCP
        </Button>
        <AlertDialog open={showConnectMcp} onOpenChange={setShowConnectMcp}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Connect NetSuite MCP</AlertDialogTitle>
              <AlertDialogDescription>
                Enter your NetSuite Account ID and Client ID from your MCP Integration Record (NetSuite AI Connector Service scope).
              </AlertDialogDescription>
            </AlertDialogHeader>
            <div className="space-y-3 py-2">
              <div>
                <label className="text-[13px] font-medium text-foreground">Account ID</label>
                <input
                  type="text"
                  value={connectAccountId}
                  onChange={(e) => setConnectAccountId(e.target.value)}
                  placeholder="e.g. 6738075"
                  className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-[14px]"
                />
              </div>
              <div>
                <label className="text-[13px] font-medium text-foreground">Client ID</label>
                <input
                  type="text"
                  value={connectClientId}
                  onChange={(e) => setConnectClientId(e.target.value)}
                  placeholder="From NetSuite MCP Integration Record"
                  className="mt-1 w-full rounded-md border bg-background px-3 py-2 text-[14px]"
                />
              </div>
            </div>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <Button onClick={handleConnectMcp} disabled={isConnecting || !connectAccountId.trim() || !connectClientId.trim()}>
                {isConnecting ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Lock className="mr-1.5 h-3.5 w-3.5" />}
                Authorize
              </Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </div>
  );
}
