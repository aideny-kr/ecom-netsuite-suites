"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { apiClient } from "@/lib/api-client";
import { CheckCircle2, Rocket, Loader2 } from "lucide-react";

interface StepFirstSuccessProps {
  onStepComplete: () => void;
}

export function StepFirstSuccess({ onStepComplete }: StepFirstSuccessProps) {
  const [hasPassed, setHasPassed] = useState(false);
  const [isChecking, setIsChecking] = useState(true);
  const [reason, setReason] = useState<string | null>(null);

  useEffect(() => {
    checkRun();
  }, []);

  const checkRun = async () => {
    setIsChecking(true);
    try {
      const result = await apiClient.get<{
        step_key: string;
        valid: boolean;
        reason?: string;
      }>("/api/v1/onboarding/checklist/first_success/validate");
      setHasPassed(result.valid);
      setReason(result.reason || null);
    } catch {
      setHasPassed(false);
      setReason("Unable to validate run status");
    } finally {
      setIsChecking(false);
    }
  };

  const handleComplete = async () => {
    try {
      await apiClient.post(
        "/api/v1/onboarding/checklist/first_success/complete",
      );
      onStepComplete();
    } catch (err: unknown) {
      console.error("Failed to complete step:", err);
    }
  };

  if (isChecking) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-6 p-6">
      <div className="rounded-lg border bg-muted/30 p-6 text-center">
        {hasPassed ? (
          <>
            <CheckCircle2 className="h-12 w-12 text-green-500 mx-auto mb-3" />
            <h3 className="font-medium">First Success Achieved!</h3>
            <p className="text-sm text-muted-foreground mt-1">
              You have passing validate and unit-test runs. Great job!
            </p>
            <Button onClick={handleComplete} className="mt-4">
              Complete Onboarding
            </Button>
          </>
        ) : (
          <>
            <Rocket className="h-12 w-12 text-muted-foreground/50 mx-auto mb-3" />
            <h3 className="font-medium">Your First Success</h3>
            <p className="text-sm text-muted-foreground mt-1 max-w-md mx-auto">
              Use the AI assistant to walk through your first script loop:
              propose a patch, create and approve a changeset, then run both
              validate and unit tests.
            </p>
            {reason && <p className="text-xs text-amber-600 mt-2">{reason}</p>}
            <Button variant="outline" onClick={checkRun} className="mt-4">
              Check Status
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
