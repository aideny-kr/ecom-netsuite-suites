"use client";

import { useState, useEffect } from "react";
import { apiClient } from "@/lib/api-client";
import type {
  OnboardingAuditTrailResponse,
  OnboardingChecklist,
  OnboardingChecklistItem,
} from "@/lib/types";
import { CheckCircle2, Circle, SkipForward, ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import Link from "next/link";

const STEP_LABELS: Record<string, string> = {
  profile: "Business Profile",
  connection: "NetSuite Connection",
  policy: "Policy Setup",
  workspace: "Workspace Setup",
  first_success: "First Success",
};

const STEP_DESCRIPTIONS: Record<string, string> = {
  profile: "Set up your industry, business description, and team size",
  connection: "Connect your NetSuite account via OAuth",
  policy: "Configure data access policies and security settings",
  workspace: "Create your first workspace for SuiteScript files",
  first_success: "Complete your first script validation cycle",
};

function StatusIcon({ status }: { status: string }) {
  if (status === "completed")
    return <CheckCircle2 className="h-5 w-5 text-green-500" />;
  if (status === "skipped")
    return <SkipForward className="h-5 w-5 text-amber-500" />;
  return <Circle className="h-5 w-5 text-muted-foreground/30" />;
}

export default function OnboardingPage() {
  const [checklist, setChecklist] = useState<OnboardingChecklist | null>(null);
  const [auditEvents, setAuditEvents] = useState<
    OnboardingAuditTrailResponse["events"]
  >([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const [checklistData, auditData] = await Promise.all([
          apiClient.get<OnboardingChecklist>("/api/v1/onboarding/checklist"),
          apiClient.get<OnboardingAuditTrailResponse>(
            "/api/v1/onboarding/audit-trail",
          ),
        ]);
        setChecklist(checklistData);
        setAuditEvents(auditData.events.slice(0, 8));
      } catch (err) {
        console.error("Failed to load onboarding data:", err);
      } finally {
        setIsLoading(false);
      }
    };
    fetchData();
  }, []);

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Onboarding</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {checklist?.finalized_at
              ? "Onboarding completed"
              : "Complete the steps below to set up your account"}
          </p>
        </div>
        {checklist && !checklist.all_completed && (
          <Button
            size="sm"
            className="gap-1"
            onClick={() => {
              localStorage.removeItem("onboarding_skipped");
              window.location.href = "/dashboard";
            }}
          >
            Resume Onboarding
            <ArrowRight className="h-4 w-4" />
          </Button>
        )}
      </div>

      <div className="space-y-3">
        {checklist?.items.map((item: OnboardingChecklistItem) => (
          <div
            key={item.step_key}
            className="flex items-center gap-4 rounded-lg border p-4 transition-colors hover:bg-muted/30"
          >
            <StatusIcon status={item.status} />
            <div className="flex-1">
              <h3 className="text-sm font-medium">
                {STEP_LABELS[item.step_key] || item.step_key}
              </h3>
              <p className="text-xs text-muted-foreground mt-0.5">
                {STEP_DESCRIPTIONS[item.step_key]}
              </p>
            </div>
            <div className="text-xs text-muted-foreground">
              {item.status === "completed" && item.completed_at && (
                <span>
                  Completed {new Date(item.completed_at).toLocaleDateString()}
                </span>
              )}
              {item.status === "skipped" && (
                <span className="text-amber-500">Skipped</span>
              )}
              {item.status === "pending" && <span>Pending</span>}
            </div>
          </div>
        ))}
      </div>

      {checklist?.finalized_at && (
        <div className="mt-6 rounded-lg border border-green-500/20 bg-green-500/5 p-4 text-center">
          <CheckCircle2 className="h-8 w-8 text-green-500 mx-auto mb-2" />
          <p className="text-sm font-medium">Onboarding Finalized</p>
          <p className="text-xs text-muted-foreground mt-1">
            Completed on {new Date(checklist.finalized_at).toLocaleDateString()}
          </p>
        </div>
      )}

      <div className="mt-8 rounded-lg border p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold">Onboarding Audit Trail</h2>
          <Link href="/audit" className="text-xs text-primary hover:underline">
            Open Audit Log
          </Link>
        </div>
        {auditEvents.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No onboarding audit events yet.
          </p>
        ) : (
          <div className="space-y-2">
            {auditEvents.map((event) => (
              <div
                key={event.id}
                className="rounded-md border bg-muted/20 px-3 py-2 text-xs"
              >
                <p className="font-medium">{event.action}</p>
                <p className="text-muted-foreground">
                  {new Date(event.created_at).toLocaleString()}
                  {event.correlation_id
                    ? ` • correlation_id: ${event.correlation_id}`
                    : ""}
                </p>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
