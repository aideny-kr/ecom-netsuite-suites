"use client";

import { useState, useMemo } from "react";
import {
  GitPullRequest,
  Eye,
  Check,
  X,
  ChevronDown,
  Send,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ToolCallStep, ProposePatchResult } from "@/lib/types";
import {
  useChangeset,
  useTransitionChangeset,
  useApplyChangeset,
} from "@/hooks/use-changesets";

interface ChangeProposalCardProps {
  step: ToolCallStep;
  workspaceId: string;
  onViewDiff: (changesetId: string) => void;
  onChangesetAction?: () => void;
}

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400",
  pending_review: "bg-blue-500/10 text-blue-700 dark:text-blue-400",
  approved: "bg-green-500/10 text-green-700 dark:text-green-400",
  applied: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  rejected: "bg-red-500/10 text-red-700 dark:text-red-400",
};

const STATUS_LABELS: Record<string, string> = {
  draft: "Draft",
  pending_review: "In Review",
  approved: "Approved",
  applied: "Applied",
  rejected: "Rejected",
};

function parseResult(resultSummary: string): ProposePatchResult | null {
  try {
    // Try full JSON parse first
    const parsed = JSON.parse(resultSummary);
    if (parsed.changeset_id) return parsed as ProposePatchResult;
  } catch {
    // Truncated JSON — extract changeset_id via regex
    const match = resultSummary.match(/"changeset_id":\s*"([^"]+)"/);
    if (match) {
      const patchMatch = resultSummary.match(/"patch_id":\s*"([^"]+)"/);
      const opMatch = resultSummary.match(/"operation":\s*"([^"]+)"/);
      const statusMatch = resultSummary.match(/"diff_status":\s*"([^"]+)"/);
      const riskMatch = resultSummary.match(/"risk_summary":\s*"([^"]+)"/);
      return {
        changeset_id: match[1],
        patch_id: patchMatch?.[1] ?? "",
        operation: (opMatch?.[1] as "modify" | "create" | "delete") ?? "modify",
        diff_status: statusMatch?.[1] ?? "unknown",
        risk_summary: riskMatch?.[1] ?? "",
      };
    }
  }
  return null;
}

