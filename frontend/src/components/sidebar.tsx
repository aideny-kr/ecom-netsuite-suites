"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import {
  LayoutDashboard,
  Plug,
  ScrollText,
  MessageSquare,
  Code,
  Settings,
  Table2,
  ChevronDown,
  LogOut,
  ChevronsUpDown,
  Check,
  Zap,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { NAV_ITEMS, CANONICAL_TABLES } from "@/lib/constants";
import { useAuth } from "@/providers/auth-provider";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

const iconMap = {
  LayoutDashboard,
  Plug,
  ScrollText,
  MessageSquare,
  Code,
  Settings,
} as const;

export function Sidebar() {
  const pathname = usePathname();
  const { user, tenants, switchTenant, logout } = useAuth();
  const [tablesExpanded, setTablesExpanded] = useState(
    pathname.startsWith("/tables"),
  );

  return (
    <aside className="flex h-full w-[260px] flex-col bg-[hsl(var(--sidebar-bg))] text-[hsl(var(--sidebar-foreground))]">
      {/* Brand */}
      <div className="border-b border-[hsl(var(--sidebar-border))] px-5 py-5">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[hsl(var(--sidebar-active))]">
            <Zap className="h-4 w-4 text-white" />
          </div>
          <div>
            <h1 className="text-[15px] font-semibold tracking-tight text-white">
              Suite Studio AI
            </h1>
          </div>
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
      <nav className="flex-1 space-y-0.5 overflow-auto px-3 py-4 scrollbar-thin">
        <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-widest text-[hsl(var(--sidebar-muted))]">
          Menu
        </p>
        {NAV_ITEMS.map((item) => {
          const Icon = iconMap[item.icon];
          const isActive = pathname === item.href;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "group flex items-center gap-3 rounded-lg px-3 py-2 text-[13px] font-medium transition-all duration-150",
                isActive
                  ? "bg-[hsl(var(--sidebar-active))] text-white shadow-sm"
                  : "text-[hsl(var(--sidebar-foreground))] hover:bg-[hsl(var(--sidebar-hover))] hover:text-white",
              )}
            >
              <Icon className={cn("h-4 w-4", isActive ? "text-white" : "text-[hsl(var(--sidebar-muted))] group-hover:text-white")} />
              {item.label}
            </Link>
          );
        })}

        <div className="pt-4">
          <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-widest text-[hsl(var(--sidebar-muted))]">
            Data
          </p>
          <button
            onClick={() => setTablesExpanded(!tablesExpanded)}
            className={cn(
              "group flex w-full items-center gap-3 rounded-lg px-3 py-2 text-[13px] font-medium transition-all duration-150",
              pathname.startsWith("/tables")
                ? "bg-[hsl(var(--sidebar-hover))] text-white"
                : "text-[hsl(var(--sidebar-foreground))] hover:bg-[hsl(var(--sidebar-hover))] hover:text-white",
            )}
          >
            <Table2 className={cn("h-4 w-4", pathname.startsWith("/tables") ? "text-white" : "text-[hsl(var(--sidebar-muted))] group-hover:text-white")} />
            Tables
            <ChevronDown
              className={cn(
                "ml-auto h-3.5 w-3.5 text-[hsl(var(--sidebar-muted))] transition-transform duration-200",
                !tablesExpanded && "-rotate-90",
              )}
            />
          </button>
          <div
            className={cn(
              "overflow-hidden transition-all duration-200",
              tablesExpanded ? "max-h-[500px] opacity-100" : "max-h-0 opacity-0",
            )}
          >
            <div className="ml-5 space-y-0.5 border-l border-[hsl(var(--sidebar-border))] py-1 pl-3">
              {CANONICAL_TABLES.map((table) => {
                const href = `/tables/${table.name}`;
                const isActive = pathname === href;
                return (
                  <Link
                    key={table.name}
                    href={href}
                    className={cn(
                      "block rounded-md px-3 py-1.5 text-[13px] transition-all duration-150",
                      isActive
                        ? "bg-[hsl(var(--sidebar-active))/0.15] font-medium text-[hsl(var(--sidebar-active))]"
                        : "text-[hsl(var(--sidebar-foreground))] hover:bg-[hsl(var(--sidebar-hover))] hover:text-white",
                    )}
                  >
                    {table.label}
                  </Link>
                );
              })}
            </div>
          </div>
        </div>
      </nav>

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
      </div>
    </aside>
  );
}
