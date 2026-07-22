"use client";

import Link from "next/link";
import { useAuth } from "@/providers/auth-provider";
import { useReports } from "@/hooks/use-reports";
import { PinnedReportCard } from "./pinned-report-card";
import {
  Plug,
  ScrollText,
  MessageSquare,
  Table2,
  ArrowRight,
} from "lucide-react";

const quickLinks = [
  {
    title: "Connections",
    description: "Manage Shopify, Stripe, and NetSuite integrations",
    href: "/connections",
    icon: Plug,
    color: "from-violet-500/10 to-purple-500/10",
    iconColor: "text-violet-600",
    borderColor: "group-hover:border-violet-200",
  },
  {
    title: "Data Tables",
    description: "Browse synced orders, payments, refunds, and more",
    href: "/tables/orders",
    icon: Table2,
    color: "from-blue-500/10 to-cyan-500/10",
    iconColor: "text-blue-600",
    borderColor: "group-hover:border-blue-200",
  },
  {
    title: "Audit Log",
    description: "Track all actions and events in your account",
    href: "/audit",
    icon: ScrollText,
    color: "from-amber-500/10 to-orange-500/10",
    iconColor: "text-amber-600",
    borderColor: "group-hover:border-amber-200",
  },
  {
    title: "AI Chat",
    description: "Ask questions about your data and operations",
    href: "/chat",
    icon: MessageSquare,
    color: "from-emerald-500/10 to-teal-500/10",
    iconColor: "text-emerald-600",
    borderColor: "group-hover:border-emerald-200",
  },
];

export default function DashboardPage() {
  const { user } = useAuth();
  const { data: reports } = useReports();

  const pinned = (reports ?? [])
    .filter((r) => r.dashboard_pinned_at != null)
    .sort((a, b) => (b.dashboard_pinned_at as string).localeCompare(a.dashboard_pinned_at as string));

  return (
    <div className="space-y-8 animate-fade-in">
      {/* Header */}
      <div>
        <h2 className="text-2xl font-semibold tracking-tight text-foreground">
          Welcome back, {user?.full_name?.split(" ")[0]}
        </h2>
        <p className="mt-1 text-[15px] text-muted-foreground">
          Here&apos;s where your business stands.
        </p>
      </div>

      {/* Pinned reports */}
      <div>
        <h3 className="mb-4 text-[13px] font-semibold uppercase tracking-wider text-muted-foreground">
          Pinned reports
        </h3>
        {pinned.length > 0 ? (
          <div className="space-y-4">
            {pinned.map((report) => (
              <PinnedReportCard key={report.id} report={report} />
            ))}
          </div>
        ) : (
          <div className="rounded-xl border border-dashed p-5 text-[13px] text-muted-foreground">
            No pinned reports yet — open any report and choose Pin to dashboard.
          </div>
        )}
      </div>

      {/* Quick Links Grid */}
      <div>
        <h3 className="mb-4 text-[13px] font-semibold uppercase tracking-wider text-muted-foreground">
          Quick Access
        </h3>
        <div className="grid gap-4 md:grid-cols-2">
          {quickLinks.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`group flex items-start gap-4 rounded-xl border bg-card p-5 shadow-soft transition-all duration-200 hover:shadow-soft-md ${item.borderColor}`}
            >
              <div
                className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br ${item.color}`}
              >
                <item.icon className={`h-5 w-5 ${item.iconColor}`} />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between">
                  <h3 className="text-[15px] font-semibold text-foreground">
                    {item.title}
                  </h3>
                  <ArrowRight className="h-4 w-4 text-muted-foreground opacity-0 transition-all duration-200 group-hover:translate-x-0.5 group-hover:opacity-100" />
                </div>
                <p className="mt-0.5 text-[13px] leading-relaxed text-muted-foreground">
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
