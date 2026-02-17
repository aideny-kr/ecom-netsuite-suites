"use client";

import { useState, useEffect } from "react";
import {
  useMcpConnectors,
  useDeleteMcpConnector,
  useTestMcpConnector,
} from "@/hooks/use-mcp-connectors";
import {
  useAiSettings,
  useUpdateAiSettings,
  useTestAiKey,
} from "@/hooks/use-ai-settings";
import { AddMcpConnectorDialog } from "@/components/add-mcp-connector-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import { AI_PROVIDERS, AI_MODELS } from "@/lib/constants";
import {
  Trash2,
  Settings,
  Server,
  FlaskConical,
  Wrench,
  Brain,
  Check,
  X,
  Loader2,
} from "lucide-react";

const providerMeta: Record<
  string,
  { icon: typeof Server; color: string; bg: string; label: string }
> = {
  netsuite_mcp: {
    icon: Server,
    color: "text-blue-600",
    bg: "bg-blue-50",
    label: "NetSuite MCP",
  },
  shopify_mcp: {
    icon: Server,
    color: "text-green-600",
    bg: "bg-green-50",
    label: "Shopify MCP",
  },
  custom: {
    icon: Server,
    color: "text-gray-600",
    bg: "bg-gray-50",
    label: "Custom",
  },
};

