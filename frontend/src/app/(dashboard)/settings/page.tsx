"use client";

import { useState, useEffect, useCallback } from "react";
import {
  useMcpConnectors,
  useDeleteMcpConnector,
  useTestMcpConnector,
} from "@/hooks/use-mcp-connectors";
import {
  useConnections,
  useUpdateConnection,
  useReconnectConnection,
  useTestConnection,
  useDeleteConnection,
} from "@/hooks/use-connections";
import {
  useAiSettings,
  useUpdateAiSettings,
  useTestAiKey,
} from "@/hooks/use-ai-settings";
import {
  useNetSuiteMetadata,
  useTriggerMetadataDiscovery,
  useMetadataFields,
} from "@/hooks/use-netsuite-metadata";
import {
  useSuiteScriptSyncStatus,
  useTriggerSuiteScriptSync,
} from "@/hooks/use-suitescript-sync";
import { usePlanInfo } from "@/hooks/use-plan";
import { PLAN_TIERS } from "@/lib/constants";
import { AddMcpConnectorDialog } from "@/components/add-mcp-connector-dialog";
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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/hooks/use-toast";
import { apiClient } from "@/lib/api-client";
import { AI_PROVIDERS, AI_MODELS } from "@/lib/constants";
import type { NetSuiteMetadataCategories } from "@/lib/types";
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
  Building2,
  Link2,
  KeyRound,
  ShieldCheck,
  CheckCircle2,
  AlertCircle,
  Pencil,
  Database,
  RefreshCw,
  ChevronDown,
  ChevronRight,
  FileText,
  GitBranch,
  MapPin,
  Users,
  Layers,
  List,
  FileCode,
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

