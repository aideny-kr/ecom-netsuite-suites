"use client";

import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useCreateMcpConnector, useMcpConnectors } from "@/hooks/use-mcp-connectors";
import { useToast } from "@/hooks/use-toast";
import { Plus } from "lucide-react";
import { apiClient } from "@/lib/api-client";

type Provider = "netsuite_mcp" | "shopify_mcp" | "custom";
type AuthType = "bearer" | "api_key" | "none";

const credentialFields: Record<AuthType, { key: string; label: string }[]> = {
  bearer: [{ key: "access_token", label: "Access Token" }],
  api_key: [
    { key: "api_key", label: "API Key" },
    { key: "header_name", label: "Header Name (default: X-API-Key)" },
  ],
  none: [],
};

export function AddMcpConnectorDialog() {
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState<Provider | "">("");
  const [label, setLabel] = useState("");
  const [serverUrl, setServerUrl] = useState("");
  const [authType, setAuthType] = useState<AuthType>("none");
  const [credentials, setCredentials] = useState<Record<string, string>>({});

  // NetSuite OAuth fields
  const [accountId, setAccountId] = useState("");
  const [clientId, setClientId] = useState("");
  const [isAuthorizing, setIsAuthorizing] = useState(false);

  const createConnector = useCreateMcpConnector();
  const { refetch: refetchConnectors } = useMcpConnectors();
  const { toast } = useToast();

  const isNetSuite = provider === "netsuite_mcp";

  function resetForm() {
    setProvider("");
    setLabel("");
    setServerUrl("");
    setAuthType("none");
    setCredentials({});
    setAccountId("");
    setClientId("");
    setIsAuthorizing(false);
  }

  // Listen for OAuth callback messages from popup window
  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      const apiOrigin = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      if (event.origin !== window.location.origin && event.origin !== apiOrigin) return;

      if (event.data?.type === "NETSUITE_MCP_AUTH_SUCCESS") {
        setIsAuthorizing(false);
        toast({ title: "NetSuite MCP connector created successfully" });
        refetchConnectors();
        setOpen(false);
        resetForm();
      } else if (event.data?.type === "NETSUITE_MCP_AUTH_ERROR") {
        setIsAuthorizing(false);
        toast({
          title: "NetSuite authentication failed",
          description: event.data.error || "Please try again",
          variant: "destructive",
        });
      }
    }

    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [toast, refetchConnectors]);

  async function onNetSuiteAuthorize() {
    if (!accountId || !clientId) return;

    setIsAuthorizing(true);
    try {
      const params = new URLSearchParams({
        account_id: accountId,
        client_id: clientId,
        label: label || `NetSuite MCP ${accountId}`,
      });
      const data = await apiClient.get<{ authorize_url: string; state: string }>(
        `/api/v1/mcp-connectors/netsuite/authorize?${params}`
      );

      // Open OAuth popup
      const width = 600;
      const height = 700;
      const left = window.screenX + (window.innerWidth - width) / 2;
      const top = window.screenY + (window.innerHeight - height) / 2;
      window.open(
        data.authorize_url,
        "netsuite_mcp_oauth",
        `width=${width},height=${height},left=${left},top=${top},popup=yes`
      );
    } catch (err) {
      setIsAuthorizing(false);
      toast({
        title: "Failed to start NetSuite authorization",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();

    if (isNetSuite) {
      await onNetSuiteAuthorize();
      return;
    }

    if (!provider || !serverUrl) return;

    try {
      await createConnector.mutateAsync({
        provider,
        label,
        server_url: serverUrl,
        auth_type: authType,
        credentials: authType !== "none" ? credentials : undefined,
      });
      toast({ title: "MCP connector created successfully" });
      setOpen(false);
      resetForm();
    } catch (err) {
      toast({
        title: "Failed to create MCP connector",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  const isSubmitDisabled = isNetSuite
    ? !accountId || !clientId || isAuthorizing
    : !provider || !serverUrl || createConnector.isPending;

  const submitLabel = isNetSuite
    ? isAuthorizing
      ? "Authorizing..."
      : "Authorize with NetSuite"
    : createConnector.isPending
      ? "Creating..."
      : "Create";

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button className="text-[13px] font-medium">
          <Plus className="mr-2 h-4 w-4" />
          Add MCP Connector
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-lg">Add MCP Connector</DialogTitle>
          <DialogDescription className="text-[13px]">
            Connect to an external MCP server for real-time data queries.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label className="text-[13px] font-medium">Provider</Label>
            <Select
              value={provider}
              onValueChange={(v) => {
                setProvider(v as Provider);
                // Reset auth fields when switching providers
                setAuthType("none");
                setCredentials({});
                setAccountId("");
                setClientId("");
              }}
            >
              <SelectTrigger className="h-10 text-[13px]">
                <SelectValue placeholder="Select a provider" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="netsuite_mcp">NetSuite MCP</SelectItem>
                <SelectItem value="shopify_mcp">Shopify MCP</SelectItem>
                <SelectItem value="custom">Custom</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="mcp-label" className="text-[13px] font-medium">
              Label
            </Label>
            <Input
              id="mcp-label"
              placeholder={
                isNetSuite
                  ? "e.g., Production NetSuite"
                  : "e.g., Production NetSuite MCP"
              }
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              className="h-10 text-[13px]"
            />
          </div>

          {/* NetSuite-specific fields */}
          {isNetSuite && (
            <>
              <div className="space-y-2">
                <Label htmlFor="ns-account-id" className="text-[13px] font-medium">
                  NetSuite Account ID
                </Label>
                <Input
                  id="ns-account-id"
                  placeholder="e.g., TSTDRV1234567 or 1234567_SB1"
                  value={accountId}
                  onChange={(e) => setAccountId(e.target.value)}
                  required
                  className="h-10 text-[13px]"
                />
                <p className="text-[11px] text-muted-foreground">
                  Found in Setup &gt; Company &gt; Company Information
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="ns-client-id" className="text-[13px] font-medium">
                  OAuth 2.0 Client ID
                </Label>
                <Input
                  id="ns-client-id"
                  placeholder="e.g., abc123def456..."
                  value={clientId}
                  onChange={(e) => setClientId(e.target.value)}
                  required
                  className="h-10 text-[13px]"
                />
                <p className="text-[11px] text-muted-foreground">
                  From your NetSuite Integration record (OAuth 2.0 PKCE)
                </p>
              </div>
            </>
          )}

          {/* Generic connector fields (non-NetSuite) */}
          {!isNetSuite && (
            <>
              <div className="space-y-2">
                <Label htmlFor="mcp-url" className="text-[13px] font-medium">
                  Server URL
                </Label>
                <Input
                  id="mcp-url"
                  placeholder="https://example.com/mcp/v1"
                  value={serverUrl}
                  onChange={(e) => setServerUrl(e.target.value)}
                  required
                  className="h-10 text-[13px]"
                />
              </div>

              <div className="space-y-2">
                <Label className="text-[13px] font-medium">Authentication</Label>
                <Select
                  value={authType}
                  onValueChange={(v) => {
                    setAuthType(v as AuthType);
                    setCredentials({});
                  }}
                >
                  <SelectTrigger className="h-10 text-[13px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">None</SelectItem>
                    <SelectItem value="bearer">Bearer Token</SelectItem>
                    <SelectItem value="api_key">API Key</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              {credentialFields[authType].map((field) => (
                <div key={field.key} className="space-y-2">
                  <Label htmlFor={field.key} className="text-[13px] font-medium">
                    {field.label}
                  </Label>
                  <Input
                    id={field.key}
                    type={field.key === "header_name" ? "text" : "password"}
                    value={credentials[field.key] || ""}
                    onChange={(e) =>
                      setCredentials((prev) => ({
                        ...prev,
                        [field.key]: e.target.value,
                      }))
                    }
                    required={field.key !== "header_name"}
                    className="h-10 text-[13px]"
                  />
                </div>
              ))}
            </>
          )}

          <DialogFooter className="gap-2 pt-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => setOpen(false)}
              className="text-[13px]"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={isSubmitDisabled}
              className="text-[13px]"
            >
              {submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
