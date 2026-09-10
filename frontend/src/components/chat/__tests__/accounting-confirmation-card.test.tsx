import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import type { WriteConfirmationData } from "@/lib/types";
import { WriteConfirmationCard } from "../write-confirmation-card";

const card: WriteConfirmationData = {
  type: "write_confirmation",
  mutation_type: "update",
  record_type: "invoice",
  record_id: "20",
  proposed_fields: { taxRate: 5 },
  current_record: { taxRate: 4 },
  tool_name: "native_update",
  tool_input: { recordId: "20", data: '{"taxRate":5}' },
  confirmation_token: "signed",
  status: "pending",
  target_account: "123",
  target_environment: "PRODUCTION",
  accounting_review: {
    order_reference: "R123",
    record_id: "20",
    case_id: "case",
    before: {
      total: "104.00",
      taxTotal: "4.00",
      taxRate: 4,
      currency_code: "USD",
      tranId: "INV20",
      subsidiary: { refName: "Example" },
      postingPeriod: { refName: "August" },
    },
    expected_after: { total: "105.00", taxTotal: "5.00" },
    proposed_fields: { taxRate: 5 },
    scope: { netsuite_account_id: "123", subsidiary_id: "1" },
    period: { arLocked: true, allLocked: true },
    tax_item: { taxAgency: { refName: "Existing agency" } },
    tax_account: "210",
    ar_account: "119",
    accounting_book: "1",
    approval_basis:
      "Keep the approved source basis and existing tax classification.",
  },
};

describe("Accounting review", () => {
  it("shows financial impact and material conditions without modifying the signed request", () => {
    const before = JSON.stringify(card);
    const approve = vi.fn();
    render(
      <WriteConfirmationCard
        data={card}
        onConfirm={approve}
        onReject={vi.fn()}
      />,
    );
    expect(screen.getByText("$105.00")).toBeVisible();
    expect(screen.getByText(/same|one difference/)).toBeVisible();
    expect(screen.getByText(/locked, but not closed/)).toBeVisible();
    expect(
      screen.getByText(/Their jurisdictional classification/),
    ).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Approve invoice correction" }),
    );
    expect(approve).toHaveBeenCalledWith({});
    expect(JSON.stringify(card)).toBe(before);
  });
  it("does not equate an approval or receipt with verified accounting", () => {
    render(
      <WriteConfirmationCard
        data={{
          ...card,
          status: "approved",
          accounting_verification: { status: "needs_review" },
        }}
        onConfirm={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(
      screen.getAllByText("Executed · verification needed")[0],
    ).toBeVisible();
    expect(
      screen.queryByText("✓ Invoice and GL verified"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Approve/ }),
    ).not.toBeInTheDocument();
  });
  it("shows actual verified amounts and retains the separate settlement scope", () => {
    render(
      <WriteConfirmationCard
        data={{
          ...card,
          status: "approved",
          accounting_verification: {
            status: "verified",
            invoice: {
              total: "105.00",
              taxTotal: "5.00",
              amountRemaining: "0",
            },
          },
        }}
        onConfirm={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(screen.getByText("Executed · verified")).toBeVisible();
    expect(screen.getByText(/Invoice balance: \$0.00/)).toBeVisible();
    expect(
      screen.getByText(/Sales-order reconciliation and deposit/),
    ).toBeVisible();
    expect(screen.queryByText(/Nothing has been sent/)).not.toBeInTheDocument();
  });
  it("preserves proven invariant blocks", () => {
    render(
      <WriteConfirmationCard
        data={{ ...card, invariant_errors: ["Posting period closed"] }}
        onConfirm={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Approve invoice correction" }),
    ).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Posting period closed",
    );
  });
  it("requires review of the frozen group and does not approve unsupported cases", () => {
    const approve = vi.fn();
    const group: WriteConfirmationData = {
      ...card,
      accounting_review: null,
      accounting_group: {
        group_id: "group",
        concurrency: 3,
        members: [
          {
            case_id: "case",
            order_reference: "R123",
            confirmation_id: "child",
            card,
          },
          {
            case_id: "other",
            order_reference: "R999",
            reason: "Unsupported treatment",
          },
        ],
      },
    };
    render(
      <WriteConfirmationCard
        data={group}
        onConfirm={approve}
        onReject={vi.fn()}
      />,
    );
    const button = screen.getByRole("button", {
      name: "Approve 1 invoice corrections",
    });
    expect(button).toBeDisabled();
    expect(
      screen.getByText(
        /2 orders reviewed · 1 exact corrections · 1 need individual/,
      ),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(button);
    expect(approve).toHaveBeenCalledOnce();
    expect(approve).toHaveBeenCalledWith({});
  });
  it("does not ask for approval when a group has no supported correction", () => {
    const group: WriteConfirmationData = {
      ...card,
      accounting_review: null,
      invariant_errors: ["No supported corrections are ready for approval."],
      accounting_group: {
        group_id: "group",
        concurrency: 3,
        members: [
          {
            case_id: "other",
            order_reference: "R999",
            reason: "Order identity requires verification",
          },
        ],
      },
    };
    render(
      <WriteConfirmationCard
        data={group}
        onConfirm={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /Approve/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Awaiting approval")).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Investigate this order →" }),
    ).toHaveAttribute("href", expect.stringContaining("case+other"));
  });
});
