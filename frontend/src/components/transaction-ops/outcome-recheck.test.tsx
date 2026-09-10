import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { OutcomeRecheck } from "./outcome-recheck";

const mocks = vi.hoisted(() => ({
  recheck: vi.fn(),
  status: "pending",
}));
vi.mock("@/hooks/use-transaction-ops", () => ({
  useRecheckTransactionOperation: () => ({ mutateAsync: mocks.recheck, isPending: false }),
  useTransactionRun: (id: string) => ({ data: id ? { id, status: mocks.status } : undefined }),
}));
beforeEach(() => {
  vi.clearAllMocks();
  mocks.status = "pending";
  mocks.recheck.mockResolvedValue({ id: "check-1", status: "pending" });
});
describe("read-only outcome recheck", () => {
  it("queues fresh evidence, links its history, and prevents another check until it finishes", async () => {
    const { rerender } = render(<OutcomeRecheck proposalId="p" />);
    expect(screen.getByText(/cannot send another write/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Recheck outcome" }));
    await waitFor(() => expect(screen.getByRole("link", { name: "View check" })).toHaveAttribute("href", "/transaction-operations/runs/check-1"));
    expect(screen.getByRole("button", { name: "Recheck queued" })).toBeDisabled();
    const first = mocks.recheck.mock.calls[0][0];
    expect(first).toEqual({ id: "p", evaluation_key: expect.stringMatching(/^[a-f0-9-]{36}$/) });
    mocks.status = "finished";
    rerender(<OutcomeRecheck proposalId="p" />);
    fireEvent.click(screen.getByRole("button", { name: "Recheck outcome" }));
    await waitFor(() => expect(mocks.recheck).toHaveBeenCalledTimes(2));
    expect(mocks.recheck.mock.calls[1][0].evaluation_key).not.toBe(first.evaluation_key);
  });
  it("reuses the request identity after an unconfirmed network response", async () => {
    mocks.recheck.mockRejectedValueOnce(new Error("network"));
    render(<OutcomeRecheck proposalId="p" />);
    fireEvent.click(screen.getByRole("button", { name: "Recheck outcome" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/could not be confirmed/i));
    fireEvent.click(screen.getByRole("button", { name: "Recheck outcome" }));
    await waitFor(() => expect(mocks.recheck).toHaveBeenCalledTimes(2));
    expect(mocks.recheck.mock.calls[1][0]).toEqual(mocks.recheck.mock.calls[0][0]);
  });
});
