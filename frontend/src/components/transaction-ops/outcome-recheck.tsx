"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { useRecheckTransactionOperation, useTransactionRun } from "@/hooks/use-transaction-ops";
import { objectValue, safeError } from "./format";

export function OutcomeRecheck({ proposalId }: { proposalId: string }) {
  const recheck = useRecheckTransactionOperation();
  const [runId, setRunId] = useState("");
  const run = useTransactionRun(runId);
  const [error, setError] = useState("");
  const requestKey = useRef<string | null>(null);
  const submitting = useRef(false);
  const waiting = !!runId && run.data?.status !== "finished";

  async function submit() {
    if (submitting.current || waiting) return;
    submitting.current = true;
    setError("");
    try {
      requestKey.current ??= crypto.randomUUID();
      const result = await recheck.mutateAsync({ id: proposalId, evaluation_key: requestKey.current });
      setRunId(result.id);
      requestKey.current = null;
    } catch (err) {
      setError(objectValue(err).status === 409
        ? "Another outcome check may already be running, or the operation has changed. Refresh status before trying again."
        : safeError(err));
    } finally {
      submitting.current = false;
    }
  }

  return (
    <div className="space-y-3 rounded-lg border bg-muted/20 p-4 text-[13px]">
      <h4 className="font-semibold">Check the current outcome</h4>
      <p>
        Read fresh evidence to check whether the approved change completed.
        This check cannot send another write. The original attempt and previous checks remain recorded.
      </p>
      <Button variant="outline" className="max-sm:w-full" disabled={recheck.isPending || waiting} onClick={submit}>
        {recheck.isPending ? "Requesting check…" : waiting ? "Recheck queued" : "Recheck outcome"}
      </Button>
      {runId && (
        <p role="status">
          {waiting ? "A read-only check is queued or running. " : "The read-only check has finished. "}
          <Link href={`/transaction-operations/runs/${encodeURIComponent(runId)}`} className="underline underline-offset-4">
            View check
          </Link>
        </p>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
