"use client";

import { useState } from "react";
import Link from "next/link";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  useTransactionAccess,
  useTransactionRun,
  useTransactionFindings,
  useTransactionProposals,
} from "@/hooks/use-transaction-ops";
import { TransactionAccessBoundary } from "./access-boundary";
import { cardClass, EvidenceJson, FindingCard, Status } from "./evidence";
import { dateLabel, runState, safeError } from "./format";
import { ProposalCard } from "./proposal-card";

export function TransactionRunPage({ id }: { id: string }) {
  return (
    <TransactionAccessBoundary>
      <RunContent key={id} id={id} />
    </TransactionAccessBoundary>
  );
}
function Pagination({
  label,
  offset,
  hasNext,
  onChange,
}: {
  label: string;
  offset: number;
  hasNext: boolean;
  onChange: (offset: number) => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 text-[13px]">
      <span>
        Page {offset / 100 + 1} · Up to 100 {label} per page
      </span>
      <div className="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          aria-label={`Previous ${label} page`}
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - 100))}
        >
          Previous
        </Button>
        <Button
          variant="outline"
          size="sm"
          aria-label={`Next ${label} page`}
          disabled={!hasNext}
          onClick={() => onChange(offset + 100)}
        >
          Next
        </Button>
      </div>
    </div>
  );
}
function RunContent({ id }: { id: string }) {
  const [findingOffset, setFindingOffset] = useState(0);
  const [proposalOffset, setProposalOffset] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const queryClient = useQueryClient();
  const access = useTransactionAccess();
  const runQuery = useTransactionRun(id);
  const findings = useTransactionFindings(id, findingOffset);
  const proposals = useTransactionProposals(id, proposalOffset);
  async function refresh() {
    setRefreshing(true);
    try {
      await queryClient.invalidateQueries({
        queryKey: ["transaction-ops", access.tenantId],
      });
    } finally {
      setRefreshing(false);
    }
  }
  const run = runQuery.data;
  if (runQuery.isLoading) return <p role="status">Loading investigation…</p>;
  if (runQuery.error || !run)
    return (
      <div className="space-y-4">
        <p role="alert">
          Investigation could not be loaded. {safeError(runQuery.error)}
        </p>
        <Link href="/transaction-operations" className="underline">
          Back to transaction operations
        </Link>
      </div>
    );
  const config = run.config_snapshot;
  const progress = run.progress_json;
  const count = (key: string) =>
    typeof progress[key] === "number"
      ? String(progress[key])
      : "Not yet reported";
  return (
    <div className="animate-fade-in space-y-8 text-[15px]">
      <header className="space-y-4">
        <Link
          href="/transaction-operations"
          className="inline-flex items-center gap-2 text-[13px] text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Transaction operations
        </Link>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-2xl font-semibold tracking-tight">
              Review the evidence
            </h1>
            <p className="mt-2 break-words text-muted-foreground">
              {String(config.name || "Framework → NetSuite")} · {run.origin}{" "}
              investigation
            </p>
            <p className="mt-1 break-all text-[13px] text-muted-foreground">
              Run {run.id} · Started {dateLabel(run.created_at)}
            </p>
          </div>
          <Button variant="outline" onClick={refresh} disabled={refreshing}>
            <RefreshCw
              className={`mr-2 h-4 w-4 ${refreshing ? "animate-spin" : ""}`}
            />
            {refreshing ? "Refreshing…" : "Refresh status"}
          </Button>
        </div>
      </header>
      <section className="space-y-3 rounded-xl border bg-muted/30 p-5">
        <Status>{runState(run.status, run.termination_reason)}</Status>
        <p className="text-[13px] text-muted-foreground">
          {run.status === "pending"
            ? "This investigation is durably queued and waiting for a worker."
            : run.status === "running"
              ? "Provider reads are in progress. Findings appear as evidence is saved."
              : run.termination_reason === "done"
                ? "Investigation completed. Review the recorded findings and proposals below."
                : "This run stopped before completing its scope. A stopped run does not mean every order was examined. Review saved findings and start a new investigation when ready."}{" "}
          Approval and execution are tracked separately.
        </p>
      </section>
      <div className="grid gap-4 md:grid-cols-3">
        <div className={cardClass}>
          <p className="text-[13px] text-muted-foreground">Orders examined</p>
          <p className="mt-2 text-2xl font-semibold tabular-nums">
            {count("processed")}
          </p>
          <p className="mt-2 text-[13px] text-muted-foreground">
            {count("matched")} matched · {count("needs_review")} need review
          </p>
        </div>
        <div className={cardClass}>
          <p className="text-[13px] text-muted-foreground">API budget used</p>
          <p className="mt-2 text-2xl font-semibold tabular-nums">
            {run.api_calls_used} / {run.max_api_calls}
          </p>
          <p className="mt-2 text-[13px] text-muted-foreground">
            Order reservations: {run.orders_used} / {run.max_orders}
          </p>
        </div>
        <div className={cardClass}>
          <p className="text-[13px] text-muted-foreground">
            Fixed deadline (UTC)
          </p>
          <p className="mt-2 text-base font-semibold tabular-nums">
            {dateLabel(run.deadline_at)}
          </p>
          <p className="mt-2 text-[13px] text-muted-foreground">
            {run.finished_at
              ? `Finished ${dateLabel(run.finished_at)}`
              : "The budget does not reset on retry."}
          </p>
        </div>
      </div>
      <section className="space-y-4" aria-labelledby="proposals-heading">
        <div>
          <h2 id="proposals-heading" className="text-lg font-semibold">
            Proposals for review
          </h2>
          <p className="mt-1 text-[13px] text-muted-foreground">
            Each decision binds to an exact action and immutable evidence. An
            approved proposal is not proof of an external write.
          </p>
        </div>
        {proposals.isLoading ? (
          <p role="status">Loading proposals…</p>
        ) : proposals.error ? (
          <p role="alert">
            Proposals could not be loaded. {safeError(proposals.error)}
          </p>
        ) : (
          <>
            <Pagination
              label="proposals"
              offset={proposalOffset}
              hasNext={proposals.data?.hasNext || false}
              onChange={setProposalOffset}
            />
            {!proposals.data?.items.length ? (
              <p className={`${cardClass} text-[13px] text-muted-foreground`}>
                No proposals recorded on this page. Findings may still require
                review.
              </p>
            ) : (
              proposals.data.items.map((proposal) => (
                <ProposalCard key={proposal.id} proposal={proposal} />
              ))
            )}
          </>
        )}
      </section>
      <section className="space-y-4" aria-labelledby="findings-heading">
        <div>
          <h2 id="findings-heading" className="text-lg font-semibold">
            Order findings
          </h2>
          <p className="mt-1 text-[13px] text-muted-foreground">
            Open an order to inspect its exact transaction amounts, reasons, and
            full recorded evidence.
          </p>
        </div>
        {findings.isLoading ? (
          <p role="status">Loading findings…</p>
        ) : findings.error ? (
          <p role="alert">
            Findings could not be loaded. {safeError(findings.error)}
          </p>
        ) : (
          <>
            <Pagination
              label="findings"
              offset={findingOffset}
              hasNext={findings.data?.hasNext || false}
              onChange={setFindingOffset}
            />
            {!findings.data?.items.length ? (
              <p className={`${cardClass} text-[13px] text-muted-foreground`}>
                No findings recorded yet.
              </p>
            ) : (
              findings.data.items.map((finding) => (
                <FindingCard key={finding.id} finding={finding} />
              ))
            )}
          </>
        )}
      </section>
      <section className={`${cardClass} space-y-4`}>
        <h2 className="text-lg font-semibold">Run scope &amp; provenance</h2>
        <p className="break-words text-[13px] text-muted-foreground">
          Account {String(config.netsuite_account_id || "Not provided")} ·
          Subsidiary {String(config.subsidiary_id || "Not provided")} ·{" "}
          {String(config.record_type || "Not provided")}
        </p>
        <EvidenceJson
          value={run.params_json}
          label="Full investigation parameters"
        />
        <p className="text-[13px] text-muted-foreground">
          Findings preserve the source and destination evidence captured during
          this run. Refresh status reloads saved records; it does not refresh
          source evidence. To collect new evidence, start a new investigation.
        </p>
      </section>
    </div>
  );
}