function DiffLines({
  diff,
  maxLines = 15,
}: {
  diff: string;
  maxLines?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const lines = diff.split("\n");
  const shown = expanded ? lines : lines.slice(0, maxLines);
  const hasMore = lines.length > maxLines;

  return (
    <div className="overflow-hidden rounded border bg-background">
      <pre className="overflow-x-auto p-2 text-[11px] leading-[1.6] font-mono">
        {shown.map((line, i) => {
          let cls = "text-muted-foreground";
          if (line.startsWith("+") && !line.startsWith("+++"))
            cls = "text-green-600 dark:text-green-400 bg-green-500/5";
          else if (line.startsWith("-") && !line.startsWith("---"))
            cls = "text-red-600 dark:text-red-400 bg-red-500/5";
          else if (line.startsWith("@@"))
            cls = "text-blue-600 dark:text-blue-400";
          return (
            <div key={i} className={cls}>
              {line || " "}
            </div>
          );
        })}
      </pre>
      {hasMore && !expanded && (
        <button
          onClick={() => setExpanded(true)}
          className="w-full border-t px-2 py-1 text-[11px] text-primary hover:bg-accent/50"
        >
          Show {lines.length - maxLines} more lines...
        </button>
      )}
    </div>
  );
}

export function ChangeProposalCard({
  step,
  workspaceId,
  onViewDiff,
  onChangesetAction,
}: ChangeProposalCardProps) {
  const [diffOpen, setDiffOpen] = useState(true);
  const result = useMemo(() => parseResult(step.result_summary), [step.result_summary]);
  const changesetId = result?.changeset_id ?? null;

  const { data: changeset } = useChangeset(changesetId);
  const transition = useTransitionChangeset();
  const apply = useApplyChangeset();

  const title = (step.params.title as string) || "Change Proposal";
  const filePath = (step.params.file_path as string) || "";
  const unifiedDiff = (step.params.unified_diff as string) || "";
  const operation = result?.operation ?? "modify";
  const riskSummary = result?.risk_summary ?? "";
  const status = changeset?.status ?? result?.diff_status ?? "draft";
  const isError = result?.diff_status?.startsWith("parse_error");
  const isBusy = transition.isPending || apply.isPending;

  const handleTransition = (action: string) => {
    if (!changesetId) return;
    transition.mutate(
      { changesetId, action },
      { onSuccess: () => onChangesetAction?.() },
    );
  };

  const handleApply = () => {
    if (!changesetId) return;
    apply.mutate(changesetId, { onSuccess: () => onChangesetAction?.() });
  };

  if (!result) {
    // Fallback if parsing failed — show generic info
    return (
      <div className="rounded-lg border bg-background/80 p-3 text-[12px]">
        <div className="flex items-center gap-2">
          <GitPullRequest className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="font-medium">Change Proposal</span>
        </div>
        <p className="mt-1 text-muted-foreground">{step.result_summary}</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-background/80 text-[12px] overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2.5 border-b bg-muted/30">
        <GitPullRequest className="h-3.5 w-3.5 text-primary" />
        <span className="font-semibold text-[13px] truncate">{title}</span>
        <span className="ml-auto shrink-0 rounded-md bg-muted px-1.5 py-0.5 text-[11px] tabular-nums text-muted-foreground">
          {step.duration_ms}ms
        </span>
      </div>

      {/* Details */}
      <div className="px-3 py-2 space-y-1.5">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-muted-foreground">File:</span>
          <code className="rounded bg-muted px-1.5 py-0.5 text-[11px] font-mono">
            {filePath}
          </code>
          <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] capitalize">
            {operation}
          </span>
        </div>
        {riskSummary && (
          <p className="text-muted-foreground">{riskSummary}</p>
        )}
        <div className="flex items-center gap-2">
          <span className="text-muted-foreground">Status:</span>
          <span
            className={cn(
              "rounded-full px-2 py-0.5 text-[11px] font-medium",
              STATUS_COLORS[status] ?? "bg-muted text-muted-foreground",
            )}
          >
            {status === "applied" && <Check className="inline h-3 w-3 mr-0.5" />}
            {STATUS_LABELS[status] ?? status}
          </span>
          {isError && (
            <span className="text-[11px] text-destructive">Diff parse error</span>
          )}
        </div>
      </div>

      {/* Inline diff preview */}
      {unifiedDiff && (
        <div className="px-3 pb-2">
          <button
            onClick={() => setDiffOpen(!diffOpen)}
            className="flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground mb-1"
          >
            <ChevronDown
              className={cn(
                "h-3 w-3 transition-transform",
                !diffOpen && "-rotate-90",
              )}
            />
            Diff preview
          </button>
          {diffOpen && <DiffLines diff={unifiedDiff} />}
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center gap-1.5 border-t px-3 py-2 bg-muted/20">
        {changesetId && (
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-[11px]"
            onClick={() => onViewDiff(changesetId)}
          >
            <Eye className="mr-1 h-3 w-3" />
            View Full Diff
          </Button>
        )}

        {status === "draft" && (
          <>
            <Button
              size="sm"
              variant="default"
              className="h-7 text-[11px]"
              disabled={isBusy}
              onClick={() => handleTransition("submit")}
            >
              {isBusy ? (
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
              ) : (
                <Send className="mr-1 h-3 w-3" />
              )}
              Submit for Review
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 text-[11px] text-destructive"
              disabled={isBusy}
              onClick={() => handleTransition("discard")}
            >
              <X className="mr-1 h-3 w-3" />
              Discard
            </Button>
          </>
        )}

        {status === "pending_review" && (
          <>
            <Button
              size="sm"
              variant="default"
              className="h-7 text-[11px]"
              disabled={isBusy}
              onClick={() => handleTransition("approve")}
            >
              <Check className="mr-1 h-3 w-3" />
              Approve
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 text-[11px] text-destructive"
              disabled={isBusy}
              onClick={() => handleTransition("reject")}
            >
              <X className="mr-1 h-3 w-3" />
              Reject
            </Button>
          </>
        )}

        {status === "approved" && (
          <Button
            size="sm"
            variant="default"
            className="h-7 text-[11px]"
            disabled={isBusy}
            onClick={handleApply}
          >
            {isBusy ? (
              <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            ) : (
              <Check className="mr-1 h-3 w-3" />
            )}
            Apply Changes
          </Button>
        )}

        {status === "applied" && (
          <span className="flex items-center gap-1 text-[11px] text-emerald-600 dark:text-emerald-400 font-medium">
            <Check className="h-3 w-3" />
            Changes Applied
          </span>
        )}
      </div>
    </div>
  );
}
