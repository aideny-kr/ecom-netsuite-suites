"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { Check, X, Play, Eye, ChevronDown, Shield, TestTube } from "lucide-react";
import type { ChangeSet } from "@/lib/types";
import {
  useTransitionChangeset,
  useApplyChangeset,
} from "@/hooks/use-changesets";
import { useTriggerValidate, useTriggerUnitTests } from "@/hooks/use-runs";

interface ChangesetPanelProps {
  changesets: ChangeSet[];
  onViewDiff: (changesetId: string) => void;
}

const statusColors: Record<string, string> = {
  draft: "bg-gray-100 text-gray-700",
  pending_review: "bg-yellow-100 text-yellow-700",
  approved: "bg-green-100 text-green-700",
  applied: "bg-blue-100 text-blue-700",
  rejected: "bg-red-100 text-red-700",
};

export function ChangesetPanel({ changesets, onViewDiff }: ChangesetPanelProps) {
  const transition = useTransitionChangeset();
  const apply = useApplyChangeset();
  const triggerValidate = useTriggerValidate();
  const triggerTests = useTriggerUnitTests();
  const [expandedId, setExpandedId] = useState<string | null>(null);

  if (changesets.length === 0) {
    return (
      <div className="flex h-32 items-center justify-center text-[13px] text-muted-foreground">
        No changesets yet
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      {changesets.map((cs) => {
        const runsDisabled = cs.status !== "approved";
        return (
          <div key={cs.id} className="rounded-lg border bg-card">
            <button
              onClick={() =>
                setExpandedId(expandedId === cs.id ? null : cs.id)
              }
              className="flex w-full items-center gap-2 px-3 py-2 text-left"
            >
              <ChevronDown
                className={cn(
                  "h-3 w-3 shrink-0 text-muted-foreground transition-transform",
                  expandedId !== cs.id && "-rotate-90",
                )}
              />
              <span className="flex-1 truncate text-[13px] font-medium">
                {cs.title}
              </span>
              <Badge
                variant="secondary"
                className={cn("text-[10px]", statusColors[cs.status])}
              >
                {cs.status.replace("_", " ")}
              </Badge>
            </button>
            {expandedId === cs.id && (
              <div className="border-t px-3 py-2 space-y-2">
                {cs.description && (
                  <p className="text-[12px] text-muted-foreground">
                    {cs.description}
                  </p>
                )}
                <div className="flex flex-wrap gap-1.5">
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 text-[12px]"
                    onClick={() => onViewDiff(cs.id)}
                  >
                    <Eye className="mr-1 h-3 w-3" />
                    View Diff
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 text-[12px]"
                    onClick={() => triggerValidate.mutate(cs.id)}
                    disabled={runsDisabled || triggerValidate.isPending}
                    title={
                      runsDisabled
                        ? "Changeset must be approved before validate/tests"
                        : undefined
                    }
                  >
                    <Shield className="mr-1 h-3 w-3" />
                    Validate
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 text-[12px]"
                    onClick={() => triggerTests.mutate(cs.id)}
                    disabled={runsDisabled || triggerTests.isPending}
                    title={
                      runsDisabled
                        ? "Changeset must be approved before validate/tests"
                        : undefined
                    }
                  >
                    <TestTube className="mr-1 h-3 w-3" />
                    Run Tests
                  </Button>
                  {cs.status === "draft" && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 text-[12px]"
                      onClick={() =>
                        transition.mutate({
                          changesetId: cs.id,
                          action: "submit",
                        })
                      }
                      disabled={transition.isPending}
                    >
                      Submit for Review
                    </Button>
                  )}
                  {cs.status === "pending_review" && (
                    <>
                      <Button
                        size="sm"
                        variant="default"
                        className="h-7 text-[12px]"
                        onClick={() =>
                          transition.mutate({
                            changesetId: cs.id,
                            action: "approve",
                          })
                        }
                        disabled={transition.isPending}
                      >
                        <Check className="mr-1 h-3 w-3" />
                        Approve
                      </Button>
                      <Button
                        size="sm"
                        variant="destructive"
                        className="h-7 text-[12px]"
                        onClick={() =>
                          transition.mutate({
                            changesetId: cs.id,
                            action: "reject",
                            reason: "Rejected by reviewer",
                          })
                        }
                        disabled={transition.isPending}
                      >
                        <X className="mr-1 h-3 w-3" />
                        Reject
                      </Button>
                    </>
                  )}
                  {cs.status === "approved" && (
                    <Button
                      size="sm"
                      variant="default"
                      className="h-7 text-[12px]"
                      onClick={() => apply.mutate(cs.id)}
                      disabled={apply.isPending}
                    >
                      <Play className="mr-1 h-3 w-3" />
                      Apply
                    </Button>
                  )}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
