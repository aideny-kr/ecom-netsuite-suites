"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { isTransactionPath } from "@/components/transactions/navigation";
import { useState, useEffect } from "react";
import { useTheme } from "next-themes";
import {
  Orbit,
  LayoutDashboard,
  Plug,
  ScrollText,
  MessageSquare,
  Code,
  Database,
  Settings,
  Table2,
  ChevronsLeft,
  LogOut,
  ChevronsUpDown,
  Check,
  Zap,
  Moon,
  Sun,
  Tag,
  BarChart3,
  Scale,
  Network,
  FileBarChart,
  Sparkles,
  CalendarClock,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { NAV_ITEMS } from "@/lib/constants";
import { useAuth } from "@/providers/auth-provider";
import { useBranding } from "@/providers/branding-provider";
import { useFeatures } from "@/hooks/use-features";
import { usePermissions } from "@/hooks/use-permissions";
import { useAgents } from "@/hooks/use-agents";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

const iconMap = {
  Table2,
  Orbit,
  LayoutDashboard,
  Plug,
  ScrollText,
  MessageSquare,
  Code,
  Database,
  Settings,
  Scale,
  Network,
  FileBarChart,
  Sparkles,
  CalendarClock,
} as const;

const agentIconMap: Record<string, typeof Tag> = {
  "pricing-agent": Tag,
  "bi-agent": BarChart3,
  "recon-agent": Scale,
};

export function Sidebar({ collapsed = false, onToggle }: { collapsed?: boolean; onToggle?: () => void }) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const pinnedAgentId = searchParams?.get("agent") || null;
  const { data: agents = [] } = useAgents();
  const specialistAgents = agents.filter(a => a.agent_id !== "unified-agent");

  const handleSelectAgent = (agentId: string) => {
    if (pinnedAgentId === agentId) {
      router.push("/chat");
    } else {
      router.push(`/chat?agent=${agentId}`);
    }
  };
  const { user, tenants, switchTenant, logout } = useAuth();
  const { brandName, logoUrl } = useBranding();
  const { data: features } = useFeatures();
  const { hasPermission } = usePermissions();
  const { setTheme, resolvedTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  const utilityPaths = new Set(["/queries", "/audit"]);
  const visibleItems = NAV_ITEMS.filter((item) => {
    return !item.featureFlag || features?.[item.featureFlag] !== false;
  });

  return (
    <aside aria-label="Workspace sidebar" style={{ display: collapsed ? "none" : undefined }} className={cn(
      "flex h-full shrink-0 flex-col bg-[hsl(var(--sidebar-bg))] text-[hsl(var(--sidebar-foreground))] transition-[width] duration-200 overflow-hidden",
      collapsed ? "w-0" : "w-[218px]"
    )}>
      {/* Brand */}
      <div className="border-b border-[hsl(var(--sidebar-border))] px-5 py-5">
        <div className="flex items-center gap-2.5">
          {logoUrl ? (
            <img src={logoUrl} alt={brandName} className="h-8 w-8 rounded-lg object-contain" />
          ) : (
            <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-[hsl(var(--sidebar-border))] bg-transparent">
              <Orbit className="h-5 w-5 text-[hsl(var(--sidebar-active))]" />
            </div>
          )}
          <div>
            <p className="text-[15px] font-semibold tracking-tight text-white">
              {brandName}
            </p>
          </div>
          {onToggle && (
            <button
              onClick={onToggle}
              className="ml-auto shrink-0 rounded-md p-1 text-[hsl(var(--sidebar-muted))] transition-colors hover:bg-[hsl(var(--sidebar-hover))] hover:text-[hsl(var(--sidebar-active))]"
              aria-label="Collapse sidebar"
            >
              <ChevronsLeft className="h-4 w-4" />
            </button>
          )}
        </div>
        {user && tenants.length > 1 ? (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="mt-3 flex w-full items-center justify-between gap-1 rounded-md px-2 py-1.5 text-xs transition-colors hover:bg-[hsl(var(--sidebar-hover))]">
                <span className="truncate">{user.tenant_name}</span>
                <ChevronsUpDown className="h-3 w-3 shrink-0 opacity-50" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-56">
              {tenants.map((t) => (
                <DropdownMenuItem
                  key={t.id}
                  onClick={() => {
                    if (t.id !== user.tenant_id) {
                      switchTenant(t.id);
                    }
                  }}
                  className="flex items-center justify-between"
                >
                  <span className="truncate">{t.name}</span>
                  {t.id === user.tenant_id && (
                    <Check className="h-4 w-4 shrink-0" />
                  )}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        ) : user ? (
          <p className="mt-2 truncate px-2 text-xs text-[hsl(var(--sidebar-muted))]">
            {user.tenant_name}
          </p>
        ) : null}
      </div>

      {/* Navigation */}
      <nav aria-label="Main navigation" className="flex-1 space-y-0.5 overflow-auto px-3 py-4 scrollbar-thin">
        <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-widest text-[hsl(var(--sidebar-muted))]">
          Workspace
        </p>
        {visibleItems.filter(item => !utilityPaths.has(item.href) && item.href !== "/settings").map((item) => {
          const Icon = iconMap[item.icon];
          const isActive = item.href === "/transactions" ? isTransactionPath(pathname) : pathname === item.href || pathname.startsWith(`${item.href}/`);
          return (
            <Link
              key={item.href}
              href={item.href}
              onClick={() => {
                if (!collapsed && window.matchMedia("(max-width: 767px)").matches) onToggle?.();
              }}
              aria-current={isActive ? "page" : undefined}
              className={cn(
                "group flex items-center gap-3 rounded-lg border-l-2 border-transparent px-3 py-2.5 text-[13px] font-medium transition-colors duration-150",
                isActive
                  ? "bg-[hsl(var(--sidebar-hover))] text-[hsl(var(--sidebar-active))] border-l-2 border-[hsl(var(--sidebar-active))]"
                  : "text-[hsl(var(--sidebar-foreground))] hover:bg-[hsl(var(--sidebar-hover))] hover:text-[hsl(var(--sidebar-active))]",
              )}
            >
              <Icon className={cn("h-4 w-4", isActive ? "text-[hsl(var(--sidebar-active))]" : "text-[hsl(var(--sidebar-muted))] group-hover:text-[hsl(var(--sidebar-active))]")} />
              {item.label}
            </Link>
          );
        })}

        {/* Custom Agents */}
        {specialistAgents.length > 0 && (
          <div className="pt-4">
            <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-widest text-[hsl(var(--sidebar-muted))]">
              Custom Agents
            </p>
            {specialistAgents.map((agent) => {
              const AgentIcon = agentIconMap[agent.agent_id] || Tag;
              const isActive = pathname === "/chat" && pinnedAgentId === agent.agent_id;
              return (
                <button
                  key={agent.agent_id}
                  onClick={() => handleSelectAgent(agent.agent_id)}
                  className={cn(
                    "group flex w-full items-center gap-3 px-4 py-2.5 text-[13px] font-medium transition-colors duration-150",
                    isActive
                      ? "bg-[hsl(var(--sidebar-hover))] text-[hsl(var(--sidebar-active))] border-l-2 border-[hsl(var(--sidebar-active))]"
                      : "text-[hsl(var(--sidebar-foreground))] hover:bg-[hsl(var(--sidebar-hover))] hover:text-[hsl(var(--sidebar-active))]",
                  )}
                >
                  <AgentIcon className={cn("h-4 w-4 shrink-0", isActive ? "text-[hsl(var(--sidebar-active))]" : "text-[hsl(var(--sidebar-muted))] group-hover:text-[hsl(var(--sidebar-active))]")} />
                  <span className="truncate">{agent.display_name}</span>
                </button>
              );
            })}
          </div>
        )}

        <details className="mt-5 border-t border-[hsl(var(--sidebar-border))] pt-3" open={utilityPaths.has(pathname)}>
          <summary className="cursor-pointer rounded-md px-3 py-2 text-xs text-[hsl(var(--sidebar-foreground))] hover:bg-[hsl(var(--sidebar-hover))]">Utilities</summary>
          <div className="my-2 space-y-1">{visibleItems.filter(item => utilityPaths.has(item.href)).map(item => {
            const Icon = iconMap[item.icon];
            return <Link key={item.href} href={item.href} aria-current={pathname === item.href ? "page" : undefined} className="flex items-center gap-3 rounded-md px-4 py-2 text-xs hover:bg-[hsl(var(--sidebar-hover))] hover:text-[hsl(var(--sidebar-active))]"><Icon className="h-4 w-4" />{item.label}</Link>;
          })}</div>
        </details>
      </nav>

      <Link href="/settings" aria-current={pathname === "/settings" || pathname === "/connections" ? "page" : undefined} className="mx-3 mb-3 flex items-center gap-3 rounded-md px-3 py-2.5 text-[13px] hover:bg-[hsl(var(--sidebar-hover))] hover:text-[hsl(var(--sidebar-active))]"><Settings className="h-4 w-4" />Settings</Link>
      {/* User / Sign Out */}
      <div className="border-t border-[hsl(var(--sidebar-border))] px-3 py-3">
        {user && (
          <div className="mb-2 px-3">
            <p className="truncate text-[13px] font-medium text-white">
              {user.full_name}
            </p>
            <p className="truncate text-[11px] text-[hsl(var(--sidebar-muted))]">
              {user.email}
            </p>
          </div>
        )}
        <button
          onClick={logout}
          className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-[13px] font-medium text-[hsl(var(--sidebar-foreground))] transition-all duration-150 hover:bg-[hsl(var(--sidebar-hover))] hover:text-white"
        >
          <LogOut className="h-4 w-4 text-[hsl(var(--sidebar-muted))]" />
          Sign Out
        </button>

        {mounted && (
          <button
            onClick={() => setTheme(resolvedTheme === 'dark' ? 'light' : 'dark')}
            className="mt-1 flex w-full items-center gap-3 rounded-lg px-3 py-2 text-[13px] font-medium text-[hsl(var(--sidebar-foreground))] transition-all duration-150 hover:bg-[hsl(var(--sidebar-hover))] hover:text-white"
          >
            {resolvedTheme === 'dark' ? (
              <>
                <Sun className="h-4 w-4 text-[hsl(var(--sidebar-muted))]" />
                Light Mode
              </>
            ) : (
              <>
                <Moon className="h-4 w-4 text-[hsl(var(--sidebar-muted))]" />
                Dark Mode
              </>
            )}
          </button>
        )}
      </div>
    </aside>
  );
}
