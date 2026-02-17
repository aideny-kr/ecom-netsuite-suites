"use client";

import Link from "next/link";
import { useAuth } from "@/providers/auth-provider";
import {
  Plug,
  ScrollText,
  MessageSquare,
  Table2,
  ArrowRight,
  TrendingUp,
  Activity,
  Database,
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

const stats = [
  { label: "Integrations", icon: Activity, value: "--" },
  { label: "Data Synced", icon: Database, value: "--" },
  { label: "This Month", icon: TrendingUp, value: "--" },
];

export default function DashboardPage() {
  const { user } = useAuth();

  return (
    <div className="space-y-8 animate-fade-in">
      {/* Header */}
      <div>
        <h2 className="text-2xl font-semibold tracking-tight text-foreground">
          Welcome back, {user?.full_name?.split(" ")[0]}
        </h2>
        <p className="mt-1 text-[15px] text-muted-foreground">
          Here&apos;s an overview of your integration platform.
        </p>
      </div>

      {/* Stats Row */}
      <div className="grid grid-cols-3 gap-4">
        {stats.map((stat) => (
          <div
            key={stat.label}
            className="flex items-center gap-4 rounded-xl border bg-card p-5 shadow-soft"
          >
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
              <stat.icon className="h-5 w-5 text-primary" />
            </div>
            <div>
              <p className="text-2xl font-semibold tabular-nums">{stat.value}</p>
              <p className="text-[13px] text-muted-foreground">{stat.label}</p>
            </div>
          </div>
        ))}
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