function AiConfigSection() {
  const { data: aiSettings, isLoading } = useAiSettings();
  const updateSettings = useUpdateAiSettings();
  const testKey = useTestAiKey();
  const { toast } = useToast();

  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [isDirty, setIsDirty] = useState(false);

  useEffect(() => {
    if (aiSettings) {
      setProvider(aiSettings.ai_provider ?? "");
      setModel(aiSettings.ai_model ?? "");
      setApiKey("");
      setIsDirty(false);
    }
  }, [aiSettings]);

  const models = provider ? AI_MODELS[provider] ?? [] : [];

  async function handleSave() {
    try {
      const payload: Record<string, string | null> = {};

      if (provider === "") {
        payload.ai_provider = null;
        payload.ai_model = null;
        payload.ai_api_key = null;
      } else {
        payload.ai_provider = provider;
        if (model) payload.ai_model = model;
        if (apiKey) payload.ai_api_key = apiKey;
      }

      await updateSettings.mutateAsync(payload);
      setApiKey("");
      setIsDirty(false);
      toast({ title: "AI settings saved" });
    } catch (err) {
      toast({
        title: "Failed to save AI settings",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleTest() {
    if (!provider || !apiKey) {
      toast({
        title: "Enter a provider and API key to test",
        variant: "destructive",
      });
      return;
    }
    try {
      const result = await testKey.mutateAsync({
        provider,
        api_key: apiKey,
        model: model || undefined,
      });
      toast({
        title: result.valid ? "API key is valid" : "API key test failed",
        description: result.error || undefined,
        variant: result.valid ? "default" : "destructive",
      });
    } catch (err) {
      toast({
        title: "Test failed",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  if (isLoading) {
    return <Skeleton className="h-[200px] rounded-xl" />;
  }

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold">AI Configuration</h3>
        <p className="mt-0.5 text-[13px] text-muted-foreground">
          Configure your AI provider for chat
        </p>
      </div>

      <div className="rounded-xl border bg-card p-6 shadow-soft space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <label className="text-[13px] font-medium text-foreground">
              Provider
            </label>
            <select
              value={provider}
              onChange={(e) => {
                setProvider(e.target.value);
                setModel("");
                setIsDirty(true);
              }}
              className="flex h-9 w-full rounded-md border border-input bg-background px-3 text-[13px] ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {AI_PROVIDERS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </div>

          {provider && (
            <div className="space-y-1.5">
              <label className="text-[13px] font-medium text-foreground">
                Model
              </label>
              <select
                value={model}
                onChange={(e) => {
                  setModel(e.target.value);
                  setIsDirty(true);
                }}
                className="flex h-9 w-full rounded-md border border-input bg-background px-3 text-[13px] ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="">Default</option>
                {models.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>

        {provider && (
          <div className="space-y-1.5">
            <label className="text-[13px] font-medium text-foreground">
              API Key
            </label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => {
                setApiKey(e.target.value);
                setIsDirty(true);
              }}
              placeholder={
                aiSettings?.ai_api_key_set
                  ? "Key is set (enter new value to change)"
                  : "Enter your API key"
              }
              className="flex h-9 w-full rounded-md border border-input bg-background px-3 text-[13px] ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
        )}

        <div className="flex items-center justify-between pt-2">
          <div className="flex items-center gap-2">
            {provider && apiKey && (
              <Button
                variant="outline"
                size="sm"
                className="h-8 text-[12px]"
                onClick={handleTest}
                disabled={testKey.isPending}
              >
                {testKey.isPending ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <FlaskConical className="mr-1.5 h-3.5 w-3.5" />
                )}
                Test Key
              </Button>
            )}
          </div>

          <div className="flex items-center gap-3">
            <span className="text-[12px] text-muted-foreground">
              {!provider ? (
                <span className="flex items-center gap-1">
                  <Check className="h-3.5 w-3.5 text-green-500" />
                  Using platform default
                </span>
              ) : aiSettings?.ai_api_key_set ? (
                <span className="flex items-center gap-1">
                  <Check className="h-3.5 w-3.5 text-green-500" />
                  Custom key configured
                </span>
              ) : (
                <span className="flex items-center gap-1">
                  <X className="h-3.5 w-3.5 text-amber-500" />
                  No key set
                </span>
              )}
            </span>

            <Button
              size="sm"
              className="h-8 text-[12px]"
              onClick={handleSave}
              disabled={!isDirty || updateSettings.isPending}
            >
              {updateSettings.isPending ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : null}
              Save
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const { data: connectors, isLoading } = useMcpConnectors();
  const deleteConnector = useDeleteMcpConnector();
  const testConnector = useTestMcpConnector();
  const { toast } = useToast();

  async function handleDelete(id: string) {
    try {
      await deleteConnector.mutateAsync(id);
      toast({ title: "MCP connector deleted" });
    } catch (err) {
      toast({
        title: "Failed to delete connector",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleTest(id: string) {
    try {
      const result = await testConnector.mutateAsync(id);
      toast({
        title:
          result.status === "ok" ? "Connection successful" : "Connection failed",
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

  return (
    <div className="space-y-8 animate-fade-in">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Settings</h2>
        <p className="mt-1 text-[15px] text-muted-foreground">
          Configure your workspace and integrations
        </p>
      </div>

      {/* AI Configuration Section */}
      <AiConfigSection />

      {/* MCP Connectors Section */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-lg font-semibold">MCP Connectors</h3>
            <p className="mt-0.5 text-[13px] text-muted-foreground">
              Connect external MCP servers for real-time data queries in Chat
            </p>
          </div>
          <AddMcpConnectorDialog />
        </div>

        {isLoading ? (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-[180px] rounded-xl" />
            ))}
          </div>
        ) : !connectors?.length ? (
          <div className="flex flex-col items-center justify-center rounded-xl border border-dashed bg-card py-16">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-muted">
              <Settings className="h-6 w-6 text-muted-foreground" />
            </div>
            <p className="mt-4 text-[15px] font-medium text-foreground">
              No MCP connectors yet
            </p>
            <p className="mt-1 mb-5 text-[13px] text-muted-foreground">
              Add an MCP server to enable real-time external queries.
            </p>
            <AddMcpConnectorDialog />
          </div>
        ) : (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {connectors.map((conn) => {
              const meta = providerMeta[conn.provider] || {
                icon: Server,
                color: "text-muted-foreground",
                bg: "bg-muted",
                label: conn.provider,
              };
              const ProviderIcon = meta.icon;
              const toolCount = conn.discovered_tools?.length ?? 0;

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
                        <p className="text-[13px] text-muted-foreground">
                          {meta.label}
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

                  <p className="mt-3 truncate text-[12px] text-muted-foreground">
                    {conn.server_url}
                  </p>

                  {toolCount > 0 && (
                    <div className="mt-2 flex items-center gap-1 text-[12px] text-muted-foreground">
                      <Wrench className="h-3 w-3" />
                      {toolCount} tool{toolCount !== 1 ? "s" : ""} discovered
                    </div>
                  )}

                  <div className="mt-4 flex items-center justify-between border-t pt-3">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-8 text-[12px]"
                      onClick={() => handleTest(conn.id)}
                      disabled={testConnector.isPending}
                    >
                      <FlaskConical className="mr-1.5 h-3.5 w-3.5" />
                      {testConnector.isPending ? "Testing..." : "Test"}
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8 opacity-0 transition-opacity group-hover:opacity-100"
                      onClick={() => handleDelete(conn.id)}
                      disabled={deleteConnector.isPending}
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
    </div>
  );
}
