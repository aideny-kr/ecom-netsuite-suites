import React from "react";
import { expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { BulkProposals } from "./bulk-proposals";
import type { TransactionProposal } from "../transaction-ops/types";
const mocks = vi.hoisted(() => ({ decide: vi.fn().mockResolvedValue({}) }));
vi.mock("@/hooks/use-transaction-ops", () => ({
  useTransactionAccess: () => ({ allowed: true }),
  useTransactionDecision: () => ({ mutateAsync: mocks.decide }),
}));
vi.mock("../transaction-ops/proposal-card", () => ({
  ProposalCard: () => <p>Proposal evidence</p>,
}));
it("requires a visible exact batch review and acknowledgement before any decision", async () => {
  const proposals = ["R123456789", "R123456788"].map((reference, i) => ({
    id: String(i),
    tenant_id: "tenant",
    config_id: "config",
    run_id: "run",
    work_key: "work",
    source_record_id: "1",
    target_record_id: "2",
    record_type: "salesorder",
    observed_at: new Date().toISOString(),
    evidence_json: {},
    decided_by: null,
    decided_at: null,
    decision_note: null,
    created_at: new Date().toISOString(),
    order_reference: reference,
    action: "correct_amounts",
    currency: "USD",
    netsuite_account_id: "EXAMPLE_SB1",
    subsidiary_id: "1",
    status: "pending",
    evidence_fingerprint: `hash-${i}`,
    valid_until: new Date(Date.now() + 60000).toISOString(),
    before_json: { total: "123456789012345.123456" },
    after_json: { total: "123456789012345.123457" },
  })) as TransactionProposal[];
  render(<BulkProposals proposals={proposals} />);
  for (const p of proposals)
    fireEvent.click(screen.getByLabelText(`Select fix ${p.order_reference}`));
  fireEvent.click(
    screen.getByRole("button", { name: "Review selected fixes (2)" }),
  );
  expect(mocks.decide).not.toHaveBeenCalled();
  expect(screen.getAllByText("123456789012345.123457")).toHaveLength(2);
  const approve = screen.getByRole("button", {
    name: "Approve 2 exact actions",
  });
  expect(approve).toBeDisabled();
  fireEvent.click(
    screen.getByLabelText(
      "I reviewed every action and its exact financial effect.",
    ),
  );
  fireEvent.click(approve);
  await waitFor(() => expect(mocks.decide).toHaveBeenCalledTimes(2));
  expect(await screen.findByRole("status")).toHaveTextContent(
    "Approval does not mean execution succeeded",
  );
});