function UsageBar({ used, limit }: { used: number; limit: number }) {
  const isUnlimited = limit === -1;
  const pct = isUnlimited ? 0 : Math.min((used / limit) * 100, 100);

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-[12px]">
        <span className="text-muted-foreground">
          {used} / {isUnlimited ? "Unlimited" : limit}
        </span>
        {!isUnlimited && (
          <span className="text-muted-foreground">{Math.round(pct)}%</span>
        )}
      </div>
      {!isUnlimited && (
        <div className="h-1.5 w-full rounded-full bg-muted">
          <div
            className={`h-1.5 rounded-full transition-all ${pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-amber-500" : "bg-primary"}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </div>
  );
}

function PlanInfoSection() {
  const { data: planInfo, isLoading } = usePlanInfo();

  if (isLoading) {
    return <Skeleton className="h-[140px] rounded-xl" />;
  }

  if (!planInfo) return null;

  const tier = PLAN_TIERS[planInfo.plan] ?? PLAN_TIERS.free;

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold">Plan</h3>
        <p className="mt-0.5 text-[13px] text-muted-foreground">
          Your current plan and usage
        </p>
      </div>

      <div className="rounded-xl border bg-card p-6 shadow-soft space-y-5">
        <div className="flex items-center gap-3">
          <Badge className={`${tier.bg} ${tier.color} border-0 text-[13px] font-semibold px-3 py-0.5`}>
            {tier.label}
          </Badge>
          {planInfo.plan_expires_at && (
            <span className="text-[12px] text-muted-foreground">
              Expires {new Date(planInfo.plan_expires_at).toLocaleDateString()}
            </span>
          )}
        </div>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <div className="space-y-1.5">
            <p className="text-[13px] font-medium">Connections</p>
            <UsageBar
              used={planInfo.usage.connections}
              limit={planInfo.limits.max_connections}
            />
          </div>
          <div className="space-y-1.5">
            <p className="text-[13px] font-medium">Schedules</p>
            <UsageBar
              used={planInfo.usage.schedules}
              limit={planInfo.limits.max_schedules}
            />
          </div>
          <div className="space-y-1.5">
            <p className="text-[13px] font-medium">Features</p>
            <div className="flex flex-wrap gap-2 text-[12px]">
              <span className="flex items-center gap-1">
                {planInfo.limits.chat ? (
                  <Check className="h-3.5 w-3.5 text-green-500" />
                ) : (
                  <X className="h-3.5 w-3.5 text-muted-foreground" />
                )}
                Chat
              </span>
              <span className="flex items-center gap-1">
                {planInfo.limits.mcp_tools ? (
                  <Check className="h-3.5 w-3.5 text-green-500" />
                ) : (
                  <X className="h-3.5 w-3.5 text-muted-foreground" />
                )}
                MCP Tools
              </span>
              <span className="flex items-center gap-1">
                {planInfo.limits.byok_ai ? (
                  <Check className="h-3.5 w-3.5 text-green-500" />
                ) : (
                  <X className="h-3.5 w-3.5 text-muted-foreground" />
                )}
                BYOK AI
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

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
        {aiSettings?.ai_provider && (
          <div className="flex items-center gap-2 rounded-lg bg-muted/50 px-3 py-2">
            <Brain className="h-4 w-4 text-primary" />
            <span className="text-[13px] font-medium">Active model:</span>
            <span className="text-[13px] text-muted-foreground">
              {AI_PROVIDERS.find((p) => p.value === aiSettings.ai_provider)?.label ?? aiSettings.ai_provider}
              {" / "}
              {(aiSettings.ai_model && AI_MODELS[aiSettings.ai_provider]?.find((m) => m.value === aiSettings.ai_model)?.label) || aiSettings.ai_model || "Default"}
            </span>
          </div>
        )}
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

// ---------------------------------------------------------------------------
// Tenant Profile Section
// ---------------------------------------------------------------------------

const INDUSTRIES = [
  "Retail / E-commerce",
  "Wholesale / Distribution",
  "SaaS / Technology",
  "Manufacturing",
  "Professional Services",
  "Healthcare",
  "Other",
];

const TEAM_SIZES = ["1-5", "6-20", "21-50", "51-200", "200+"];

interface TenantProfile {
  id: string;
  industry: string;
  team_size: string | null;
  business_description: string | null;
  version: number;
  status: string;
}

function TenantProfileSection() {
  const [profile, setProfile] = useState<TenantProfile | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isEditing, setIsEditing] = useState(false);
  const [industry, setIndustry] = useState("");
  const [description, setDescription] = useState("");
  const [teamSize, setTeamSize] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const { toast } = useToast();

  useEffect(() => {
    apiClient
      .get<TenantProfile>("/api/v1/onboarding/profiles/active")
      .then((p) => {
        setProfile(p);
        setIndustry(p.industry || "");
        setDescription(p.business_description || "");
        setTeamSize(p.team_size || "");
      })
      .catch(() => {
        setProfile(null);
        setIsEditing(true);
      })
      .finally(() => setIsLoading(false));
  }, []);

  async function handleSave() {
    if (!industry || !description) {
      setError("Industry and business description are required");
      return;
    }
    setIsSaving(true);
    setError("");
    try {
      const created = await apiClient.post<{ id: string }>(
        "/api/v1/onboarding/profiles",
        {
          industry,
          business_description: description,
          team_size: teamSize || undefined,
        },
      );
      await apiClient.post(
        `/api/v1/onboarding/profiles/${created.id}/confirm`,
      );
      const updated = await apiClient.get<TenantProfile>(
        "/api/v1/onboarding/profiles/active",
      );
      setProfile(updated);
      setIsEditing(false);
      toast({ title: "Tenant profile saved" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save profile");
    } finally {
      setIsSaving(false);
    }
  }

  if (isLoading) return <Skeleton className="h-[140px] rounded-xl" />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">Tenant Profile</h3>
          <p className="mt-0.5 text-[13px] text-muted-foreground">
            Your business profile used for AI context
          </p>
        </div>
        {profile && !isEditing && (
          <Button
            variant="outline"
            size="sm"
            className="h-8 text-[12px]"
            onClick={() => setIsEditing(true)}
          >
            <Pencil className="mr-1.5 h-3.5 w-3.5" />
            Edit
          </Button>
        )}
      </div>

      <div className="rounded-xl border bg-card p-6 shadow-soft space-y-4">
        {!isEditing && profile ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Building2 className="h-4 w-4 text-primary" />
              <span className="text-[13px] font-medium">
                {profile.industry}
              </span>
              {profile.team_size && (
                <Badge variant="secondary" className="text-[11px]">
                  {profile.team_size} people
                </Badge>
              )}
            </div>
            {profile.business_description && (
              <p className="text-[13px] text-muted-foreground">
                {profile.business_description}
              </p>
            )}
          </div>
        ) : (
          <div className="space-y-4">
            <div>
              <label className="text-[13px] font-medium">Industry</label>
              <div className="mt-2 grid grid-cols-2 gap-2">
                {INDUSTRIES.map((ind) => (
                  <button
                    key={ind}
                    onClick={() => setIndustry(ind)}
                    className={`rounded-lg border px-3 py-2 text-[13px] text-left transition-colors ${
                      industry === ind
                        ? "border-primary bg-primary/5 text-foreground"
                        : "border-border hover:border-primary/40 text-muted-foreground"
                    }`}
                  >
                    {ind}
                  </button>
                ))}
              </div>
            </div>

            <div className="space-y-1.5">
              <label className="text-[13px] font-medium">
                Business Description
              </label>
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Describe your business..."
                className="w-full rounded-lg border bg-background px-3 py-2 text-[13px] placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring min-h-[80px] resize-none"
              />
            </div>

            <div>
              <label className="text-[13px] font-medium">
                Team Size{" "}
                <span className="text-muted-foreground font-normal">
                  (optional)
                </span>
              </label>
              <div className="mt-2 flex gap-2 flex-wrap">
                {TEAM_SIZES.map((size) => (
                  <button
                    key={size}
                    onClick={() => setTeamSize(size)}
                    className={`rounded-full border px-4 py-1.5 text-[12px] font-medium transition-colors ${
                      teamSize === size
                        ? "border-primary bg-primary/5 text-foreground"
                        : "border-border hover:border-primary/40 text-muted-foreground"
                    }`}
                  >
                    {size}
                  </button>
                ))}
              </div>
            </div>

            {error && (
              <p className="text-[12px] text-destructive">{error}</p>
            )}

            <div className="flex items-center gap-2 pt-1">
              <Button
                size="sm"
                className="h-8 text-[12px]"
                onClick={handleSave}
                disabled={!industry || !description || isSaving}
              >
                {isSaving ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : null}
                Save Profile
              </Button>
              {profile && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 text-[12px]"
                  onClick={() => {
                    setIsEditing(false);
                    setIndustry(profile.industry || "");
                    setDescription(profile.business_description || "");
                    setTeamSize(profile.team_size || "");
                    setError("");
                  }}
                >
                  Cancel
                </Button>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// NetSuite Connection Section
// ---------------------------------------------------------------------------

type ConnectionStatus = "idle" | "connecting" | "connected" | "error";

function ConnectionStatusBadge({ status }: { status: ConnectionStatus }) {
  switch (status) {
    case "connected":
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-green-100 px-2 py-0.5 text-[11px] font-medium text-green-700">
          <CheckCircle2 className="h-3 w-3" />
          Connected
        </span>
      );
    case "connecting":
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-blue-100 px-2 py-0.5 text-[11px] font-medium text-blue-700">
          <Loader2 className="h-3 w-3 animate-spin" />
          Connecting
        </span>
      );
    case "error":
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-red-100 px-2 py-0.5 text-[11px] font-medium text-red-700">
          <AlertCircle className="h-3 w-3" />
          Error
        </span>
      );
    default:
      return (
        <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
          Pending
        </span>
      );
  }
}

function ConnectionManagementCard({
  connection,
}: {
  connection: { id: string; label: string; status: string; auth_type: string | null; provider: string };
}) {
  const { toast } = useToast();
  const updateConn = useUpdateConnection();
  const reconnectConn = useReconnectConnection();
  const testConn = useTestConnection();
  const deleteConn = useDeleteConnection();
  const [editLabel, setEditLabel] = useState(connection.label);
  const [editOpen, setEditOpen] = useState(false);
  const [testResult, setTestResult] = useState<{
    oauth_status?: string;
    restlet_status?: string;
    restlet_error?: string;
  } | null>(null);

  async function handleTest() {
    try {
      const result = await testConn.mutateAsync(connection.id);
      setTestResult({
        oauth_status: result.oauth_status,
        restlet_status: result.restlet_status,
        restlet_error: result.restlet_error,
      });
      toast({
        title: result.status === "ok" ? "Connection OK" : "Connection issue",
        description: result.message,
        variant: result.status === "ok" ? "default" : "destructive",
      });
    } catch (err) {
      setTestResult(null);
      toast({
        title: "Test failed",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleReconnect() {
    try {
      await reconnectConn.mutateAsync(connection.id);
      toast({ title: "Connection reconnected" });
    } catch (err) {
      toast({
        title: "Reconnect failed",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleDisconnect() {
    try {
      await deleteConn.mutateAsync(connection.id);
      toast({ title: "Connection disconnected" });
    } catch (err) {
      toast({
        title: "Failed to disconnect",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  async function handleSaveLabel() {
    if (!editLabel.trim()) return;
    try {
      await updateConn.mutateAsync({ id: connection.id, data: { label: editLabel.trim() } });
      setEditOpen(false);
      toast({ title: "Label updated" });
    } catch (err) {
      toast({
        title: "Failed to update",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  const statusColor =
    connection.status === "active"
      ? "text-green-700 bg-green-100"
      : connection.status === "error"
        ? "text-red-700 bg-red-100"
        : "text-muted-foreground bg-muted";

  return (
    <div className="rounded-lg border p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {connection.provider === "netsuite" ? (
            <KeyRound className="h-4 w-4 text-muted-foreground" />
          ) : (
            <Link2 className="h-4 w-4 text-muted-foreground" />
          )}
          <span className="text-[13px] font-medium">{connection.label}</span>
        </div>
        <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${statusColor}`}>
          {connection.status}
        </span>
      </div>
      {connection.auth_type && (
        <p className="text-[12px] text-muted-foreground">Auth: {connection.auth_type}</p>
      )}
      {testResult && (testResult.oauth_status || testResult.restlet_status) && (
        <div className="flex items-center gap-3 text-[11px]">
          {testResult.oauth_status && (
            <span className="inline-flex items-center gap-1">
              <span className={`h-2 w-2 rounded-full ${testResult.oauth_status === "valid" ? "bg-green-500" : "bg-red-500"}`} />
              OAuth {testResult.oauth_status === "valid" ? "OK" : "Failed"}
            </span>
          )}
          {testResult.restlet_status && (
            <span className="inline-flex items-center gap-1">
              <span className={`h-2 w-2 rounded-full ${testResult.restlet_status === "available" ? "bg-green-500" : "bg-yellow-500"}`} />
              RESTlet {testResult.restlet_status === "available" ? "OK" : "N/A"}
            </span>
          )}
          {testResult.restlet_error && (
            <span className="text-yellow-600">{testResult.restlet_error}</span>
          )}
        </div>
      )}
      <div className="flex items-center gap-2 pt-1">
        <Button
          variant="outline"
          size="sm"
          className="h-7 text-[12px]"
          onClick={handleTest}
          disabled={testConn.isPending}
        >
          {testConn.isPending ? (
            <Loader2 className="mr-1 h-3 w-3 animate-spin" />
          ) : (
            <FlaskConical className="mr-1 h-3 w-3" />
          )}
          Test
        </Button>

        <Dialog open={editOpen} onOpenChange={setEditOpen}>
          <DialogTrigger asChild>
            <Button variant="outline" size="sm" className="h-7 text-[12px]">
              <Pencil className="mr-1 h-3 w-3" />
              Edit
            </Button>
          </DialogTrigger>
          <DialogContent className="sm:max-w-[400px]">
            <DialogHeader>
              <DialogTitle>Edit Connection</DialogTitle>
              <DialogDescription>Update the connection label.</DialogDescription>
            </DialogHeader>
            <div className="space-y-2 py-2">
              <Label className="text-[13px]">Label</Label>
              <Input
                value={editLabel}
                onChange={(e) => setEditLabel(e.target.value)}
                className="h-9 text-[13px]"
              />
            </div>
            <DialogFooter>
              <Button
                size="sm"
                className="h-8 text-[12px]"
                onClick={handleSaveLabel}
                disabled={updateConn.isPending || !editLabel.trim()}
              >
                {updateConn.isPending && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
                Save
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {connection.status !== "active" && (
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-[12px]"
            onClick={handleReconnect}
            disabled={reconnectConn.isPending}
          >
            {reconnectConn.isPending ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            ) : (
              <RefreshCw className="mr-1 h-3 w-3" />
            )}
            Reconnect
          </Button>
        )}

        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button variant="ghost" size="sm" className="h-7 text-[12px] text-destructive hover:text-destructive ml-auto">
              <Trash2 className="mr-1 h-3 w-3" />
              Disconnect
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Disconnect &quot;{connection.label}&quot;?</AlertDialogTitle>
              <AlertDialogDescription>
                This will remove the connection and revoke stored credentials. You can reconnect later.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={handleDisconnect}
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              >
                Disconnect
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </div>
  );
}

function NetSuiteConnectionSection() {
  const { data: mcpConnectors } = useMcpConnectors();
  const { data: connections } = useConnections();
  const { toast } = useToast();

  const [mcpStatus, setMcpStatus] = useState<ConnectionStatus>("idle");
  const [oauthStatus, setOauthStatus] = useState<ConnectionStatus>("idle");
  const [accountId, setAccountId] = useState("");
  const [clientId, setClientId] = useState("");
  const [label, setLabel] = useState("");
  const [errorMessage, setErrorMessage] = useState("");

  // Derive status from fetched data
  useEffect(() => {
    if (mcpConnectors) {
      const hasActiveMcp = mcpConnectors.some(
        (c) =>
          c.provider === "netsuite_mcp" &&
          c.status === "active" &&
          c.is_enabled,
      );
      if (hasActiveMcp) setMcpStatus("connected");
    }
  }, [mcpConnectors]);

  useEffect(() => {
    if (connections) {
      const hasActiveOauth = connections.some(
        (c) => c.provider === "netsuite" && c.status === "active",
      );
      if (hasActiveOauth) setOauthStatus("connected");
    }
  }, [connections]);

  // Listen for OAuth popup messages
  const handleMessage = useCallback(
    (event: MessageEvent) => {
      const apiOrigin =
        process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      if (
        event.origin !== window.location.origin &&
        event.origin !== apiOrigin
      )
        return;
      if (event.data?.type === "NETSUITE_MCP_AUTH_SUCCESS") {
        setMcpStatus("connected");
        setErrorMessage("");
        toast({ title: "NetSuite MCP connector created" });
      } else if (event.data?.type === "NETSUITE_MCP_AUTH_ERROR") {
        setMcpStatus("error");
        setErrorMessage(event.data.error || "MCP authentication failed");
      } else if (event.data?.type === "NETSUITE_AUTH_SUCCESS") {
        setOauthStatus("connected");
        setErrorMessage("");
        toast({ title: "NetSuite OAuth connection created" });
      } else if (event.data?.type === "NETSUITE_AUTH_ERROR") {
        setOauthStatus("error");
        setErrorMessage(event.data.error || "OAuth authentication failed");
      }
    },
    [toast],
  );

  useEffect(() => {
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [handleMessage]);

  function openOAuthPopup(url: string, name: string) {
    const width = 600;
    const height = 700;
    const left = window.screenX + (window.innerWidth - width) / 2;
    const top = window.screenY + (window.innerHeight - height) / 2;
    window.open(
      url,
      name,
      `width=${width},height=${height},left=${left},top=${top},popup=yes`,
    );
  }

  async function handleConnectMcp() {
    if (!accountId || !clientId) return;
    setMcpStatus("connecting");
    setErrorMessage("");
    try {
      const params = new URLSearchParams({
        account_id: accountId,
        client_id: clientId,
        label: label || `NetSuite MCP ${accountId}`,
      });
      const data = await apiClient.get<{
        authorize_url: string;
        state: string;
      }>(`/api/v1/onboarding/netsuite-mcp/authorize?${params}`);
      openOAuthPopup(data.authorize_url, "netsuite_mcp_oauth");
    } catch (err) {
      setMcpStatus("error");
      setErrorMessage(
        err instanceof Error ? err.message : "Failed to start MCP auth",
      );
    }
  }

  async function handleConnectOauth() {
    if (!accountId) return;
    setOauthStatus("connecting");
    setErrorMessage("");
    try {
      const params = new URLSearchParams({ account_id: accountId });
      const data = await apiClient.get<{
        authorize_url: string;
        state: string;
      }>(`/api/v1/onboarding/netsuite-oauth/authorize?${params}`);
      openOAuthPopup(data.authorize_url, "netsuite_oauth");
    } catch (err) {
      setOauthStatus("error");
      setErrorMessage(
        err instanceof Error ? err.message : "Failed to start OAuth auth",
      );
    }
  }

  const bothConnected =
    mcpStatus === "connected" && oauthStatus === "connected";

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold">NetSuite Connections</h3>
        <p className="mt-0.5 text-[13px] text-muted-foreground">
          Connect NetSuite via MCP and OAuth for AI and API access
        </p>
      </div>

      <div className="rounded-xl border bg-card p-6 shadow-soft space-y-4">
        {bothConnected ? (
          <>
            <div className="flex items-center gap-3 rounded-lg bg-green-50 px-4 py-3">
              <CheckCircle2 className="h-5 w-5 text-green-600" />
              <div>
                <p className="text-[13px] font-medium text-green-800">
                  Both connections active
                </p>
                <p className="text-[12px] text-green-600">
                  MCP connector and OAuth API tokens are connected.
                </p>
              </div>
            </div>
            {connections
              ?.filter((c) => c.provider === "netsuite")
              .map((conn) => (
                <ConnectionManagementCard key={conn.id} connection={conn} />
              ))}
          </>
        ) : (
          <>
            {/* Phase A: MCP */}
            <div className="rounded-lg border p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Link2 className="h-4 w-4 text-muted-foreground" />
                  <span className="text-[13px] font-medium">
                    MCP Connector
                  </span>
                </div>
                <ConnectionStatusBadge status={mcpStatus} />
              </div>
              {mcpStatus !== "connected" && (
                <div className="space-y-3">
                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1">
                      <Label className="text-[12px]">Account ID</Label>
                      <Input
                        placeholder="e.g., TSTDRV1234567"
                        value={accountId}
                        onChange={(e) => setAccountId(e.target.value)}
                        className="h-8 text-[12px]"
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-[12px]">Client ID</Label>
                      <Input
                        placeholder="OAuth 2.0 Client ID"
                        value={clientId}
                        onChange={(e) => setClientId(e.target.value)}
                        className="h-8 text-[12px]"
                      />
                    </div>
                  </div>
                  <div className="space-y-1">
                    <Label className="text-[12px]">Label (optional)</Label>
                    <Input
                      placeholder="e.g., Production NetSuite"
                      value={label}
                      onChange={(e) => setLabel(e.target.value)}
                      className="h-8 text-[12px]"
                    />
                  </div>
                  <Button
                    size="sm"
                    className="h-8 text-[12px]"
                    onClick={handleConnectMcp}
                    disabled={
                      !accountId ||
                      !clientId ||
                      mcpStatus === "connecting"
                    }
                  >
                    {mcpStatus === "connecting" ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    ) : null}
                    Connect MCP via OAuth
                  </Button>
                </div>
              )}
            </div>

            {/* Phase B: OAuth */}
            <div className="rounded-lg border p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <KeyRound className="h-4 w-4 text-muted-foreground" />
                  <span className="text-[13px] font-medium">
                    OAuth API Tokens
                  </span>
                </div>
                <ConnectionStatusBadge status={oauthStatus} />
              </div>
              {oauthStatus !== "connected" && (
                <div className="space-y-3">
                  <div className="space-y-1">
                    <Label className="text-[12px]">Account ID</Label>
                    <Input
                      placeholder="Pre-filled from MCP setup"
                      value={accountId}
                      onChange={(e) => setAccountId(e.target.value)}
                      className="h-8 text-[12px]"
                    />
                  </div>
                  <Button
                    size="sm"
                    className="h-8 text-[12px]"
                    onClick={handleConnectOauth}
                    disabled={
                      !accountId || oauthStatus === "connecting"
                    }
                  >
                    {oauthStatus === "connecting" ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    ) : null}
                    Connect OAuth API
                  </Button>
                </div>
              )}
            </div>
          </>
        )}

        {errorMessage && (
          <div className="flex items-start gap-2 rounded-md bg-destructive/10 px-3 py-2 text-destructive text-[12px]">
            <AlertCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
            <span>{errorMessage}</span>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// NetSuite Metadata Discovery Section
// ---------------------------------------------------------------------------

const CATEGORY_META: Record<
  keyof NetSuiteMetadataCategories,
  { label: string; icon: typeof FileText; description: string }
> = {
  transaction_body_fields: {
    label: "Transaction Body Fields",
    icon: FileText,
    description: "custbody_* fields on transactions",
  },
  transaction_column_fields: {
    label: "Transaction Line Fields",
    icon: Layers,
    description: "custcol_* fields on transaction lines",
  },
  entity_custom_fields: {
    label: "Entity Custom Fields",
    icon: Users,
    description: "custentity_* fields on customers, vendors, employees",
  },
  item_custom_fields: {
    label: "Item Custom Fields",
    icon: List,
    description: "custitem_* fields on items",
  },
  custom_record_types: {
    label: "Custom Record Types",
    icon: Database,
    description: "Custom record type definitions",
  },
  custom_lists: {
    label: "Custom Lists",
    icon: List,
    description: "Custom list definitions",
  },
  subsidiaries: {
    label: "Subsidiaries",
    icon: Building2,
    description: "Organizational subsidiary hierarchy",
  },
  departments: {
    label: "Departments",
    icon: GitBranch,
    description: "Department hierarchy",
  },
  classifications: {
    label: "Classes",
    icon: Layers,
    description: "Classification / class hierarchy",
  },
  locations: {
    label: "Locations",
    icon: MapPin,
    description: "Location hierarchy",
  },
};

function MetadataCategoryRow({
  categoryKey,
  count,
}: {
  categoryKey: keyof NetSuiteMetadataCategories;
  count: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const meta = CATEGORY_META[categoryKey];
  const Icon = meta.icon;
  const { data: fieldsData, isLoading } = useMetadataFields(
    expanded ? categoryKey : null,
  );

  return (
    <div className="rounded-lg border">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center justify-between px-3 py-2.5 hover:bg-muted/30 transition-colors"
      >
        <div className="flex items-center gap-2">
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
          )}
          <Icon className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="text-[13px] font-medium">{meta.label}</span>
        </div>
        <Badge variant="secondary" className="text-[11px]">
          {count}
        </Badge>
      </button>

      {expanded && (
        <div className="border-t px-3 py-2 max-h-[200px] overflow-y-auto">
          {isLoading ? (
            <div className="flex items-center gap-2 py-2">
              <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
              <span className="text-[12px] text-muted-foreground">
                Loading fields...
              </span>
            </div>
          ) : fieldsData?.data?.length ? (
            <div className="space-y-1">
              {fieldsData.data.map((item, idx) => (
                <div
                  key={item.scriptid || item.id || idx}
                  className="flex items-center justify-between py-1 text-[12px]"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <code className="rounded bg-muted px-1.5 py-0.5 text-[11px] font-mono shrink-0">
                      {item.scriptid || item.id || "—"}
                    </code>
                    <span className="text-muted-foreground truncate">
                      {item.label || item.name || ""}
                    </span>
                  </div>
                  {item.fieldtype && (
                    <span className="text-[11px] text-muted-foreground shrink-0 ml-2">
                      {item.fieldtype}
                    </span>
                  )}
                  {item.isinactive === "T" && (
                    <Badge
                      variant="outline"
                      className="text-[10px] text-amber-600 shrink-0 ml-2"
                    >
                      Inactive
                    </Badge>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <p className="py-2 text-[12px] text-muted-foreground">
              No data discovered for this category.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function NetSuiteMetadataSection() {
  const { data: metadata, isLoading } = useNetSuiteMetadata();
  const triggerDiscovery = useTriggerMetadataDiscovery();
  const { toast } = useToast();

  async function handleDiscover() {
    try {
      await triggerDiscovery.mutateAsync();
      toast({
        title: "Metadata discovery started",
        description:
          "Discovering custom fields, record types, and org hierarchy in the background...",
      });
    } catch (err) {
      toast({
        title: "Failed to start discovery",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  if (isLoading) {
    return <Skeleton className="h-[200px] rounded-xl" />;
  }

  const isDiscovered =
    metadata && metadata.status === "completed" && metadata.categories;
  const isPending = metadata && metadata.status === "pending";
  const totalFields = metadata?.total_fields_discovered ?? 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">NetSuite Metadata</h3>
          <p className="mt-0.5 text-[13px] text-muted-foreground">
            Custom fields, record types, and org hierarchy discovered from your
            NetSuite account
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-8 text-[12px]"
          onClick={handleDiscover}
          disabled={triggerDiscovery.isPending || isPending === true}
        >
          {triggerDiscovery.isPending || isPending ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
          )}
          {isPending ? "Discovering..." : "Refresh Metadata"}
        </Button>
      </div>

      <div className="rounded-xl border bg-card p-6 shadow-soft space-y-4">
        {isPending && (
          <div className="flex items-center gap-3 rounded-lg bg-blue-50 px-4 py-3">
            <Loader2 className="h-5 w-5 text-blue-600 animate-spin" />
            <div>
              <p className="text-[13px] font-medium text-blue-800">
                Discovery in progress
              </p>
              <p className="text-[12px] text-blue-600">
                Querying NetSuite for custom fields, record types, and
                organizational data. This usually takes 15-30 seconds.
              </p>
            </div>
          </div>
        )}

        {metadata?.status === "failed" && (
          <div className="flex items-center gap-3 rounded-lg bg-red-50 px-4 py-3">
            <AlertCircle className="h-5 w-5 text-red-600" />
            <div>
              <p className="text-[13px] font-medium text-red-800">
                Discovery failed
              </p>
              <p className="text-[12px] text-red-600">
                Some queries could not complete. Try refreshing, or check that
                your NetSuite connection is active.
              </p>
            </div>
          </div>
        )}

        {!isDiscovered && !isPending && metadata?.status !== "failed" && (
          <div className="flex flex-col items-center justify-center py-8">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-muted">
              <Database className="h-6 w-6 text-muted-foreground" />
            </div>
            <p className="mt-4 text-[15px] font-medium text-foreground">
              No metadata discovered yet
            </p>
            <p className="mt-1 mb-4 text-[13px] text-muted-foreground text-center max-w-md">
              Run metadata discovery to pull custom fields, record types,
              subsidiaries, departments, classes, and locations from your
              NetSuite account. This enriches the AI chat with your specific
              customizations.
            </p>
            <Button
              size="sm"
              className="h-8 text-[12px]"
              onClick={handleDiscover}
              disabled={triggerDiscovery.isPending}
            >
              {triggerDiscovery.isPending ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <Database className="mr-1.5 h-3.5 w-3.5" />
              )}
              Discover Metadata
            </Button>
          </div>
        )}

        {isDiscovered && metadata.categories && (
          <>
            {/* Summary banner */}
            <div className="flex items-center gap-3 rounded-lg bg-green-50 px-4 py-3">
              <CheckCircle2 className="h-5 w-5 text-green-600" />
              <div className="flex-1">
                <p className="text-[13px] font-medium text-green-800">
                  {totalFields} fields discovered across{" "}
                  {metadata.queries_succeeded} categories
                </p>
                {metadata.discovered_at && (
                  <p className="text-[12px] text-green-600">
                    Last discovered{" "}
                    {new Date(metadata.discovered_at).toLocaleString()}
                    {metadata.version > 1 && ` (v${metadata.version})`}
                  </p>
                )}
              </div>
            </div>

            {/* Discovery errors (partial failures) */}
            {metadata.discovery_errors &&
              Object.keys(metadata.discovery_errors).length > 0 && (
                <div className="flex items-start gap-2 rounded-md bg-amber-50 px-3 py-2 text-[12px]">
                  <AlertCircle className="h-3.5 w-3.5 mt-0.5 text-amber-600 shrink-0" />
                  <div>
                    <span className="font-medium text-amber-800">
                      Some queries had errors:
                    </span>
                    <ul className="mt-1 text-amber-700 space-y-0.5">
                      {Object.entries(metadata.discovery_errors).map(
                        ([key, msg]) => (
                          <li key={key}>
                            {key}: {msg}
                          </li>
                        ),
                      )}
                    </ul>
                  </div>
                </div>
              )}

            {/* Category breakdown */}
            <div className="space-y-2">
              {(
                Object.entries(metadata.categories) as [
                  keyof NetSuiteMetadataCategories,
                  number,
                ][]
              )
                .filter(([, count]) => count > 0)
                .sort(([, a], [, b]) => b - a)
                .map(([key, count]) => (
                  <MetadataCategoryRow
                    key={key}
                    categoryKey={key}
                    count={count}
                  />
                ))}
            </div>

            {/* Categories with zero results */}
            {(
              Object.entries(metadata.categories) as [
                keyof NetSuiteMetadataCategories,
                number,
              ][]
            ).filter(([, count]) => count === 0).length > 0 && (
              <div className="text-[12px] text-muted-foreground">
                No data found for:{" "}
                {(
                  Object.entries(metadata.categories) as [
                    keyof NetSuiteMetadataCategories,
                    number,
                  ][]
                )
                  .filter(([, count]) => count === 0)
                  .map(
                    ([key]) =>
                      CATEGORY_META[key]?.label ?? key,
                  )
                  .join(", ")}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SuiteScript Files Section
// ---------------------------------------------------------------------------

function SuiteScriptFilesSection() {
  const { data: syncStatus, isLoading } = useSuiteScriptSyncStatus();
  const triggerSync = useTriggerSuiteScriptSync();
  const { toast } = useToast();

  async function handleSync() {
    try {
      await triggerSync.mutateAsync();
      toast({
        title: "SuiteScript sync started",
        description:
          "Discovering and loading JavaScript files from your NetSuite account...",
      });
    } catch (err) {
      toast({
        title: "Failed to start sync",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    }
  }

  if (isLoading) {
    return <Skeleton className="h-[120px] rounded-xl" />;
  }

  const isSyncing = syncStatus?.status === "in_progress";
  const isCompleted = syncStatus?.status === "completed";
  const isFailed = syncStatus?.status === "failed";
  const filesLoaded = syncStatus?.total_files_loaded ?? 0;
  const filesFailed = syncStatus?.failed_files_count ?? 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">SuiteScript Files</h3>
          <p className="mt-0.5 text-[13px] text-muted-foreground">
            Sync JavaScript files from your NetSuite File Cabinet into the
            workspace
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-8 text-[12px]"
          onClick={handleSync}
          disabled={triggerSync.isPending || isSyncing}
        >
          {triggerSync.isPending || isSyncing ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
          )}
          {isSyncing ? "Syncing..." : "Sync Scripts"}
        </Button>
      </div>

      <div className="rounded-xl border bg-card p-6 shadow-soft space-y-3">
        {isSyncing && (
          <div className="flex items-center gap-3 rounded-lg bg-blue-50 px-4 py-3">
            <Loader2 className="h-5 w-5 text-blue-600 animate-spin" />
            <div>
              <p className="text-[13px] font-medium text-blue-800">
                Sync in progress
              </p>
              <p className="text-[12px] text-blue-600">
                Discovering JavaScript files and fetching content from NetSuite.
                This may take 30-60 seconds.
              </p>
            </div>
          </div>
        )}

        {isFailed && (
          <div className="flex items-center gap-3 rounded-lg bg-red-50 px-4 py-3">
            <AlertCircle className="h-5 w-5 text-red-600" />
            <div>
              <p className="text-[13px] font-medium text-red-800">
                Sync failed
              </p>
              <p className="text-[12px] text-red-600">
                {syncStatus?.error_message ||
                  "Something went wrong. Try again or check your NetSuite connection."}
              </p>
            </div>
          </div>
        )}

        {isCompleted && (
          <div className="flex items-center gap-3 rounded-lg bg-green-50 px-4 py-3">
            <CheckCircle2 className="h-5 w-5 text-green-600" />
            <div className="flex-1">
              <p className="text-[13px] font-medium text-green-800">
                {filesLoaded} file{filesLoaded !== 1 ? "s" : ""} loaded
                {filesFailed > 0 && (
                  <span className="text-amber-700">
                    {" "}
                    ({filesFailed} failed)
                  </span>
                )}
              </p>
              {syncStatus?.last_sync_at && (
                <p className="text-[12px] text-green-600">
                  Last synced{" "}
                  {new Date(syncStatus.last_sync_at).toLocaleString()}
                </p>
              )}
            </div>
          </div>
        )}

        {!isCompleted && !isSyncing && !isFailed && (
          <div className="flex flex-col items-center justify-center py-6">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-muted">
              <FileCode className="h-6 w-6 text-muted-foreground" />
            </div>
            <p className="mt-4 text-[15px] font-medium text-foreground">
              No scripts synced yet
            </p>
            <p className="mt-1 mb-4 text-[13px] text-muted-foreground text-center max-w-md">
              Sync SuiteScript files from your NetSuite account to browse and
              edit them in the workspace.
            </p>
            <Button
              size="sm"
              className="h-8 text-[12px]"
              onClick={handleSync}
              disabled={triggerSync.isPending}
            >
              {triggerSync.isPending ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <FileCode className="mr-1.5 h-3.5 w-3.5" />
              )}
              Sync Scripts
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Governance Policy Section
// ---------------------------------------------------------------------------

const TOOL_OPTIONS = [
  "netsuite.suiteql",
  "netsuite.connectivity",
  "workspace.list_files",
  "workspace.read_file",
  "workspace.search",
  "workspace.propose_patch",
];

interface PolicyData {
  id: string;
  name: string;
  read_only_mode: boolean;
  sensitivity_default: string;
  tool_allowlist: string[] | null;
  max_rows_per_query: number;
  require_row_limit: boolean;
  is_active: boolean;
  is_locked: boolean;
}

function GovernancePolicySection() {
  const [policy, setPolicy] = useState<PolicyData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isEditing, setIsEditing] = useState(false);

  const [readOnlyMode, setReadOnlyMode] = useState(true);
  const [sensitivityDefault, setSensitivityDefault] = useState("financial");
  const [enforceToolAllowlist, setEnforceToolAllowlist] = useState(false);
  const [toolAllowlist, setToolAllowlist] = useState<string[]>([]);
  const [maxRows, setMaxRows] = useState(1000);
  const [requireRowLimit, setRequireRowLimit] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const { toast } = useToast();

  useEffect(() => {
    apiClient
      .get<PolicyData[]>("/api/v1/policies")
      .then((policies) => {
        const active = policies.find((p) => p.is_active);
        if (active) {
          setPolicy(active);
          populateForm(active);
        } else {
          setIsEditing(true);
        }
      })
      .catch(() => setIsEditing(true))
      .finally(() => setIsLoading(false));
  }, []);

  function populateForm(p: PolicyData) {
    setReadOnlyMode(p.read_only_mode ?? true);
    setSensitivityDefault(p.sensitivity_default ?? "financial");
    setEnforceToolAllowlist(!!p.tool_allowlist && p.tool_allowlist.length > 0);
    setToolAllowlist(p.tool_allowlist ?? []);
    setMaxRows(p.max_rows_per_query ?? 1000);
    setRequireRowLimit(p.require_row_limit ?? true);
  }

  function toggleTool(tool: string) {
    setToolAllowlist((prev) =>
      prev.includes(tool) ? prev.filter((t) => t !== tool) : [...prev, tool],
    );
  }

  async function handleSave() {
    setIsSaving(true);
    setError("");
    try {
      await apiClient.post("/api/v1/onboarding/setup-policy", {
        read_only_mode: readOnlyMode,
        sensitivity_default: sensitivityDefault,
        tool_allowlist: enforceToolAllowlist ? toolAllowlist : null,
        max_rows_per_query: maxRows,
        require_row_limit: requireRowLimit,
      });
      // Refetch active policy
      const policies = await apiClient.get<PolicyData[]>("/api/v1/policies");
      const active = policies.find((p) => p.is_active);
      if (active) {
        setPolicy(active);
        populateForm(active);
      }
      setIsEditing(false);
      toast({ title: "Governance policy saved" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save policy");
    } finally {
      setIsSaving(false);
    }
  }

  if (isLoading) return <Skeleton className="h-[200px] rounded-xl" />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">Governance Policy</h3>
          <p className="mt-0.5 text-[13px] text-muted-foreground">
            Control AI access, query limits, and data sensitivity
          </p>
        </div>
        {policy && !isEditing && (
          <Button
            variant="outline"
            size="sm"
            className="h-8 text-[12px]"
            onClick={() => setIsEditing(true)}
          >
            <Pencil className="mr-1.5 h-3.5 w-3.5" />
            Edit
          </Button>
        )}
      </div>

      <div className="rounded-xl border bg-card p-6 shadow-soft space-y-4">
        {!isEditing && policy ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-primary" />
              <span className="text-[13px] font-medium">{policy.name}</span>
              {policy.is_locked && (
                <Badge variant="secondary" className="text-[11px]">
                  Locked
                </Badge>
              )}
            </div>
            <div className="grid grid-cols-2 gap-3 text-[13px]">
              <div className="flex items-center justify-between rounded-lg bg-muted/50 px-3 py-2">
                <span className="text-muted-foreground">Read-only</span>
                <span className="font-medium">
                  {policy.read_only_mode ? "Yes" : "No"}
                </span>
              </div>
              <div className="flex items-center justify-between rounded-lg bg-muted/50 px-3 py-2">
                <span className="text-muted-foreground">Sensitivity</span>
                <span className="font-medium capitalize">
                  {policy.sensitivity_default}
                </span>
              </div>
              <div className="flex items-center justify-between rounded-lg bg-muted/50 px-3 py-2">
                <span className="text-muted-foreground">Max rows</span>
                <span className="font-medium">
                  {policy.max_rows_per_query}
                </span>
              </div>
              <div className="flex items-center justify-between rounded-lg bg-muted/50 px-3 py-2">
                <span className="text-muted-foreground">Row limit</span>
                <span className="font-medium">
                  {policy.require_row_limit ? "Required" : "Optional"}
                </span>
              </div>
            </div>
            {policy.tool_allowlist && policy.tool_allowlist.length > 0 && (
              <div className="space-y-1">
                <span className="text-[12px] text-muted-foreground">
                  Allowed tools:
                </span>
                <div className="flex flex-wrap gap-1.5">
                  {policy.tool_allowlist.map((tool) => (
                    <Badge
                      key={tool}
                      variant="outline"
                      className="text-[11px] font-mono"
                    >
                      {tool}
                    </Badge>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-4">
            <ToggleSetting
              title="Read-only Mode"
              description="Only allow read operations on NetSuite data"
              enabled={readOnlyMode}
              onToggle={() => setReadOnlyMode(!readOnlyMode)}
            />

            <ToggleSetting
              title="Require Row Limit"
              description="All queries must include a row limit"
              enabled={requireRowLimit}
              onToggle={() => setRequireRowLimit(!requireRowLimit)}
            />

            <div className="rounded-lg border p-4">
              <label className="text-[13px] font-medium">
                Default Sensitivity
              </label>
              <p className="text-[12px] text-muted-foreground mt-0.5 mb-2">
                Classify default output sensitivity
              </p>
              <select
                value={sensitivityDefault}
                onChange={(e) => setSensitivityDefault(e.target.value)}
                className="w-full rounded-lg border bg-background px-3 py-2 text-[13px] focus:outline-none focus:ring-1 focus:ring-ring"
              >
                <option value="financial">Financial</option>
                <option value="non_financial">Non-financial</option>
                <option value="mixed">Mixed</option>
              </select>
            </div>

            <div className="rounded-lg border p-4">
              <div className="flex items-center justify-between">
                <div>
                  <label className="text-[13px] font-medium">
                    Tool Allowlist
                  </label>
                  <p className="text-[12px] text-muted-foreground mt-0.5">
                    Restrict the AI to specific tools
                  </p>
                </div>
                <ToggleSwitch
                  enabled={enforceToolAllowlist}
                  onToggle={() =>
                    setEnforceToolAllowlist(!enforceToolAllowlist)
                  }
                />
              </div>
              {enforceToolAllowlist && (
                <div className="mt-3 grid grid-cols-2 gap-2">
                  {TOOL_OPTIONS.map((tool) => (
                    <label
                      key={tool}
                      className="flex items-center gap-2 text-[12px] text-muted-foreground"
                    >
                      <input
                        type="checkbox"
                        checked={toolAllowlist.includes(tool)}
                        onChange={() => toggleTool(tool)}
                      />
                      <span className="font-mono">{tool}</span>
                    </label>
                  ))}
                </div>
              )}
            </div>

            <div className="rounded-lg border p-4">
              <label className="text-[13px] font-medium">
                Max Rows per Query
              </label>
              <p className="text-[12px] text-muted-foreground mt-0.5 mb-2">
                Maximum rows returned by a single query
              </p>
              <input
                type="number"
                value={maxRows}
                onChange={(e) => setMaxRows(Number(e.target.value))}
                min={1}
                max={10000}
                className="w-full rounded-lg border bg-background px-3 py-2 text-[13px] focus:outline-none focus:ring-1 focus:ring-ring"
              />
            </div>

            {error && (
              <p className="text-[12px] text-destructive">{error}</p>
            )}

            <div className="flex items-center gap-2 pt-1">
              <Button
                size="sm"
                className="h-8 text-[12px]"
                onClick={handleSave}
                disabled={isSaving}
              >
                {isSaving ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : null}
                Save Policy
              </Button>
              {policy && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 text-[12px]"
                  onClick={() => {
                    setIsEditing(false);
                    populateForm(policy);
                    setError("");
                  }}
                >
                  Cancel
                </Button>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function ToggleSetting({
  title,
  description,
  enabled,
  onToggle,
}: {
  title: string;
  description: string;
  enabled: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="flex items-center justify-between rounded-lg border p-4">
      <div>
        <label className="text-[13px] font-medium">{title}</label>
        <p className="text-[12px] text-muted-foreground mt-0.5">
          {description}
        </p>
      </div>
      <ToggleSwitch enabled={enabled} onToggle={onToggle} />
    </div>
  );
}

function ToggleSwitch({
  enabled,
  onToggle,
}: {
  enabled: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      onClick={onToggle}
      className={`relative h-6 w-11 rounded-full transition-colors ${
        enabled ? "bg-primary" : "bg-muted-foreground/20"
      }`}
    >
      <span
        className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${
          enabled ? "translate-x-5" : "translate-x-0"
        }`}
      />
    </button>
  );
}

// ---------------------------------------------------------------------------
// Main Settings Page
// ---------------------------------------------------------------------------

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

      {/* Plan Info Section */}
      <PlanInfoSection />

      {/* AI Configuration Section */}
      <AiConfigSection />

      {/* Tenant Profile Section */}
      <TenantProfileSection />

      {/* NetSuite Connection Section */}
      <NetSuiteConnectionSection />

      {/* NetSuite Metadata Discovery Section */}
      <NetSuiteMetadataSection />

      {/* SuiteScript Files Section */}
      <SuiteScriptFilesSection />

      {/* Governance Policy Section */}
      <GovernancePolicySection />

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
