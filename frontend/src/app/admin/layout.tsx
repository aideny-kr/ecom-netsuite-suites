"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/providers/auth-provider";
import {
  LayoutDashboard,
  Building2,
  Zap,
  ArrowLeft,
  LogOut,
} from "lucide-react";
import { cn } from "@/lib/utils";

const ADMIN_NAV = [
  { label: "Overview", href: "/admin/dashboard", icon: LayoutDashboard },
];

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, isLoading, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isLoading && !user) {
      router.push("/login");
    }
  }, [isLoading, user, router]);

  if (isLoading || !user) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          <span className="text-sm text-muted-foreground">Loading...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Admin Sidebar */}
      <aside className="flex h-full w-[260px] flex-col bg-[hsl(var(--sidebar-bg))] text-[hsl(var(--sidebar-foreground))]">
        {/* Brand */}
        <div className="border-b border-[hsl(var(--sidebar-border))] px-5 py-5">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-red-600">
              <Zap className="h-4 w-4 text-white" />
            </div>
            <div>
              <h1 className="text-[15px] font-semibold tracking-tight text-white">
                Admin Panel
              </h1>
              <p className="text-[11px] text-[hsl(var(--sidebar-muted))]">
                Super Admin
              </p>
            </div>
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 space-y-0.5 overflow-auto px-3 py-4">
          <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-widest text-[hsl(var(--sidebar-muted))]">
            Platform
          </p>
          {ADMIN_NAV.map((item) => {
            const Icon = item.icon;
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
                <Icon
                  className={cn(
                    "h-4 w-4",
                    isActive
                      ? "text-white"
                      : "text-[hsl(var(--sidebar-muted))] group-hover:text-white",
                  )}
                />
                {item.label}
              </Link>
            );
          })}

          <div className="pt-6">
            <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-widest text-[hsl(var(--sidebar-muted))]">
              Quick Links
            </p>
            <Link
              href="/dashboard"
              className="group flex items-center gap-3 rounded-lg px-3 py-2 text-[13px] font-medium text-[hsl(var(--sidebar-foreground))] transition-all duration-150 hover:bg-[hsl(var(--sidebar-hover))] hover:text-white"
            >
              <ArrowLeft className="h-4 w-4 text-[hsl(var(--sidebar-muted))] group-hover:text-white" />
              Back to App
            </Link>
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

      <main className="flex-1 overflow-auto bg-[hsl(240_5%_97.5%)] scrollbar-thin">
        <div className="mx-auto max-w-[1400px] px-8 py-8">{children}</div>
      </main>
    </div>
  );
}
