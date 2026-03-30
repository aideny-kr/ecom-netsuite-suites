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
import {
  useReconRuns,
  useReconResults,
} from "@/hooks/use-reconciliation";
import { useReconPipeline } from "@/hooks/use-recon-pipeline";
import type { ReconResult } from "@/lib/types";

type TabId = "all" | "exceptions" | "unmatched";

export default function ReconciliationPage() {
  const router = useRouter();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TabId>("all");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  const { data: runs } = useReconRuns();
  const { data: results } = useReconResults(selectedRunId);
  const pipeline = useReconPipeline();

  const selectedRun = runs?.find((r) => r.id === selectedRunId) || null;

  const filteredResults = (results || []).filter((r) => {
    if (activeTab === "exceptions") return r.status === "suggested" || r.status === "pending";
    if (activeTab === "unmatched") return r.match_type === "unmatched";
    return true;
  });

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

  const handleInvestigate = (result: ReconResult) => {
    const payoutId = result.evidence?.payout_source_id || "unknown";
    const amount = Number(result.stripe_amount || result.variance_amount).toLocaleString("en-US", { style: "currency", currency: "USD" });
    const varType = result.variance_type || "unmatched";
    const query = `Investigate this reconciliation exception: Stripe payout ${payoutId} for ${amount} is ${varType}. No matching NetSuite deposit was found. Can you check NetSuite for deposits around this amount and date range?`;
    router.push(`/chat?agent=recon-agent&prefill=${encodeURIComponent(query)}`);
  };

  const tabs: { id: TabId; label: string; count: number }[] = [
    { id: "all", label: "All Results", count: results?.length || 0 },
    { id: "exceptions", label: "Exceptions", count: results?.filter((r) => r.status === "suggested" || r.status === "pending").length || 0 },
    { id: "unmatched", label: "Unmatched", count: results?.filter((r) => r.match_type === "unmatched").length || 0 },
  ];

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
      {!pipeline.isRunning && selectedRunId && <ReconSummaryBar run={selectedRun} />}

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

          {/* Exception cards (exceptions tab, top 5) */}
          {activeTab === "exceptions" && filteredResults.length > 0 && (
            <div className="space-y-4">
              {filteredResults.slice(0, 5).map((result) => (
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
            results={filteredResults}
            onInvestigate={handleInvestigate}
          />

          {/* Close checklist */}
          {selectedRun && selectedRun.status !== "closed" && (
            <CloseChecklist
              run={selectedRun}
              results={results || []}
              period={selectedRun.date_from.substring(0, 7)}
            />
          )}
        </>
      )}
    </div>
  );
}
