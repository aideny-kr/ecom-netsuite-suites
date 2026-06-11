"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { Scale, Play, Download, Calendar } from "lucide-react";
import { ReconSummaryBar } from "@/components/reconciliation/recon-summary-bar";
import { ReconResultsTable } from "@/components/reconciliation/recon-results-table";
import { ReconExceptionCard } from "@/components/reconciliation/recon-exception-card";
import { ReconProgressStepper } from "@/components/reconciliation/recon-progress-stepper";
import { DataFreshnessBanner } from "@/components/reconciliation/data-freshness-banner";
import { CloseChecklist } from "@/components/reconciliation/close-checklist";
import { BulkApprovalCard } from "@/components/reconciliation/bulk-approval-card";
import {
  useReconRuns,
  useReconResults,
  useReconBucketSummary,
  useApproveBucket,
} from "@/hooks/use-reconciliation";
import { useReconPipeline } from "@/hooks/use-recon-pipeline";
import { useFeature } from "@/hooks/use-features";
import type { ReconResult, ReconBucketId } from "@/lib/types";

const BUCKET_TABS: { id: ReconBucketId; label: string }[] = [
  { id: "matches", label: "Matches" },
  { id: "rules", label: "Rules" },
  { id: "auto_classifications", label: "Auto-Classifications" },
  { id: "needs_review", label: "Needs Review" },
];

const BULK_APPROVABLE: ReconBucketId[] = [
  "matches",
  "rules",
  "auto_classifications",
];

