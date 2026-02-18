"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { ChevronDown } from "lucide-react";
import type { WorkspaceRun } from "@/lib/types";
import { useRunArtifacts } from "@/hooks/use-runs";

interface RunsPanelProps {
  runs: WorkspaceRun[];
}

const statusStyles: Record<string, string> = {
  queued: "bg-gray-100 text-gray-700",
  running: "bg-blue-100 text-blue-700 animate-pulse",
  passed: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  error: "bg-orange-100 text-orange-700",
};

const runTypeLabels: Record<string, string> = {
  sdf_validate: "SDF Validate",
  jest_unit_test: "Jest Tests",
  suiteql_assertions: "SuiteQL Assertions",
  deploy_sandbox: "Sandbox Deploy",
};

function formatDuration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function RunDetail({ runId }: { runId: string }) {
  const { data: artifacts = [] } = useRunArtifacts(runId);

  if (artifacts.length === 0) {
    return (
      <p className="text-[11px] text-muted-foreground italic">
        No output yet
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {artifacts.map((a) => (
        <div key={a.id}>
          <p className="text-[10px] font-medium uppercase text-muted-foreground mb-0.5">
            {a.artifact_type}
          </p>
          <pre className="max-h-[200px] overflow-auto rounded bg-muted/50 p-2 text-[11px] font-mono whitespace-pre-wrap break-all">
            {a.content || "(empty)"}
          </pre>
        </div>
      ))}
    </div>
  );
}

export function RunsPanel({ runs }: RunsPanelProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  if (runs.length === 0) {
    return (
      <div className="flex h-24 items-center justify-center text-[13px] text-muted-foreground">
        No runs yet
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      {runs.map((run) => (
        <div key={run.id} className="rounded-lg border bg-card">
          <button
            onClick={() =>
              setExpandedId(expandedId === run.id ? null : run.id)
            }
            className="flex w-full items-center gap-2 px-3 py-2 text-left"
          >
            <ChevronDown
              className={cn(
                "h-3 w-3 shrink-0 text-muted-foreground transition-transform",
                expandedId !== run.id && "-rotate-90",
              )}
            />
            <span className="flex-1 truncate text-[12px] font-medium">
              {runTypeLabels[run.run_type] || run.run_type}
            </span>
            <span className="text-[10px] text-muted-foreground">
              {formatDuration(run.duration_ms)}
            </span>
            <Badge
              variant="secondary"
              className={cn("text-[10px]", statusStyles[run.status])}
            >
              {run.status}
            </Badge>
          </button>
          {expandedId === run.id && (
            <div className="border-t px-3 py-2">
              <RunDetail runId={run.id} />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
