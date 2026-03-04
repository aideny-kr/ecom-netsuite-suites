"use client";

import { useState } from "react";
import { useAuth } from "@/providers/auth-provider";
import {
  useAdminTenants,
  usePlatformStats,
  useImpersonateTenant,
  useTenantFeatures,
  useUpdateTenantFeatures,
} from "@/hooks/use-admin-data";
import {
  Building2,
  Users,
  CreditCard,
  TrendingUp,
  UserCheck,
  Copy,
  Check,
  Flag,
  X,
  Loader2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { AdminTenant } from "@/lib/types";

function StatCard({
  label,
  value,
  icon: Icon,
  color,
}: {
  label: string;
  value: string | number;
  icon: React.ElementType;
  color: string;
}) {
  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-[13px] text-muted-foreground">{label}</p>
          <p className="mt-1 text-2xl font-semibold tracking-tight text-foreground">
            {value}
          </p>
        </div>
        <div className={cn("flex h-10 w-10 items-center justify-center rounded-lg", color)}>
          <Icon className="h-5 w-5" />
        </div>
      </div>
    </div>
  );
}

const FLAG_LABELS: Record<string, { label: string; description: string }> = {
  chat: { label: "Chat", description: "AI chat assistant" },
  mcp_tools: { label: "MCP Tools", description: "External MCP server connections" },
  workspace: { label: "Workspace", description: "Dev workspace & file management" },
  reconciliation: { label: "Reconciliation", description: "Payment reconciliation engine" },
  byok_ai: { label: "BYOK AI", description: "Bring your own AI API key" },
  custom_branding: { label: "Custom Branding", description: "White-label branding" },
  custom_domain: { label: "Custom Domain", description: "Custom domain mapping" },
  analytics_export: { label: "Analytics Export", description: "CSV/data export" },
};

function FeatureFlagPanel({
  tenantId,
  tenantName,
  onClose,
}: {
  tenantId: string;
  tenantName: string;
  onClose: () => void;
}) {
  const { data, isLoading } = useTenantFeatures(tenantId);
  const updateFeatures = useUpdateTenantFeatures();

  async function handleToggle(flagKey: string, enabled: boolean) {
    await updateFeatures.mutateAsync({
      tenantId,
      flags: { [flagKey]: enabled },
    });
  }

  return (
    <>
      {/* Backdrop */}
      <div className="fixed inset-0 z-40 bg-black/40" onClick={onClose} />
      {/* Slide-over panel */}
      <div className="fixed inset-y-0 right-0 z-50 w-[380px] overflow-y-auto border-l bg-card shadow-xl animate-fade-in">
        <div className="sticky top-0 z-10 flex items-center justify-between border-b bg-card px-5 py-4">
          <div>
            <h3 className="text-[15px] font-semibold text-foreground">
              Feature Flags — {tenantName}
            </h3>
            <p className="text-[11px] text-muted-foreground">
              Toggle features for this tenant
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="divide-y">
        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : (
          Object.entries(FLAG_LABELS).map(([key, meta]) => {
            const enabled = data?.flags?.[key] ?? false;
            return (
              <div key={key} className="flex items-center justify-between px-5 py-3">
                <div>
                  <p className="text-[13px] font-medium text-foreground">{meta.label}</p>
                  <p className="text-[11px] text-muted-foreground">{meta.description}</p>
                </div>
                <button
                  onClick={() => handleToggle(key, !enabled)}
                  disabled={updateFeatures.isPending}
                  className={cn(
                    "relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none disabled:opacity-50",
                    enabled ? "bg-primary" : "bg-muted",
                  )}
                >
                  <span
                    className={cn(
                      "pointer-events-none inline-block h-5 w-5 rounded-full bg-white shadow-sm ring-0 transition-transform duration-200 ease-in-out",
                      enabled ? "translate-x-5" : "translate-x-0",
                    )}
                  />
                </button>
              </div>
            );
          })
        )}
      </div>
      </div>
    </>
  );
}

