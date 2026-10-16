"use client";

import Link from "next/link";
import { useState } from "react";
import { CommandLaunch } from "@/components/orbital/command-launch";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/providers/auth-provider";
import { useDashboard } from "@/hooks/use-dashboard";
import { DashboardWall, DashboardWallSkeleton } from "./dashboard-wall";
import { DashboardEmptyState } from "./dashboard-empty-state";
import { DashboardTrackingEmptyState } from "./dashboard-tracking-empty-state";
import {
  Plug,
  ScrollText,
  MessageSquare,
  Table2,
  Orbit,
} from "lucide-react";

const quickLinks = [
  {
    title: "Connections",
    description: "Manage Shopify, Stripe, and NetSuite integrations",
    href: "/settings#connections",
    icon: Plug,
    color: "from-violet-500/10 to-purple-500/10",
    iconColor: "text-violet-600",
  },
  {
    title: "Transactions",
    description: "Browse synced orders, payments, refunds, and more",
    href: "/transactions",
    icon: Table2,
    color: "from-blue-500/10 to-cyan-500/10",
    iconColor: "text-blue-600",
  },
  {
    title: "Audit Log",
    description: "Track all actions and events in your account",
    href: "/audit",
    icon: ScrollText,
    color: "from-amber-500/10 to-orange-500/10",
    iconColor: "text-amber-600",
  },
  {
    title: "Chat",
    description: "Ask questions about your data and operations",
    href: "/chat",
    icon: MessageSquare,
    color: "from-emerald-500/10 to-teal-500/10",
    iconColor: "text-emerald-600",
  },
];

export default function DashboardPage() {
  const [launchOpen, setLaunchOpen] = useState(false);
  const { user } = useAuth();
  const { data, isLoading, isError } = useDashboard();
  const firstName = user?.full_name?.split(" ")[0];
  const active = data?.active ?? null;

  return (
    <div className="space-y-8 animate-fade-in">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div>
        <p className="orbital-eyebrow">{user?.tenant_name || "Your workspace"}</p>
        <h1 className="mt-2 text-2xl font-medium">Command Center</h1>
        <p className="mt-2 text-[13px] text-muted-foreground">Your business at a glance. Review reports and choose your next action.</p>
        </div>
        {(active || data?.active_tracking) && <Button variant="outline" size="sm" aria-expanded={launchOpen} aria-controls={launchOpen ? "command-launchpad" : undefined} onClick={() => setLaunchOpen(open => !open)}><Orbit className="mr-2 h-4 w-4" />{launchOpen ? "Close launchpad" : "Open launchpad"}</Button>}
      </header>
      {(active || data?.active_tracking) && launchOpen && <div id="command-launchpad"><CommandLaunch /></div>}
      {isLoading ? (
        <DashboardWallSkeleton />
      ) : active ? (
        <DashboardWall
          report={active}
          published={data?.published ?? []}
          activeIsFallback={data?.active_is_fallback}
          // Rolling-period Stage 1 (Task 5): threaded straight through from
          // useDashboard() — both optional/defaulted on DashboardWall, so this stays
          // a no-op for a tenant with no tracking series yet.
          publishedSeries={data?.published_series}
          activeTracking={data?.active_tracking}
          subtitle={<p className="text-[13px] text-muted-foreground">Welcome back, {firstName}</p>}
        />
      ) : isError ? (
        // The dashboard query itself failed — distinct from "legitimately nothing
        // published": don't invite the user to "browse reports" when we don't
        // actually know the published state.
        <div>
          <h2 className="text-2xl font-semibold tracking-tight text-foreground">
            Welcome back, {firstName}
          </h2>
          <p className="mt-1 text-[15px] text-muted-foreground">
            Here&apos;s where your business stands.
          </p>
          <p className="mt-4 text-[13px] text-muted-foreground">
            Couldn&apos;t load Command Center. Try refreshing the page.
          </p>
        </div>
      ) : data?.active_tracking ? (
        // Round-2 T2-gate MAJOR A: a tracking series was selected but hasn't composed
        // its first report yet (mode="tracking" get-or-creates the series row up front
        // — see DashboardSwitcher's "Tracking the close" group, which deliberately lets
        // you pick such a series). Distinct from "nothing published at all"
        // (DashboardEmptyState, below) — and crucially still shows the switcher, so
        // picking this series is never a dead end.
        <DashboardTrackingEmptyState
          tracking={data.active_tracking}
          published={data?.published ?? []}
          publishedSeries={data?.published_series ?? []}
        />
      ) : (
        <DashboardEmptyState />
      )}

      {/* Quick Access — slim row beneath the wall, not a bulletin board of its own. */}
      <div>
        <h3 className="mb-3 text-[13px] font-semibold uppercase tracking-wider text-muted-foreground">
          Quick Access
        </h3>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {quickLinks.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="orbital-entry group flex items-center gap-3 rounded-lg border bg-card p-3"
            >
              <div
                className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br ${item.color}`}
              >
                <item.icon className={`h-4 w-4 ${item.iconColor}`} />
              </div>
              <div className="min-w-0">
                <p className="truncate text-[13px] font-semibold text-foreground">
                  {item.title}
                </p>
                <p className="truncate text-[11px] text-muted-foreground">
                  {item.description}
                </p>
              </div>
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}
