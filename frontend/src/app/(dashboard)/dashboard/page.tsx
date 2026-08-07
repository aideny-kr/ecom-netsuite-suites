"use client";

import Link from "next/link";
import { useAuth } from "@/providers/auth-provider";
import { useDashboard } from "@/hooks/use-dashboard";
import { DashboardWall, DashboardWallSkeleton } from "./dashboard-wall";
import { DashboardEmptyState } from "./dashboard-empty-state";
import {
  Plug,
  ScrollText,
  MessageSquare,
  Table2,
} from "lucide-react";

const quickLinks = [
  {
    title: "Connections",
    description: "Manage Shopify, Stripe, and NetSuite integrations",
    href: "/connections",
    icon: Plug,
    color: "from-violet-500/10 to-purple-500/10",
    iconColor: "text-violet-600",
  },
  {
    title: "Data Tables",
    description: "Browse synced orders, payments, refunds, and more",
    href: "/tables/orders",
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
    title: "AI Chat",
    description: "Ask questions about your data and operations",
    href: "/chat",
    icon: MessageSquare,
    color: "from-emerald-500/10 to-teal-500/10",
    iconColor: "text-emerald-600",
  },
];

export default function DashboardPage() {
  const { user } = useAuth();
  const { data, isLoading, isError } = useDashboard();
  const firstName = user?.full_name?.split(" ")[0];
  const active = data?.active ?? null;

  return (
    <div className="space-y-8 animate-fade-in">
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
            Couldn&apos;t load your dashboard. Try refreshing the page.
          </p>
        </div>
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
              className="group flex items-center gap-3 rounded-lg border bg-card p-3 shadow-soft transition-colors hover:bg-muted/30"
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
