"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import {
  LayoutDashboard,
  Plug,
  ScrollText,
  Table2,
  ChevronDown,
  ChevronRight,
  LogOut,
  ChevronsUpDown,
  Check,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { NAV_ITEMS, CANONICAL_TABLES } from "@/lib/constants";
import { useAuth } from "@/providers/auth-provider";
import { Button } from "@/components/ui/button";
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
} as const;

export function Sidebar() {
  const pathname = usePathname();
  const { user, tenants, switchTenant, logout } = useAuth();
  const [tablesExpanded, setTablesExpanded] = useState(
    pathname.startsWith("/tables"),
  );

  return (
    <aside className="flex h-full w-64 flex-col border-r bg-background">
      <div className="border-b p-4">
        <h1 className="text-lg font-semibold">Ecom NetSuite</h1>
        {user && tenants.length > 1 ? (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="flex w-full items-center justify-between gap-1 rounded-md px-1 py-0.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground">
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
          <p className="truncate text-sm text-muted-foreground">
            {user.tenant_name}
          </p>
        ) : null}
      </div>

      <nav className="flex-1 space-y-1 p-2">
        {NAV_ITEMS.map((item) => {
          const Icon = iconMap[item.icon];
          const isActive = pathname === item.href;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors hover:bg-accent",
                isActive && "bg-accent text-accent-foreground",
              )}
            >
              <Icon className="h-4 w-4" />
              {item.label}
            </Link>
          );
        })}

        <div>
          <button
            onClick={() => setTablesExpanded(!tablesExpanded)}
            className={cn(
              "flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors hover:bg-accent",
              pathname.startsWith("/tables") && "bg-accent text-accent-foreground",
            )}
          >
            <Table2 className="h-4 w-4" />
            Tables
            {tablesExpanded ? (
              <ChevronDown className="ml-auto h-4 w-4" />
            ) : (
              <ChevronRight className="ml-auto h-4 w-4" />
            )}
          </button>
          {tablesExpanded && (
            <div className="ml-4 space-y-1 border-l pl-3 pt-1">
              {CANONICAL_TABLES.map((table) => {
                const href = `/tables/${table.name}`;
                const isActive = pathname === href;
                return (
                  <Link
                    key={table.name}
                    href={href}
                    className={cn(
                      "block rounded-md px-3 py-1.5 text-sm transition-colors hover:bg-accent",
                      isActive && "bg-accent font-medium text-accent-foreground",
                    )}
                  >
                    {table.label}
                  </Link>
                );
              })}
            </div>
          )}
        </div>
      </nav>

      <div className="border-t p-2">
        <Button
          variant="ghost"
          className="w-full justify-start gap-3"
          onClick={logout}
        >
          <LogOut className="h-4 w-4" />
          Sign Out
        </Button>
      </div>
    </aside>
  );
}
