"use client";

import { useState, useCallback, useEffect, Suspense } from "react";
import Link from "next/link";
import { useRouter, usePathname } from "next/navigation";
import { useAuth } from "@/providers/auth-provider";
import { apiClient } from "@/lib/api-client";
import { TransactionsSection } from "@/components/transactions/section";
import { isTransactionPath } from "@/components/transactions/navigation";
import { Sidebar } from "@/components/sidebar";
import { OnboardingWizard } from "@/components/onboarding/onboarding-wizard";
import { AlertTriangle, Plug, X, Menu, ChevronRight } from "lucide-react";
import { NAV_ITEMS } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { ConnectionAlertBanner } from "@/components/connection-alert-banner";

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, isLoading, refreshUser } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  // /reports/<id> (the viewer) is fluid so the report iframe gets real height —
  // the boxed container has no height chain and collapses iframes to ~150px.
  // The /reports LIST page stays boxed ("/reports".startsWith("/reports/") is false).
  const isFluid =
    pathname?.startsWith("/workspace") ||
    pathname?.startsWith("/chat") ||
    pathname?.startsWith("/reports/");
  const routeLabel = isTransactionPath(pathname) ? "Transactions" : (pathname === "/connections" ? "Settings" : NAV_ITEMS.find((item) => pathname === item.href || pathname.startsWith(`${item.href}/`))?.label) || "Workspace";
  const showNetSuiteSetup = pathname.startsWith("/reconciliation") || pathname.startsWith("/transaction-operations");
  const [showOnboarding, setShowOnboarding] = useState(false);
  const [connectionHealth, setConnectionHealth] = useState<
    | { state: "ok" }
    | { state: "missing" }
    | { state: "expired"; reason: string }
  >({ state: "ok" });
  const [bannerDismissed, setBannerDismissed] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  useEffect(() => {
    const compact = window.matchMedia("(max-width: 767px)");
    const update = () => setSidebarCollapsed(compact.matches);
    update();
    compact.addEventListener("change", update);
    return () => compact.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (window.matchMedia("(max-width: 767px)").matches)
      setSidebarCollapsed(true);
  }, [pathname]);

  useEffect(() => {
    if (!user) {
      setShowOnboarding(false);
      return;
    }
    if (user.onboarding_completed_at) {
      setShowOnboarding(false);
      return;
    }
    const skipped = localStorage.getItem("onboarding_skipped");
    if (skipped) {
      setShowOnboarding(false);
      return;
    }
    // Check if both connections already exist — skip onboarding if so
    apiClient
      .get<{ valid: boolean }>(
        "/api/v1/onboarding/checklist/connection/validate",
      )
      .then((result) => {
        if (result.valid) {
          setShowOnboarding(false);
        } else {
          setShowOnboarding(true);
        }
      })
      .catch(() => {
        // If check fails, show onboarding to be safe
        setShowOnboarding(true);
      });
  }, [user]);

  // Check connection status for the warning banner (runs when onboarding is not shown)
  useEffect(() => {
    if (!user || showOnboarding) {
      setConnectionHealth({ state: "ok" });
      return;
    }
    apiClient
      .get<{
        valid: boolean;
        connection_status?: string | null;
        mcp_status?: string | null;
        error_reason?: string | null;
      }>("/api/v1/onboarding/checklist/connection/validate")
      .then((result) => {
        if (result.valid) {
          setConnectionHealth({ state: "ok" });
        } else if (
          result.connection_status === "error" ||
          result.mcp_status === "error"
        ) {
          setConnectionHealth({
            state: "expired",
            reason:
              result.error_reason ||
              "OAuth token expired — re-authorize your NetSuite connection",
          });
        } else {
          setConnectionHealth({ state: "missing" });
        }
      })
      .catch(() => {
        setConnectionHealth({ state: "missing" });
      });
  }, [user, showOnboarding]);

  const handleOnboardingComplete = useCallback(async () => {
    setShowOnboarding(false);
    // Refresh user profile to pick up onboarding_completed_at without a full reload
    await refreshUser();
  }, [refreshUser]);

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
          <span className="text-sm text-muted-foreground">
            {isLoading ? "Loading..." : "Redirecting to login..."}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="orbital-shell flex h-screen overflow-hidden">
      <a href="#main-content" className="sr-only z-50 rounded-md bg-primary p-3 text-primary-foreground focus:not-sr-only focus:fixed focus:left-3 focus:top-3">Skip to content</a>
      {showOnboarding && (
        <OnboardingWizard onComplete={handleOnboardingComplete} />
      )}
      {!sidebarCollapsed && (
        <button
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
          aria-label="Close sidebar"
          onClick={() => setSidebarCollapsed(true)}
        />
      )}
      <div
        className="shrink-0 max-md:absolute max-md:inset-y-0 max-md:left-0 max-md:z-40"
        aria-hidden={sidebarCollapsed}
      >
        <Suspense fallback={null}>
          <Sidebar
            collapsed={sidebarCollapsed}
            onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
          />
        </Suspense>
      </div>
      <main id="main-content" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background">
        <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b bg-card/40 px-4 md:px-8">
          <div className="flex min-w-0 items-center gap-3">
            {sidebarCollapsed && <button onClick={() => setSidebarCollapsed(false)} className="rounded-md border border-input p-2 text-primary hover:border-primary hover:bg-accent" aria-label="Open sidebar" aria-expanded={false}><Menu className="h-4 w-4" /></button>}
            <Link href="/dashboard" className="text-xs text-muted-foreground hover:text-primary">Workspace</Link>
            <ChevronRight aria-hidden="true" className="h-3 w-3 text-muted-foreground" />
            <span className="truncate text-xs text-foreground">{routeLabel}</span>
          </div>
          <span className="hidden truncate text-[11px] text-muted-foreground sm:block">{user.tenant_name}</span>
        </header>
        <ConnectionAlertBanner />
        {/* Connection warning banner — missing */}
        {connectionHealth.state === "missing" && showNetSuiteSetup && !bannerDismissed && (
          <div className="orbital-notice mx-4 mt-4 shrink-0 md:mx-8" role="status">
            <div className="flex items-center gap-3">
              <Plug className="h-4 w-4 shrink-0 text-primary" />
              <p className="text-[13px] text-muted-foreground">
                Connect NetSuite when your work needs NetSuite data.{" "}
                <Link
                  href="/settings#connections"
                  className="font-medium underline underline-offset-2 hover:text-primary"
                >
                  Manage connections
                </Link>
              </p>
            </div>
            <button
              aria-label="Dismiss connection warning"
              onClick={() => setBannerDismissed(true)}
              className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )}

        {/* Connection warning banner — expired */}
        {connectionHealth.state === "expired" && !bannerDismissed && (
          <div className="orbital-notice mx-4 mt-4 shrink-0 md:mx-8" data-tone="error" role="status">
            <div className="flex items-center gap-3">
              <AlertTriangle className="h-4 w-4 shrink-0 text-red-600 dark:text-red-400" />
              <p className="text-[13px] text-foreground">
                {connectionHealth.reason}{" "}
                <Link
                  href="/settings#connections"
                  className="font-medium underline underline-offset-2 hover:text-primary"
                >
                  Reconnect in Settings
                </Link>
              </p>
            </div>
            <button
              aria-label="Dismiss connection warning"
              onClick={() => setBannerDismissed(true)}
              className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )}
        <div
          className={cn(
            "mx-auto w-full min-h-0 flex-1 overflow-auto scrollbar-thin",
            isFluid
              ? "min-w-0 max-w-none"
              : "max-w-[1400px] px-4 py-6 md:px-8 md:py-8",
          )}
        >
          {isTransactionPath(pathname) ? <TransactionsSection>{children}</TransactionsSection> : children}
        </div>
      </main>
    </div>
  );
}