export default function AdminDashboardPage() {
  const { user } = useAuth();
  const { data: tenants, isLoading: tenantsLoading } = useAdminTenants();
  const { data: stats, isLoading: statsLoading } = usePlatformStats();
  const impersonate = useImpersonateTenant();
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [flagsTenantId, setFlagTenantId] = useState<string | null>(null);
  const flagsTenant = tenants?.find((t) => t.id === flagsTenantId);

  const handleImpersonate = async (tenant: AdminTenant) => {
    try {
      // Save original admin token before impersonating
      const currentToken = localStorage.getItem("access_token");
      if (currentToken) {
        localStorage.setItem("admin_token", currentToken);
        localStorage.setItem("impersonating_tenant", tenant.name);
      }
      const result = await impersonate.mutateAsync(tenant.id);
      localStorage.setItem("access_token", result.access_token);
      document.cookie = `access_token=${result.access_token}; path=/; max-age=${60 * 60}; samesite=lax`;
      window.location.href = "/dashboard";
    } catch {
      // Error handled by mutation state
    }
  };

  const handleCopyId = (id: string) => {
    navigator.clipboard.writeText(id);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  return (
    <div className="space-y-8 animate-fade-in">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight text-foreground">
          Admin Dashboard
        </h2>
        <p className="mt-1 text-[15px] text-muted-foreground">
          Platform-wide tenant management and billing overview
        </p>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="Active Tenants"
          value={statsLoading ? "..." : stats?.active_tenants ?? 0}
          icon={Building2}
          color="bg-blue-100 text-blue-600"
        />
        <StatCard
          label="Total Users"
          value={statsLoading ? "..." : stats?.total_users ?? 0}
          icon={Users}
          color="bg-emerald-100 text-emerald-600"
        />
        <StatCard
          label="Base Credits Remaining"
          value={
            statsLoading
              ? "..."
              : (stats?.total_base_credits_remaining ?? 0).toLocaleString()
          }
          icon={CreditCard}
          color="bg-purple-100 text-purple-600"
        />
        <StatCard
          label="Metered Credits Used"
          value={
            statsLoading
              ? "..."
              : (stats?.total_metered_credits_used ?? 0).toLocaleString()
          }
          icon={TrendingUp}
          color="bg-amber-100 text-amber-600"
        />
      </div>

      {/* Tenants Table */}
      <div className="rounded-xl border bg-card shadow-soft">
        <div className="border-b px-5 py-4">
          <h3 className="text-[15px] font-semibold text-foreground">
            All Tenants
          </h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b text-left text-[13px] text-muted-foreground">
                <th className="px-5 py-3 font-medium">Tenant</th>
                <th className="px-5 py-3 font-medium">Plan</th>
                <th className="px-5 py-3 font-medium">Users</th>
                <th className="px-5 py-3 font-medium">Base Credits</th>
                <th className="px-5 py-3 font-medium">Metered Used</th>
                <th className="px-5 py-3 font-medium">Status</th>
                <th className="px-5 py-3 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {tenantsLoading ? (
                <tr>
                  <td colSpan={7} className="px-5 py-8 text-center text-[13px] text-muted-foreground">
                    Loading tenants...
                  </td>
                </tr>
              ) : !tenants?.length ? (
                <tr>
                  <td colSpan={7} className="px-5 py-8 text-center text-[13px] text-muted-foreground">
                    No tenants found
                  </td>
                </tr>
              ) : (
                tenants.map((tenant) => (
                  <tr
                    key={tenant.id}
                    className="border-b last:border-0 hover:bg-muted/50 transition-colors"
                  >
                    <td className="px-5 py-3">
                      <div>
                        <p className="text-[13px] font-medium text-foreground">
                          {tenant.name}
                        </p>
                        <button
                          onClick={() => handleCopyId(tenant.id)}
                          className="flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
                        >
                          {tenant.slug}
                          {copiedId === tenant.id ? (
                            <Check className="h-3 w-3 text-emerald-500" />
                          ) : (
                            <Copy className="h-3 w-3" />
                          )}
                        </button>
                      </div>
                    </td>
                    <td className="px-5 py-3">
                      <span
                        className={cn(
                          "inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium",
                          tenant.plan === "free"
                            ? "bg-gray-100 text-gray-600"
                            : tenant.plan === "pro"
                            ? "bg-blue-100 text-blue-600"
                            : "bg-purple-100 text-purple-600",
                        )}
                      >
                        {tenant.plan}
                      </span>
                    </td>
                    <td className="px-5 py-3 text-[13px] text-foreground">
                      {tenant.user_count}
                    </td>
                    <td className="px-5 py-3 text-[13px] text-foreground">
                      {tenant.wallet?.base_credits_remaining?.toLocaleString() ?? "—"}
                    </td>
                    <td className="px-5 py-3 text-[13px] text-foreground">
                      {tenant.wallet?.metered_credits_used?.toLocaleString() ?? "—"}
                    </td>
                    <td className="px-5 py-3">
                      <span
                        className={cn(
                          "inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium",
                          tenant.is_active
                            ? "bg-emerald-100 text-emerald-700"
                            : "bg-red-100 text-red-700",
                        )}
                      >
                        {tenant.is_active ? "Active" : "Inactive"}
                      </span>
                    </td>
                    <td className="px-5 py-3">
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => setFlagTenantId(tenant.id === flagsTenantId ? null : tenant.id)}
                          className={cn(
                            "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[11px] font-medium transition-opacity hover:opacity-80",
                            flagsTenantId === tenant.id
                              ? "bg-primary text-primary-foreground"
                              : "bg-muted text-foreground",
                          )}
                        >
                          <Flag className="h-3 w-3" />
                          Flag
                        </button>
                        <button
                          onClick={() => handleImpersonate(tenant)}
                          disabled={impersonate.isPending}
                          className="inline-flex items-center gap-1.5 rounded-md bg-foreground px-3 py-1.5 text-[11px] font-medium text-background transition-opacity hover:opacity-80 disabled:opacity-50"
                        >
                          <UserCheck className="h-3 w-3" />
                          Impersonate
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Feature Flag Panel */}
      {flagsTenant && (
        <FeatureFlagPanel
          tenantId={flagsTenant.id}
          tenantName={flagsTenant.name}
          onClose={() => setFlagTenantId(null)}
        />
      )}
    </div>
  );
}
