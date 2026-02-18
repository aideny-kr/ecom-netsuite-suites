"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  Check,
  X,
  Play,
  Eye,
  ChevronDown,
  Shield,
  TestTube,
  Rocket,
  FileCheck2,
} from "lucide-react";
import type { AssertionDefinition, ChangeSet } from "@/lib/types";
import {
  useTransitionChangeset,
  useApplyChangeset,
} from "@/hooks/use-changesets";
import {
  useTriggerAssertions,
  useTriggerDeploySandbox,
  useTriggerUnitTests,
  useTriggerValidate,
  useUATReport,
} from "@/hooks/use-runs";

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

const DEFAULT_ASSERTIONS_TEMPLATE = JSON.stringify(
  [
    {
      name: "Customer row count",
      query: "SELECT COUNT(*) FROM customer",
      expected: {
        type: "row_count",
        operator: "gte",
        value: 0,
      },
    },
  ],
  null,
  2,
);

export function ChangesetPanel({
  changesets,
  onViewDiff,
}: ChangesetPanelProps) {
  const transition = useTransitionChangeset();
  const apply = useApplyChangeset();
  const triggerValidate = useTriggerValidate();
  const triggerTests = useTriggerUnitTests();
  const triggerAssertions = useTriggerAssertions();
  const triggerDeploy = useTriggerDeploySandbox();
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [uatChangesetId, setUatChangesetId] = useState<string | null>(null);
  const [sandboxTargets, setSandboxTargets] = useState<Record<string, string>>(
    {},
  );
  const [assertionDrafts, setAssertionDrafts] = useState<
    Record<string, string>
  >({});
  const [panelError, setPanelError] = useState<string | null>(null);

  const { data: uatReport, isLoading: isUatLoading } =
    useUATReport(uatChangesetId);

  const getAssertionDraft = (changesetId: string) =>
    assertionDrafts[changesetId] ?? DEFAULT_ASSERTIONS_TEMPLATE;

  const runAssertions = (changesetId: string) => {
    setPanelError(null);
    let assertions: AssertionDefinition[];
    try {
      const parsed = JSON.parse(getAssertionDraft(changesetId));
      if (!Array.isArray(parsed)) {
        throw new Error("Assertions input must be a JSON array");
      }
      assertions = parsed;
    } catch (error) {
      setPanelError(
        error instanceof Error ? error.message : "Invalid assertions JSON",
      );
      return;
    }
    triggerAssertions.mutate({ changesetId, assertions });
  };

  const triggerSandboxDeploy = (changesetId: string) => {
    setPanelError(null);
    const sandboxId = sandboxTargets[changesetId]?.trim();
    if (!sandboxId) {
      setPanelError("Sandbox target is required (example: 6738075-sb1).");
      return;
    }
    triggerDeploy.mutate({ changesetId, sandboxId });
  };

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
              onClick={() => setExpandedId(expandedId === cs.id ? null : cs.id)}
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
                {panelError && (
                  <p className="text-[12px] text-destructive">{panelError}</p>
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
                    <>
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-[12px]"
                        onClick={() => runAssertions(cs.id)}
                        disabled={triggerAssertions.isPending}
                      >
                        <Shield className="mr-1 h-3 w-3" />
                        Run Assertions
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-[12px]"
                        onClick={() =>
                          setUatChangesetId(
                            uatChangesetId === cs.id ? null : cs.id,
                          )
                        }
                      >
                        <FileCheck2 className="mr-1 h-3 w-3" />
                        {uatChangesetId === cs.id ? "Hide UAT" : "View UAT"}
                      </Button>
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
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-[12px]"
                        onClick={() => triggerSandboxDeploy(cs.id)}
                        disabled={triggerDeploy.isPending}
                        title="Deploy to NetSuite sandbox (requires approved + validate + tests)"
                      >
                        <Rocket className="mr-1 h-3 w-3" />
                        Deploy Sandbox
                      </Button>
                    </>
                  )}
                </div>
                {cs.status === "approved" && (
                  <div className="space-y-2 rounded-md border p-2">
                    <p className="text-[11px] font-medium text-muted-foreground">
                      SuiteQL Assertions (JSON)
                    </p>
                    <textarea
                      value={getAssertionDraft(cs.id)}
                      onChange={(event) =>
                        setAssertionDrafts((prev) => ({
                          ...prev,
                          [cs.id]: event.target.value,
                        }))
                      }
                      className="min-h-[100px] w-full rounded border bg-background p-2 font-mono text-[11px]"
                    />
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="text-[11px] text-muted-foreground">
                        Sandbox target
                      </label>
                      <input
                        value={sandboxTargets[cs.id] ?? ""}
                        onChange={(event) =>
                          setSandboxTargets((prev) => ({
                            ...prev,
                            [cs.id]: event.target.value,
                          }))
                        }
                        placeholder="6738075-sb1"
                        className="h-7 w-44 rounded border bg-background px-2 text-[11px]"
                      />
                    </div>
                  </div>
                )}
                {uatChangesetId === cs.id && (
                  <div className="space-y-2 rounded-md border p-2 text-[11px]">
                    {isUatLoading && (
                      <p className="text-muted-foreground">
                        Loading UAT report...
                      </p>
                    )}
                    {uatReport && (
                      <>
                        <p className="font-semibold">
                          Overall: {uatReport.overall_status}
                        </p>
                        <p className="text-muted-foreground">
                          Generated:{" "}
                          {new Date(uatReport.generated_at).toLocaleString()}
                        </p>
                        <div className="grid grid-cols-2 gap-1">
                          <p>Validate: {uatReport.gates.validate}</p>
                          <p>Unit tests: {uatReport.gates.unit_tests}</p>
                          <p>Assertions: {uatReport.gates.assertions}</p>
                          <p>Deploy: {uatReport.gates.deploy}</p>
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
