import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ProposalCard } from "./proposal-card";
import type { TransactionProposal } from "./types";
const mocks = vi.hoisted(() => ({
  mutate: vi.fn(),
  operation: null as null | {
    status: string;
    result_json: object;
    attempted_at: string;
    completed_at: null;
  },
  operationError: null,
}));
vi.mock("@/hooks/use-transaction-ops", () => ({
  useRecheckTransactionOperation: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useTransactionRun: () => ({ data: undefined }),
  useTransactionDecision: () => ({
    mutateAsync: mocks.mutate,
    isPending: false,
  }),
  useTransactionOperation: () => ({
    data: mocks.operation,
    error: mocks.operationError,
    isLoading: false,
  }),
}));
const proposal: TransactionProposal = {
  id: "p",
  tenant_id: "tenant",
  config_id: "config",
  run_id: "run",
  work_key: "work",
  source_record_id: "1",
  order_reference: "R123456789-EU",
  target_record_id: "91",
  action: "correct_amounts",
  currency: "EUR",
  netsuite_account_id: "EXAMPLE_SB1",
  subsidiary_id: "4",
  record_type: "salesorder",
  evidence_fingerprint: "a".repeat(64),
  observed_at: new Date().toISOString(),
  valid_until: new Date(Date.now() + 600000).toISOString(),
  before_json: { total: "9999999999999999.000" },
  after_json: { total: "9999999999999999.010" },
  evidence_json: { lookup: { complete: true } },
  status: "pending",
  decided_by: null,
  decided_at: null,
  decision_note: null,
  created_at: new Date().toISOString(),
};
beforeEach(() => {
  vi.clearAllMocks();
  mocks.operation = null;
  mocks.mutate.mockResolvedValue({ status: "approved" });
});
describe("immutable proposal review", () => {
  it("shows exact before and after without amount-edit fields and requires a concrete confirmation", async () => {
    render(<ProposalCard proposal={proposal} />);
    expect(screen.getByText("9999999999999999.000")).toBeInTheDocument();
    expect(screen.getByText("9999999999999999.010")).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review approval" }));
    expect(mocks.mutate).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toHaveTextContent("EXAMPLE_SB1");
    fireEvent.click(
      screen.getByRole("button", { name: "Approve this proposal" }),
    );
    await waitFor(() =>
      expect(mocks.mutate).toHaveBeenCalledWith({
        id: "p",
        decision: "approve",
        evidence_fingerprint: "a".repeat(64),
        note: undefined,
      }),
    );
  });
  it("requires refresh/new investigation for expired evidence and never offers approval", () => {
    render(
      <ProposalCard
        proposal={{ ...proposal, valid_until: "2020-01-01T00:00:00Z" }}
      />,
    );
    expect(screen.getByText("Evidence expired")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Review approval" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/new investigation/i)).toBeInTheDocument();
  });
  it("will not submit if the proposal changed while the confirmation was open", async () => {
    const { rerender } = render(<ProposalCard proposal={proposal} />);
    fireEvent.click(screen.getByRole("button", { name: "Review approval" }));
    rerender(<ProposalCard proposal={{ ...proposal, status: "superseded" }} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Approve this proposal" }),
    );
    expect(mocks.mutate).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toHaveTextContent(/changed/i);
  });
  it("shows an unknown external outcome as unsettled and offers no retry", () => {
    mocks.operation = {
      status: "unknown",
      result_json: { code: "timeout" },
      attempted_at: proposal.observed_at,
      completed_at: null,
    };
    render(<ProposalCard proposal={{ ...proposal, status: "approved" }} />);
    expect(screen.getByText("Execution outcome unknown")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Recheck outcome" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/external outcome must be reconciled/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /retry|approve/i }),
    ).not.toBeInTheDocument();
  });
  it("preserves the fixed rejection decision and surfaces an unconfirmed request", async () => {
    mocks.mutate.mockRejectedValue(new Error("network"));
    render(<ProposalCard proposal={proposal} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject proposal" }));
    fireEvent.change(screen.getByLabelText("Review note (optional)"), {
      target: { value: "Wrong scope" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm rejection" }));
    await waitFor(() =>
      expect(screen.getByRole("alertdialog")).toHaveTextContent(
        /could not be confirmed/,
      ),
    );
    expect(mocks.mutate).toHaveBeenCalledWith({
      id: "p",
      decision: "reject",
      evidence_fingerprint: proposal.evidence_fingerprint,
      note: "Wrong scope",
    });
  });
});

it("allows authenticated rejection of expired pending evidence without enabling approval", async () => {
  render(
    <ProposalCard
      proposal={{ ...proposal, valid_until: "2020-01-01T00:00:00Z" }}
    />,
  );
  expect(
    screen.queryByRole("button", { name: "Review approval" }),
  ).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Reject proposal" }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm rejection" }));
  await waitFor(() =>
    expect(mocks.mutate).toHaveBeenCalledWith({
      id: proposal.id,
      decision: "reject",
      evidence_fingerprint: proposal.evidence_fingerprint,
      note: undefined,
    }),
  );
});

it("keeps the source assessment limitation in the immutable approval even when the live card changes", () => {
  const assessed = {
    ...proposal,
    evidence_json: {
      report: {
        source: { tax_details: [{ calculation: "source_assessment" }] },
        comparison: { findings: [], differences: [] },
      },
    },
  };
  const view = render(<ProposalCard proposal={assessed} />);
  expect(
    screen.getByText(/statutory rates are not independently verified/i),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Review approval" }));
  expect(screen.getByRole("alertdialog")).toHaveTextContent(
    /statutory rates are not independently verified/i,
  );
  view.rerender(
    <ProposalCard
      proposal={{ ...proposal, evidence_fingerprint: "b".repeat(64) }}
    />,
  );
  expect(screen.getByRole("alertdialog")).toHaveTextContent(
    /statutory rates are not independently verified/i,
  );
});
