"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { apiClient } from "@/lib/api-client";
import { CheckCircle2, FolderOpen, Loader2 } from "lucide-react";

interface StepWorkspaceProps {
  onStepComplete: () => void;
}

export function StepWorkspace({ onStepComplete }: StepWorkspaceProps) {
  const [hasWorkspace, setHasWorkspace] = useState(false);
  const [isChecking, setIsChecking] = useState(true);

  useEffect(() => {
    checkWorkspace();
  }, []);

  const checkWorkspace = async () => {
    setIsChecking(true);
    try {
      const result = await apiClient.get<{ step_key: string; valid: boolean }>(
        "/api/v1/onboarding/checklist/workspace/validate"
      );
      setHasWorkspace(result.valid);
    } catch {
      setHasWorkspace(false);
    } finally {
      setIsChecking(false);
    }
  };

  const handleComplete = async () => {
    try {
      await apiClient.post("/api/v1/onboarding/checklist/workspace/complete");
      onStepComplete();
    } catch (err: unknown) {
      console.error("Failed to complete workspace step:", err);
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
        {hasWorkspace ? (
          <>
            <CheckCircle2 className="h-12 w-12 text-green-500 mx-auto mb-3" />
            <h3 className="font-medium">Workspace Found</h3>
            <p className="text-sm text-muted-foreground mt-1">
              You have at least one workspace set up.
            </p>
            <Button onClick={handleComplete} className="mt-4">Continue</Button>
          </>
        ) : (
          <>
            <FolderOpen className="h-12 w-12 text-muted-foreground/50 mx-auto mb-3" />
            <h3 className="font-medium">Create a Workspace</h3>
            <p className="text-sm text-muted-foreground mt-1 max-w-md mx-auto">
              Create a workspace to store and manage your SuiteScript files. You can also ask the AI assistant for help.
            </p>
            <div className="flex gap-2 justify-center mt-4">
              <Button variant="outline" onClick={checkWorkspace}>Refresh</Button>
              <Button onClick={() => window.open("/workspaces", "_blank")}>
                Go to Workspaces
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
