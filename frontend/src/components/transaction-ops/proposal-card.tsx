"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogCancel,
} from "@/components/ui/alert-dialog";
import {
  useTransactionDecision,
  useTransactionOperation,
} from "@/hooks/use-transaction-ops";
import {
  actionLabel,
  dateLabel,
  objectValue,
  proposalState,
  safeError,
} from "./format";
import {
  cardClass,
  ComparisonEvidence,
  EvidenceJson,
  ExactChanges,
  inputClass,
  Status,
  TaxEvidenceNotice,
} from "./evidence";
import type { TransactionProposal } from "./types";
import { OutcomeRecheck } from "./outcome-recheck";
import { CreationReview } from "./creation-review";

export function ProposalCard({ proposal }: { proposal: TransactionProposal }) {
  const decision = useTransactionDecision();
  const operation = useTransactionOperation(proposal.id, proposal.status);
  const [now, setNow] = useState(Date.now());
  const [review, setReview] = useState<{
    proposal: TransactionProposal;
    decision: "approve" | "reject";
  } | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (proposal.status !== "pending") return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [proposal.status]);
  const fresh = Date.parse(proposal.valid_until) > now;
  const canDecide = proposal.status === "pending";
  const evidence = proposal.evidence_json;
  const report = Object.keys(objectValue(evidence.comparison)).length
    ? evidence
    : objectValue(evidence.report);
  function openReview(choice: "approve" | "reject") {
    setError("");
    setNote("");
    setReview({ proposal: structuredClone(proposal), decision: choice });
  }
  async function submit() {
    if (!review) return;
    const frozen = review.proposal;
    if (
      proposal.status !== "pending" ||
      proposal.evidence_fingerprint !== frozen.evidence_fingerprint ||
      (review.decision === "approve" &&
        !(Date.parse(frozen.valid_until) > Date.now()))
    ) {
      setError(
        "The state or evidence changed. Close this review, refresh status, and start a new investigation if needed.",
      );
      return;
    }
    try {
      await decision.mutateAsync({
        id: frozen.id,
        decision: review.decision,
        evidence_fingerprint: frozen.evidence_fingerprint,
        note: note.trim() || undefined,
      });
      setReview(null);
    } catch (err) {
      setError(safeError(err));
    }
  }
  return (
    <article className={`${cardClass} space-y-5`}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-[13px] text-muted-foreground">
            {actionLabel(proposal.action)} · {proposal.currency}
          </p>
          <h3 className="mt-1 break-all text-lg font-semibold">
            {proposal.order_reference}
          </h3>
          <p className="mt-2 break-words text-[13px] text-muted-foreground">
            Account {proposal.netsuite_account_id} · Subsidiary{" "}
            {proposal.subsidiary_id} · {proposal.record_type}
            {proposal.target_record_id
              ? ` ${proposal.target_record_id}`
              : " · No destination record"}
          </p>
        </div>
        <Status>
          {proposalState(
            proposal.status,
            proposal.valid_until,
            operation.data?.status,
            now,
          )}
        </Status>
      </div>
      {Object.keys(report).length > 0 && <ComparisonEvidence report={report} />}
      {proposal.action === "sync_missing_order" ? (
        <CreationReview after={proposal.after_json} />
      ) : (
        <ExactChanges
          before={proposal.before_json}
          after={proposal.after_json}
        />
      )}
      <div className="space-y-3 rounded-lg bg-muted/40 p-4">
        <h4 className="text-[13px] font-semibold">Sources &amp; method</h4>
        <p className="text-[13px] text-muted-foreground">
          Evidence observed {dateLabel(proposal.observed_at)}. Valid until{" "}
          {dateLabel(proposal.valid_until)}. Approval applies only to this
          action, scope, and exact evidence. Amounts are supplied by the server;
          the review does not calculate or override them.
        </p>
        <p className="break-all font-mono text-xs">
          Evidence fingerprint: {proposal.evidence_fingerprint}
        </p>
        <EvidenceJson value={evidence} />
      </div>
      {operation.error && (
        <p role="alert" className="text-[13px]">
          Execution status could not be loaded. Refresh status before taking
          further action.
        </p>
      )}
      {operation.data && (
        <div className="space-y-3 text-[13px]">
          <p>
            Attempt recorded {dateLabel(operation.data.attempted_at)}
            {operation.data.completed_at
              ? ` · Settled ${dateLabel(operation.data.completed_at)}`
              : ""}
          </p>
          {operation.data.status === "unknown" && (
            <>
              <p className="rounded-md border p-3">
                The external outcome must be reconciled before another attempt.
                A timeout does not prove the write failed.
              </p>
              <OutcomeRecheck key={proposal.id} proposalId={proposal.id} />
            </>
          )}
          <EvidenceJson
            value={operation.data.result_json}
            label="Execution result"
          />
        </div>
      )}
      {proposal.status === "approved" && !operation.data && (
        <p className="text-[13px] text-muted-foreground">
          Approval is recorded. An external write has not been verified.
        </p>
      )}
      {proposal.decided_at && (
        <p className="text-[13px] text-muted-foreground">
          Decision recorded {dateLabel(proposal.decided_at)}
          {proposal.decision_note ? ` · ${proposal.decision_note}` : ""}
        </p>
      )}
      {proposal.status === "pending" && !fresh && (
        <p className="text-[13px]">
          This evidence has expired. Refreshing status does not renew it.{" "}
          <Link
            href="/transaction-operations"
            className="underline underline-offset-4"
          >
            Start a new investigation
          </Link>{" "}
          to obtain current evidence.
        </p>
      )}
      {canDecide && (
        <div className="flex flex-wrap items-center justify-between gap-4 border-t pt-4">
          <p className="max-w-lg text-[13px] text-muted-foreground">
            Your decision is recorded with your authenticated identity. Approval
            does not confirm an external write.
          </p>
          <div className="flex flex-wrap gap-3">
            <Button variant="outline" onClick={() => openReview("reject")}>
              Reject proposal
            </Button>
            {fresh && (
              <Button
                className="bg-foreground text-background hover:bg-foreground/90"
                onClick={() => openReview("approve")}
              >
                Review approval
              </Button>
            )}
          </div>
        </div>
      )}
      <AlertDialog
        open={!!review}
        onOpenChange={(open) => {
          if (!open && !decision.isPending) setReview(null);
        }}
      >
        <AlertDialogContent
          className={`max-h-[90dvh] overflow-y-auto overflow-x-hidden max-sm:w-[calc(100%-2rem)] [&>*]:min-w-0 ${review?.proposal.action === "sync_missing_order" ? "sm:max-w-5xl" : ""}`}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>
              {review?.decision === "approve"
                ? "Approve this exact proposal?"
                : "Reject this proposal?"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              Your authenticated decision applies only to the values and
              evidence shown here. Approval permits the server to revalidate and
              attempt the proposed action.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {review && (
            <>
              <div className="min-w-0 space-y-2 text-[13px]">
                <p className="font-semibold break-all">
                  {actionLabel(review.proposal.action)} ·{" "}
                  {review.proposal.order_reference} · {review.proposal.currency}
                </p>
                <p>
                  Account {review.proposal.netsuite_account_id} · Subsidiary{" "}
                  {review.proposal.subsidiary_id} ·{" "}
                  {review.proposal.record_type}{" "}
                  {review.proposal.target_record_id}
                </p>
                <TaxEvidenceNotice evidence={review.proposal.evidence_json} />
                {review.proposal.action === "sync_missing_order" ? (
                  <CreationReview after={review.proposal.after_json} />
                ) : (
                  <ExactChanges
                    before={review.proposal.before_json}
                    after={review.proposal.after_json}
                  />
                )}
                <p className="break-all font-mono text-xs">
                  {review.proposal.evidence_fingerprint}
                </p>
              </div>
              <label className="space-y-2 text-[13px]">
                Review note (optional)
                <textarea
                  className={inputClass}
                  maxLength={2000}
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  disabled={decision.isPending}
                />
              </label>
              {error && (
                <p role="alert" className="rounded-md border p-3 text-[13px]">
                  {error}
                </p>
              )}
              <AlertDialogFooter>
                <AlertDialogCancel disabled={decision.isPending}>
                  Cancel
                </AlertDialogCancel>
                <Button
                  className="bg-foreground text-background hover:bg-foreground/90"
                  disabled={decision.isPending}
                  onClick={submit}
                >
                  {decision.isPending
                    ? "Recording decision…"
                    : review.decision === "approve"
                      ? "Approve this proposal"
                      : "Confirm rejection"}
                </Button>
              </AlertDialogFooter>
            </>
          )}
        </AlertDialogContent>
      </AlertDialog>
    </article>
  );
}