export default function ReconciliationPage() {
  const router = useRouter();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<ReconBucketId>("matches");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  // Bumped after every successful bulk-approve so the BulkApprovalCard clears its
  // notes — a note must not survive an approve and ride into a re-approval.
  const [approveResetSignal, setApproveResetSignal] = useState(0);

  const { data: runs } = useReconRuns();
  const { data: results } = useReconResults(selectedRunId, undefined, activeTab);
  // The CloseChecklist's auto-checks key on summary.close_readiness — live
  // server-side counts over the FULL run. (It previously scanned an unbucketed
  // useReconResults page, which only ever saw limit=100 rows at scale.)
  const summary = useReconBucketSummary(selectedRunId);
  const approveBucket = useApproveBucket(selectedRunId || "");
  const reconEnabled = useFeature("reconciliation");
  const pipeline = useReconPipeline();

  const selectedRun = runs?.find((r) => r.id === selectedRunId) || null;

  const activeBucket = BUCKET_TABS.find((t) => t.id === activeTab)!;
  const activeCount = summary.data?.[activeTab]?.count ?? 0;
  const activeVariance = Number(summary.data?.[activeTab]?.total_variance ?? 0);
  const isRunClosed = selectedRun?.status === "closed";
  const isBulkApprovable = BULK_APPROVABLE.includes(activeTab) && !isRunClosed;

  // The approve-bucket mutation returns { approved_count, skipped_count, ... }
  // (ReconBucketApproveResult). The bucket count is status-agnostic, so surface
  // what actually changed rather than implying every line was approved.
  const approveResult = approveBucket.data as
    | { approved_count: number; skipped_count: number }
    | undefined;

  const handleRunRecon = () => {
    if (!dateFrom || !dateTo) return;
    pipeline.runPipeline({ date_from: dateFrom, date_to: dateTo });
  };

  // Auto-select the run when pipeline completes
  useEffect(() => {
    if (pipeline.runId && pipeline.runId !== selectedRunId) {
      setSelectedRunId(pipeline.runId);
    }
  }, [pipeline.runId]);

  // Auto-select the latest run when none is selected yet
  useEffect(() => {
    if (!selectedRunId && runs && runs.length > 0) {
      setSelectedRunId(runs[0].id);
    }
  }, [runs, selectedRunId]);

  const handleApproveBucket = (notes: string) => {
    if (!selectedRunId || !isBulkApprovable) return;
    // notes are audit-only — only send the field when the operator typed one.
    const trimmed = notes.trim();
    approveBucket.mutate(
      trimmed ? { bucket: activeTab, notes: trimmed } : { bucket: activeTab },
      // Clear the card's note on success so it can't carry into a re-approval.
      { onSuccess: () => setApproveResetSignal((n) => n + 1) }
    );
  };

  const handleInvestigate = (result: ReconResult) => {
    const orderRef = result.evidence?.order_reference;
    const chargeId = result.evidence?.charge_source_id || result.evidence?.payout_source_id;
    const amount = Number(result.stripe_amount || result.variance_amount).toLocaleString("en-US", { style: "currency", currency: "USD" });
    const dateRange = selectedRun ? `between ${selectedRun.date_from} and ${selectedRun.date_to}` : "";

    let query: string;
    if (orderRef) {
      query = `Use SuiteQL to investigate order ${orderRef} in NetSuite. A Stripe charge of ${amount} ${dateRange} has no matching customer deposit. Run: SELECT t.id, t.tranid, t.trandate, t.total, BUILTIN.DF(t.entity) AS customer FROM transaction t WHERE t.type = 'CustDep' AND t.tranid LIKE '%${orderRef}%' OR t.total = ${Number(result.stripe_amount || 0).toFixed(2)} FETCH FIRST 10 ROWS ONLY`;
    } else {
      query = `Use SuiteQL to find customer deposits in NetSuite around ${amount} ${dateRange}. Run: SELECT t.id, t.tranid, t.trandate, t.total, BUILTIN.DF(t.entity) AS customer FROM transaction t WHERE t.type = 'CustDep' AND t.total BETWEEN ${(Number(result.stripe_amount || 0) * 0.95).toFixed(2)} AND ${(Number(result.stripe_amount || 0) * 1.05).toFixed(2)} ${dateFrom ? `AND t.trandate >= TO_DATE('${dateFrom}', 'YYYY-MM-DD') AND t.trandate <= TO_DATE('${dateTo}', 'YYYY-MM-DD')` : ""} FETCH FIRST 10 ROWS ONLY`;
    }
    // Use unified agent (not recon-agent) — it has SuiteQL tools
    router.push(`/chat?prefill=${encodeURIComponent(query)}&new_session=true`);
  };

  const tabs = BUCKET_TABS.map((t) => ({
    ...t,
    count: summary.data?.[t.id]?.count ?? 0,
  }));

  return (
    <div className="animate-fade-in space-y-8 p-8">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Scale className="h-6 w-6 text-muted-foreground" />
          <h1 className="text-2xl font-bold text-foreground">Reconciliation</h1>
        </div>

        <div className="flex items-center gap-3">
          <input
            type="date"
            value={dateFrom}
            onChange={(e) => setDateFrom(e.target.value)}
            className="rounded-lg border bg-background px-3 py-1.5 text-[13px]"
          />
          <span className="text-muted-foreground">to</span>
          <input
            type="date"
            value={dateTo}
            onChange={(e) => setDateTo(e.target.value)}
            className="rounded-lg border bg-background px-3 py-1.5 text-[13px]"
          />
          <button
            onClick={handleRunRecon}
            disabled={!dateFrom || !dateTo || pipeline.isRunning}
            className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-1.5 text-[13px] font-medium text-white hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            <Play className="h-3.5 w-3.5" />
            {pipeline.isRunning ? "Running..." : "Run Reconciliation"}
          </button>
        </div>
      </div>

      {/* Data freshness banner — always visible */}
      {!pipeline.isRunning && <DataFreshnessBanner />}

      {/* Pipeline progress stepper — visible during/after a run */}
      {(pipeline.isRunning || pipeline.summary || pipeline.error) && (
        <ReconProgressStepper
          stages={pipeline.stages}
          progress={pipeline.progress}
          error={pipeline.error}
          summary={pipeline.summary}
        />
      )}

      {/* Previous runs selector */}
      {runs && runs.length > 0 && !pipeline.isRunning && (
        <div className="flex items-center gap-2">
          <Calendar className="h-4 w-4 text-muted-foreground" />
          <select
            value={selectedRunId || ""}
            onChange={(e) => setSelectedRunId(e.target.value || null)}
            className="rounded-lg border bg-background px-3 py-1.5 text-[13px]"
          >
            <option value="">Select a previous run...</option>
            {runs.map((run) => (
              <option key={run.id} value={run.id}>
                {run.date_from} to {run.date_to} — {run.status} ({run.matched_count} matched)
              </option>
            ))}
          </select>
          {selectedRunId && (
            <a
              href={`/api/v1/reconciliation/evidence/${selectedRunId}`}
              className="flex items-center gap-1 rounded-lg border px-3 py-1.5 text-[13px] text-muted-foreground hover:bg-muted/50 transition-colors"
            >
              <Download className="h-3.5 w-3.5" />
              Evidence Pack
            </a>
          )}
        </div>
      )}

      {/* Summary bar */}
      {!pipeline.isRunning && selectedRunId && (
        <ReconSummaryBar summary={summary.data ?? null} run={selectedRun} />
      )}

      {/* Tabs + results */}
      {!pipeline.isRunning && selectedRunId && (
        <>
          <div className="flex gap-1 border-b">
            {tabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`px-4 py-2 text-[13px] font-medium border-b-2 transition-colors ${
                  activeTab === tab.id
                    ? "border-blue-600 text-blue-600"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                }`}
              >
                {tab.label} ({tab.count})
              </button>
            ))}
          </div>

          {/* Bulk-approval card for bulk-approvable buckets.
              Never offered on a closed run (mirrors the CloseChecklist gate). */}
          {isBulkApprovable && (
            <div className="space-y-2">
              {/* key={activeTab}: remount per bucket so a note typed for one
                  bucket can't carry into the next bucket's approve. */}
              <BulkApprovalCard
                key={activeTab}
                bucketLabel={activeBucket.label}
                count={activeCount}
                totalVariance={activeVariance}
                onApprove={handleApproveBucket}
                isApproving={approveBucket.isPending}
                disabled={!reconEnabled || activeCount === 0 || isRunClosed}
                resetSignal={approveResetSignal}
              />
              {/* Surface what bulk-approve actually did — the bucket count is
                  status-agnostic and can overstate the eligible set. */}
              {approveResult && (
                <p className="text-[13px] text-green-700">
                  Approved {approveResult.approved_count} · skipped{" "}
                  {approveResult.skipped_count} already-decided
                </p>
              )}
            </div>
          )}

          {/* Exception cards (needs-review tab, top 5) */}
          {activeTab === "needs_review" && (results || []).length > 0 && (
            <div className="space-y-4">
              {(results || []).slice(0, 5).map((result) => (
                <ReconExceptionCard
                  key={result.id}
                  result={result}
                  onInvestigate={handleInvestigate}
                />
              ))}
            </div>
          )}

          {/* Results table */}
          <ReconResultsTable
            results={results || []}
            onInvestigate={handleInvestigate}
          />

          {/* Close checklist — gated on the FULL run via the server-computed
              close_readiness counts, not the active bucket or a results page */}
          {selectedRun && selectedRun.date_from && selectedRun.status !== "closed" && (
            <CloseChecklist
              run={selectedRun}
              summary={summary.data}
              period={selectedRun.date_from.substring(0, 7)}
            />
          )}
        </>
      )}
    </div>
  );
}
