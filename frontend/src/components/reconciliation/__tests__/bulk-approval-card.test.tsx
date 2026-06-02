import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { BulkApprovalCard } from "@/components/reconciliation/bulk-approval-card";

const base = {
  bucketLabel: "Matches",
  count: 2,
  totalVariance: 9.24,
  currency: "USD",
  isApproving: false,
};

describe("BulkApprovalCard", () => {
  it("shows the count, variance, and per-line audit notice", () => {
    render(<BulkApprovalCard {...base} onApprove={vi.fn()} />);
    expect(screen.getByText(/2 lines/i)).toBeInTheDocument();
    expect(screen.getByText(/one audit record per line/i)).toBeInTheDocument();
  });

  it("calls onApprove when the button is clicked", () => {
    const onApprove = vi.fn();
    render(<BulkApprovalCard {...base} onApprove={onApprove} />);
    fireEvent.click(screen.getByRole("button", { name: /approve all/i }));
    expect(onApprove).toHaveBeenCalledOnce();
  });

  it("disables the button when disabled or count is 0", () => {
    const { rerender } = render(<BulkApprovalCard {...base} disabled onApprove={vi.fn()} />);
    expect(screen.getByRole("button", { name: /approve all/i })).toBeDisabled();
    rerender(<BulkApprovalCard {...base} count={0} onApprove={vi.fn()} />);
    expect(screen.getByRole("button", { name: /approve all/i })).toBeDisabled();
  });
});
