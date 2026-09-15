"use client";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  useTransactionAccess,
  useTransactionDecision,
} from "@/hooks/use-transaction-ops";
import { ProposalCard } from "../transaction-ops/proposal-card";
import { ExactChanges } from "../transaction-ops/evidence";
import { actionLabel, safeError } from "../transaction-ops/format";
import type { TransactionProposal } from "../transaction-ops/types";
import { approveExactBatch, type BulkDecisionResult } from "./bulk-approval";
export function BulkProposals({
  proposals,
}: {
  proposals: TransactionProposal[];
}) {
  const access = useTransactionAccess();
  const decision = useTransactionDecision();
  const [selected, setSelected] = useState<string[]>([]);
  const [frozen, setFrozen] = useState<TransactionProposal[] | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [results, setResults] = useState<BulkDecisionResult[]>([]);
  const live = useRef(proposals);
  live.current = proposals;
  const permitted = useRef(access.allowed);
  permitted.current = access.allowed;
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function approve() {
    if (!frozen || !reviewed || busy) return;
    setBusy(true);
    setError("");
    try {
      const outcome = await approveExactBatch(
        frozen,
        () => live.current,
        decision.mutateAsync,
        () => mounted.current && permitted.current,
      );
      if (mounted.current) {
        setResults(outcome);
        setFrozen(null);
        setSelected([]);
      }
    } catch (err) {
      if (mounted.current) setError(safeError(err));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <section className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-[13px] text-muted-foreground">
          Review up to 20 exact actions. Each approval is audited and its
          execution verified separately.
        </p>
        <Button
          disabled={!selected.length || busy}
          onClick={() => {
            setFrozen(
              structuredClone(proposals.filter((p) => selected.includes(p.id))),
            );
            setReviewed(false);
            setError("");
          }}
        >
          Review selected fixes ({selected.length})
        </Button>
      </div>
      {results.length > 0 && (
        <div role="status" className="rounded-xl border p-4 text-[13px]">
          <p>Approval results. Approval does not mean execution succeeded.</p>
          {results.map((r) => (
            <p key={r.id}>
              {r.id}:{" "}
              {r.status === "approved"
                ? "Approved · awaiting execution verification"
                : r.status === "unconfirmed"
                  ? "Approval unconfirmed · refresh status before retrying"
                  : "Not submitted"}
            </p>
          ))}
        </div>
      )}
      {!proposals.length && (
        <div className="rounded-xl border p-6">
          <h3 className="font-medium">No fix proposals available</h3>
          <p className="mt-2 text-[13px] text-muted-foreground">
            Investigate real cases first. A fix becomes available only when its
            evidence, repair mapping and connection safeguards are verified.
          </p>
        </div>
      )}
      {proposals.map((p) => (
        <div key={p.id} className="space-y-2">
          <label className="flex items-center gap-2 text-[13px]">
            <input
              type="checkbox"
              aria-label={`Select fix ${p.order_reference}`}
              checked={selected.includes(p.id)}
              disabled={
                busy ||
                p.status !== "pending" ||
                Date.parse(p.valid_until) <= Date.now() ||
                (!selected.includes(p.id) && selected.length >= 20)
              }
              onChange={(e) =>
                setSelected(
                  e.target.checked
                    ? [...selected, p.id]
                    : selected.filter((id) => id !== p.id),
                )
              }
            />
            Include this exact proposal in bulk review
          </label>
          <ProposalCard proposal={p} />
        </div>
      ))}
      <Dialog
        open={!!frozen}
        onOpenChange={(open) => {
          if (!open && !busy) setFrozen(null);
        }}
      >
        <DialogContent className="max-h-[90vh] max-w-4xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Approve {frozen?.length} exact actions</DialogTitle>
            <DialogDescription>
              Each action below uses its recorded evidence. A changed, expired
              or unconfirmed action stops further approvals. Already accepted
              actions may execute.
            </DialogDescription>
          </DialogHeader>
          {frozen?.map((p) => (
            <article key={p.id} className="rounded-lg border p-4">
              <h3 className="font-semibold">
                {p.order_reference} · {actionLabel(p.action)}
              </h3>
              <p className="mb-3 text-[13px] text-muted-foreground">
                {p.currency} · Account {p.netsuite_account_id} · Entity{" "}
                {p.subsidiary_id} · {p.target_record_id || "New record"}
              </p>
              <ExactChanges before={p.before_json} after={p.after_json} />
            </article>
          ))}
          <label className="flex items-start gap-2 text-[13px]">
            <input
              type="checkbox"
              checked={reviewed}
              disabled={busy}
              onChange={(e) => setReviewed(e.target.checked)}
            />
            I reviewed every action and its exact financial effect.
          </label>
          {error && (
            <p role="alert" className="text-destructive">
              {error}
            </p>
          )}
          <Button disabled={!reviewed || busy} onClick={approve}>
            {busy
              ? "Submitting approvals…"
              : `Approve ${frozen?.length} exact actions`}
          </Button>
        </DialogContent>
      </Dialog>
    </section>
  );
}
