"use client";

import { useState, useCallback, useEffect } from "react";
import Link from "next/link";
import { useAuth } from "@/providers/auth-provider";
import { apiClient } from "@/lib/api-client";
import { Sidebar } from "@/components/sidebar";
import { OnboardingWizard } from "@/components/onboarding/onboarding-wizard";
import { AlertTriangle, X } from "lucide-react";

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, isLoading, refreshUser } = useAuth();
  const [showOnboarding, setShowOnboarding] = useState(false);
  const [connectionMissing, setConnectionMissing] = useState(false);
  const [bannerDismissed, setBannerDismissed] = useState(false);

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
      .get<{ valid: boolean }>("/api/v1/onboarding/checklist/connection/validate")
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
      setConnectionMissing(false);
      return;
    }
    apiClient
      .get<{ valid: boolean }>("/api/v1/onboarding/checklist/connection/validate")
      .then((result) => {
        setConnectionMissing(!result.valid);
      })
      .catch(() => {
        setConnectionMissing(true);
      });
  }, [user, showOnboarding]);

  const handleOnboardingComplete = useCallback(async () => {
    setShowOnboarding(false);
    // Refresh user profile to pick up onboarding_completed_at without a full reload
    await refreshUser();
  }, [refreshUser]);

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          <span className="text-sm text-muted-foreground">Loading...</span>
        </div>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-3">
          <span className="text-sm text-muted-foreground">Redirecting to login...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {showOnboarding && (
        <OnboardingWizard onComplete={handleOnboardingComplete} />
      )}
      <Sidebar />
      <main className="flex-1 overflow-auto bg-[hsl(240_5%_97.5%)] scrollbar-thin">
        {/* Connection warning banner */}
        {connectionMissing && !bannerDismissed && (
          <div className="mx-8 mt-6 flex items-center justify-between gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 dark:border-amber-800 dark:bg-amber-950/50">
            <div className="flex items-center gap-3">
              <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
              <p className="text-sm text-amber-800 dark:text-amber-200">
                NetSuite is not connected.{" "}
                <Link
                  href="/settings"
                  className="font-medium underline underline-offset-2 hover:text-amber-900 dark:hover:text-amber-100"
                >
                  Go to Settings
                </Link>{" "}
                to set up your MCP and OAuth connections.
              </p>
            </div>
            <button
              onClick={() => setBannerDismissed(true)}
              className="shrink-0 rounded p-1 text-amber-600 hover:bg-amber-100 dark:text-amber-400 dark:hover:bg-amber-900"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )}
        <div className="mx-auto max-w-[1400px] px-8 py-8">
          {children}
        </div>
      </main>
    </div>
  );
}
