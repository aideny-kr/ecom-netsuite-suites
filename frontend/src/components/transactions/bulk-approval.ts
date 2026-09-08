import type {
  DecisionInput,
  TransactionProposal,
} from "../transaction-ops/types";
export type BulkDecisionResult = {
  id: string;
  status: "approved" | "unconfirmed" | "not_submitted";
};
export async function approveExactBatch(
  frozen: TransactionProposal[],
  current: () => TransactionProposal[],
  decide: (input: DecisionInput) => Promise<unknown>,
  allowed: () => boolean,
): Promise<BulkDecisionResult[]> {
  if (
    !allowed() ||
    !frozen.length ||
    frozen.length > 20 ||
    new Set(frozen.map((p) => p.id)).size !== frozen.length
  )
    throw new Error("Select up to 20 exact actions with approval access.");
  const valid = (p: TransactionProposal) => {
    const live = current().find((v) => v.id === p.id);
    return (
      live?.status === "pending" &&
      live.evidence_fingerprint === p.evidence_fingerprint &&
      live.valid_until === p.valid_until &&
      Date.parse(p.valid_until) > Date.now()
    );
  };
  if (!frozen.every(valid))
    throw new Error(
      "The selection changed or evidence expired. Review refreshed proposals.",
    );
  const results: BulkDecisionResult[] = [];
  let stopped = false;
  for (const row of frozen) {
    if (stopped || !allowed() || !valid(row)) {
      stopped = true;
      results.push({ id: row.id, status: "not_submitted" });
      continue;
    }
    try {
      await decide({
        id: row.id,
        decision: "approve",
        evidence_fingerprint: row.evidence_fingerprint,
      });
      results.push({ id: row.id, status: "approved" });
    } catch {
      // The response may be lost after the server committed approval. Never retry automatically.
      stopped = true;
      results.push({ id: row.id, status: "unconfirmed" });
    }
  }
  return results;
}
