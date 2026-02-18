"use client";

import { useState, useCallback, useEffect } from "react";
import { useAuth } from "@/providers/auth-provider";
import { apiClient } from "@/lib/api-client";
import { Sidebar } from "@/components/sidebar";
import { OnboardingWizard } from "@/components/onboarding/onboarding-wizard";

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, isLoading } = useAuth();
  const [showOnboarding, setShowOnboarding] = useState(false);

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

  const handleOnboardingComplete = useCallback(() => {
    setShowOnboarding(false);
    // Reload user profile to get updated onboarding_completed_at
    window.location.reload();
  }, []);

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
    return null;
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {showOnboarding && (
        <OnboardingWizard onComplete={handleOnboardingComplete} />
      )}
      <Sidebar />
      <main className="flex-1 overflow-auto bg-[hsl(240_5%_97.5%)] scrollbar-thin">
        <div className="mx-auto max-w-[1400px] px-8 py-8">
          {children}
        </div>
      </main>
    </div>
  );
}
