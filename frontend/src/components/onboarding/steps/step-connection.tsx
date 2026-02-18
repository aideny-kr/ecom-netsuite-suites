"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { apiClient } from "@/lib/api-client";
import { CheckCircle2, Loader2, ExternalLink } from "lucide-react";

interface StepConnectionProps {
  onStepComplete: () => void;
}

export function StepConnection({ onStepComplete }: StepConnectionProps) {
  const [status, setStatus] = useState<
    "idle" | "checking" | "connected" | "error"
  >("idle");
  const [message, setMessage] = useState("");
  const [isDiscovering, setIsDiscovering] = useState(false);

  useEffect(() => {
    checkConnection();
  }, []);

  const checkConnection = async () => {
    setStatus("checking");
    try {
      const result = await apiClient.get<{
        step_key: string;
        valid: boolean;
        reason?: string;
      }>("/api/v1/onboarding/checklist/connection/validate");
      if (result.valid) {
        setStatus("connected");
        setMessage("NetSuite is connected!");
      } else {
        setStatus("idle");
        setMessage(result.reason || "Not connected yet");
      }
    } catch {
      setStatus("idle");
      setMessage("No connection found");
    }
  };

  const handleComplete = async () => {
    try {
      setIsDiscovering(true);
      const discovery = await apiClient.post<{
        status: string;
        summary?: Record<string, unknown>;
        snapshot_profile_id?: string;
        snapshot_version?: number;
      }>("/api/v1/onboarding/discover");

      if (discovery.status !== "completed") {
        throw new Error("NetSuite discovery did not complete successfully");
      }

      await apiClient.post("/api/v1/onboarding/checklist/connection/complete", {
        metadata: {
          discovery_status: discovery.status,
          summary: discovery.summary || null,
          snapshot_profile_id: discovery.snapshot_profile_id || null,
          snapshot_version: discovery.snapshot_version || null,
        },
      });
      onStepComplete();
    } catch (err: unknown) {
      setMessage(
        err instanceof Error ? err.message : "Failed to complete step",
      );
    } finally {
      setIsDiscovering(false);
    }
  };

  return (
    <div className="space-y-6 p-6">
      <div className="rounded-lg border bg-muted/30 p-6 text-center">
        {status === "connected" ? (
          <>
            <CheckCircle2 className="h-12 w-12 text-green-500 mx-auto mb-3" />
            <h3 className="font-medium">NetSuite Connected</h3>
            <p className="text-sm text-muted-foreground mt-1">{message}</p>
            <Button
              onClick={handleComplete}
              className="mt-4"
              disabled={isDiscovering}
            >
              {isDiscovering
                ? "Running Discovery..."
                : "Run Discovery & Continue"}
            </Button>
          </>
        ) : status === "checking" ? (
          <>
            <Loader2 className="h-12 w-12 text-muted-foreground mx-auto mb-3 animate-spin" />
            <h3 className="font-medium">Checking connection...</h3>
          </>
        ) : (
          <>
            <ExternalLink className="h-12 w-12 text-muted-foreground/50 mx-auto mb-3" />
            <h3 className="font-medium">Connect your NetSuite account</h3>
            <p className="text-sm text-muted-foreground mt-1 max-w-md mx-auto">
              {message ||
                "Use the AI assistant on the right to start the OAuth connection flow, or skip this step for now."}
            </p>
            <div className="flex gap-2 justify-center mt-4">
              <Button variant="outline" onClick={checkConnection}>
                Check Connection
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
