"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiClient } from "@/lib/api-client";
import { useCreateWorkspace } from "@/hooks/use-workspace";
import { CheckCircle2, FolderOpen, Loader2, Plus } from "lucide-react";
import { useToast } from "@/hooks/use-toast";

interface StepWorkspaceProps {
  onStepComplete: () => void;
}

export function StepWorkspace({ onStepComplete }: StepWorkspaceProps) {
  const [hasWorkspace, setHasWorkspace] = useState(false);
  const [isChecking, setIsChecking] = useState(true);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const createMutation = useCreateWorkspace();
  const { toast } = useToast();

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

  const handleCreate = async () => {
    if (!name.trim()) return;
    try {
      await createMutation.mutateAsync({ name, description });
      toast({
        title: "Workspace created",
        description: "Your workspace has been created successfully.",
      });
      await checkWorkspace();
    } catch (err) {
      toast({
        title: "Failed to create workspace",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
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

  if (isChecking && !createMutation.isPending) {
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
          <div className="animate-in fade-in zoom-in duration-300">
            <CheckCircle2 className="h-12 w-12 text-green-500 mx-auto mb-3" />
            <h3 className="font-medium text-lg">Workspace Found</h3>
            <p className="text-sm text-muted-foreground mt-1 mb-6">
              You have successfully set up your workspace.
            </p>
            <Button onClick={handleComplete} size="lg" className="w-full sm:w-auto">
              Continue to Next Step
            </Button>
          </div>
        ) : (
          <div className="space-y-6">
            <div className="text-center">
              <FolderOpen className="h-10 w-10 text-primary/50 mx-auto mb-3" />
              <h3 className="font-medium text-lg">Create Your First Workspace</h3>
              <p className="text-sm text-muted-foreground mt-1 max-w-md mx-auto">
                A workspace is where you&apos;ll manage your SuiteScript files and deployments.
              </p>
            </div>

            <div className="mx-auto max-w-sm space-y-4 text-left bg-background p-4 rounded-lg border shadow-sm">
              <div className="space-y-2">
                <Label htmlFor="workspace-name">Workspace Name</Label>
                <Input
                  id="workspace-name"
                  placeholder="e.g., My Project"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="workspace-desc">Description (Optional)</Label>
                <Input
                  id="workspace-desc"
                  placeholder="Brief description..."
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </div>
              <Button
                onClick={handleCreate}
                disabled={!name.trim() || createMutation.isPending}
                className="w-full"
              >
                {createMutation.isPending ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Creating...
                  </>
                ) : (
                  <>
                    <Plus className="mr-2 h-4 w-4" /> Create Workspace
                  </>
                )}
              </Button>
            </div>

            <div className="relative">
              <div className="absolute inset-0 flex items-center">
                <span className="w-full border-t" />
              </div>
              <div className="relative flex justify-center text-xs uppercase">
                <span className="bg-muted/30 px-2 text-muted-foreground">Or</span>
              </div>
            </div>

            <div className="flex gap-2 justify-center">
              <Button variant="outline" onClick={checkWorkspace} disabled={isChecking}>
                {isChecking ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                Check Again
              </Button>
              <Button variant="ghost" onClick={() => window.open("/workspaces", "_blank")}>
                Go to Workspaces
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
