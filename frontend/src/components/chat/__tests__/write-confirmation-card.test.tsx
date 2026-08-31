import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { WriteConfirmationCard } from "../write-confirmation-card";
import type { WriteConfirmationData } from "@/lib/types";

const baseCreate: WriteConfirmationData = {
  type: "write_confirmation",
  mutation_type: "create",
  record_type: "Sales Order",
  record_id: null,
  proposed_fields: {
    customer: "ACME Corp",
    amount: 1500,
    currency: "USD",
  },
  current_record: null,
  tool_name: "ns_createRecord",
  tool_input: { type: "salesorder" },
  confirmation_token: "tok_abc123",
  status: "pending",
};

const baseUpdate: WriteConfirmationData = {
  type: "write_confirmation",
  mutation_type: "update",
  record_type: "Customer",
  record_id: "12345",
  proposed_fields: {
    phone: "555-9999",
    email: "new@example.com",
  },
  current_record: {
    phone: "555-1234",
    email: "old@example.com",
    id: "12345",
  },
  tool_name: "ns_updateRecord",
  tool_input: { type: "customer", id: "12345" },
  confirmation_token: "tok_def456",
  status: "pending",
};

describe("WriteConfirmationCard", () => {
  it("renders mutation type and record type in the header", () => {
    render(<WriteConfirmationCard data={baseCreate} onConfirm={() => {}} onReject={() => {}} />);
    expect(screen.getByText(/create/i)).toBeInTheDocument();
    expect(screen.getByText(/sales order/i)).toBeInTheDocument();
  });

  it("shows proposed fields for creates", () => {
    render(<WriteConfirmationCard data={baseCreate} onConfirm={() => {}} onReject={() => {}} />);
    expect(screen.getByText("customer")).toBeInTheDocument();
    expect(screen.getByText("ACME Corp")).toBeInTheDocument();
    expect(screen.getByText("amount")).toBeInTheDocument();
    expect(screen.getByText("1500")).toBeInTheDocument();
  });

  it("shows before/after diff for updates with current_record", () => {
    render(<WriteConfirmationCard data={baseUpdate} onConfirm={() => {}} onReject={() => {}} />);
    // Old values should be present
    expect(screen.getByText("555-1234")).toBeInTheDocument();
    expect(screen.getByText("old@example.com")).toBeInTheDocument();
    // New values should be present
    expect(screen.getByText("555-9999")).toBeInTheDocument();
    expect(screen.getByText("new@example.com")).toBeInTheDocument();
  });

  it("filters out 'id' and 'type' from display", () => {
    const dataWithMeta: WriteConfirmationData = {
      ...baseCreate,
      proposed_fields: {
        id: "should-be-hidden",
        type: "should-be-hidden",
        customer: "Visible Corp",
      },
    };
    render(<WriteConfirmationCard data={dataWithMeta} onConfirm={() => {}} onReject={() => {}} />);
    expect(screen.getByText("customer")).toBeInTheDocument();
    expect(screen.queryByText("should-be-hidden")).not.toBeInTheDocument();
    // 'id' and 'type' as field keys should not appear
    const fieldKeys = screen.queryAllByText(/^id$|^type$/);
    expect(fieldKeys.length).toBe(0);
  });

  it("calls onConfirm when Approve button clicked", () => {
    const onConfirm = vi.fn();
    render(<WriteConfirmationCard data={baseCreate} onConfirm={onConfirm} onReject={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("calls onReject when Reject button clicked", () => {
    const onReject = vi.fn();
    render(<WriteConfirmationCard data={baseCreate} onConfirm={() => {}} onReject={onReject} />);
    fireEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it("shows 'Approved' state and hides buttons when status is approved", () => {
    const approved: WriteConfirmationData = { ...baseCreate, status: "approved" };
    render(<WriteConfirmationCard data={approved} onConfirm={() => {}} onReject={() => {}} />);
    expect(screen.getByText(/approved/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /reject/i })).not.toBeInTheDocument();
  });

  it("shows 'Cancelled' state and hides buttons when status is rejected", () => {
    const rejected: WriteConfirmationData = { ...baseCreate, status: "rejected" };
    render(<WriteConfirmationCard data={rejected} onConfirm={() => {}} onReject={() => {}} />);
    expect(screen.getByText(/cancelled/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /reject/i })).not.toBeInTheDocument();
  });

  it("disables both buttons when disabled prop is true", () => {
    render(<WriteConfirmationCard data={baseCreate} onConfirm={() => {}} onReject={() => {}} disabled />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDisabled();
  });

  it("shows an in-flight state and hides both buttons when status is executing", () => {
    // 'executing' is the atomic-claim window (the pending->executing
    // compare-and-swap won, the write is in flight against NetSuite) — a
    // card in this state must render as non-actionable: no Approve, no
    // Reject, just an in-progress treatment.
    const executing: WriteConfirmationData = { ...baseCreate, status: "executing" };
    render(<WriteConfirmationCard data={executing} onConfirm={() => {}} onReject={() => {}} />);
    expect(screen.getByText(/writing to netsuite/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /reject/i })).not.toBeInTheDocument();
  });
});

// ── Write loop states (Task 9): editable slots, failed, unvalidated,
// unfillable line fields, proposed_lines. Backend source: Tasks 4-8 of
// docs/superpowers/plans/2026-08-19-agentic-netsuite-write-loop.md.
const base: WriteConfirmationData = {
  type: "write_confirmation",
  mutation_type: "create",
  record_type: "customer",
  record_id: null,
  proposed_fields: { companyname: "test ai customer" },
  proposed_lines: [],
  current_record: null,
  tool_name: "ext__aaa__ns_createRecord",
  tool_input: {},
  confirmation_token: "t",
  editable_slots: [],
  unvalidated: false,
  status: "pending",
};

describe("WriteConfirmationCard — write loop states", () => {
  it("renders the proposed fields", () => {
    render(<WriteConfirmationCard data={base} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText("test ai customer")).toBeInTheDocument();
  });

  it("renders editable slots and blocks approve until filled", () => {
    const data: WriteConfirmationData = {
      ...base,
      editable_slots: [
        {
          name: "subsidiary",
          label: "Primary Subsidiary",
          type: "select",
          allowed: [{ value: "1", label: "Framework Inc" }],
        },
      ],
    };
    render(<WriteConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByLabelText("Primary Subsidiary")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
  });

  it("passes filled slot values to onConfirm", () => {
    const onConfirm = vi.fn();
    const data: WriteConfirmationData = {
      ...base,
      editable_slots: [
        {
          name: "subsidiary",
          label: "Primary Subsidiary",
          type: "select",
          allowed: [{ value: "1", label: "Framework Inc" }],
        },
      ],
    };
    render(<WriteConfirmationCard data={data} onConfirm={onConfirm} onReject={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Primary Subsidiary"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(onConfirm).toHaveBeenCalledWith({ subsidiary: "1" });
  });

  it("renders a free-text input for a slot with no allowed list", () => {
    const onConfirm = vi.fn();
    const data: WriteConfirmationData = {
      ...base,
      editable_slots: [{ name: "memo", label: "Memo", type: "text", allowed: null }],
    };
    render(<WriteConfirmationCard data={data} onConfirm={onConfirm} onReject={vi.fn()} />);
    const input = screen.getByLabelText("Memo");
    expect(input.tagName).toBe("INPUT");
    fireEvent.change(input, { target: { value: "revised" } });
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(onConfirm).toHaveBeenCalledWith({ memo: "revised" });
  });

  it("keeps Approve disabled when a required slot is whitespace-only", () => {
    const data: WriteConfirmationData = {
      ...base,
      editable_slots: [{ name: "memo", label: "Memo", type: "text", allowed: null }],
    };
    render(<WriteConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Memo"), { target: { value: "   " } });
    // A space is not a value — a human typing a space must not be told the
    // field is complete when it isn't.
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
  });

  it("sends a padded slot value trimmed, not verbatim", () => {
    const onConfirm = vi.fn();
    const data: WriteConfirmationData = {
      ...base,
      editable_slots: [{ name: "memo", label: "Memo", type: "text", allowed: null }],
    };
    render(<WriteConfirmationCard data={data} onConfirm={onConfirm} onReject={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Memo"), { target: { value: "  revised  " } });
    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(onConfirm).toHaveBeenCalledWith({ memo: "revised" });
  });

  it("renders the failed state with the NetSuite error and no Approve button", () => {
    const data: WriteConfirmationData = {
      ...base,
      status: "failed",
      error: "Please enter value(s) for: Primary Subsidiary.",
    };
    render(<WriteConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText(/Primary Subsidiary/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
  });

  it("warns when the payload could not be validated", () => {
    render(
      <WriteConfirmationCard data={{ ...base, unvalidated: true }} onConfirm={vi.fn()} onReject={vi.fn()} />,
    );
    expect(screen.getByText(/could not be validated/i)).toBeInTheDocument();
    // State D still allows approval — the human is the control.
    expect(screen.getByRole("button", { name: /approve/i })).toBeEnabled();
  });

  it("renders proposed line items when present", () => {
    const data: WriteConfirmationData = {
      ...base,
      proposed_lines: [
        { item: "SKU-1", quantity: 2 },
        { item: "SKU-2", quantity: 1 },
      ],
    };
    render(<WriteConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText("SKU-1")).toBeInTheDocument();
    expect(screen.getByText("SKU-2")).toBeInTheDocument();
  });

  it("makes the card terminal when unfillable_line_fields is non-empty — no slot inputs, no Approve", () => {
    const data: WriteConfirmationData = {
      ...base,
      editable_slots: [
        {
          name: "subsidiary",
          label: "Primary Subsidiary",
          type: "select",
          allowed: [{ value: "1", label: "Framework Inc" }],
        },
      ],
      unfillable_line_fields: ["rate"],
    };
    render(<WriteConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.queryByLabelText("Primary Subsidiary")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.getByText(/rate/i)).toBeInTheDocument();
    // Rejecting is still safe — nothing was submitted to NetSuite.
    expect(screen.getByRole("button", { name: /reject/i })).toBeInTheDocument();
  });

  it("recon group confirmation cards (editable_slots: []) keep rendering as before", () => {
    // build_recon_group_confirmation always sends editable_slots: [] — this
    // must NOT be mistaken for the terminal unfillable_line_fields path.
    const data: WriteConfirmationData = {
      ...base,
      editable_slots: [],
      unfillable_line_fields: [],
    };
    render(<WriteConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeEnabled();
  });

  // MF-1 (final whole-branch review): the server has PROVED the payload
  // invalid (unbalanced JE, closed period) — invariant_errors must reach the
  // human with the same terminal treatment as unfillable_line_fields, not
  // render as nothing.
  it("makes the card terminal when invariant_errors is non-empty — no slot inputs, no Approve", () => {
    const data: WriteConfirmationData = {
      ...base,
      editable_slots: [
        {
          name: "subsidiary",
          label: "Primary Subsidiary",
          type: "select",
          allowed: [{ value: "1", label: "Framework Inc" }],
        },
      ],
      invariant_errors: ["Journal entry does not balance: debits 100.00 != credits 90.00."],
    };
    render(<WriteConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText(/does not balance/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Primary Subsidiary")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    // Rejecting is still safe — nothing was submitted to NetSuite.
    expect(screen.getByRole("button", { name: /reject/i })).toBeInTheDocument();
  });

  it("renders a closed-period invariant error the same way", () => {
    const data: WriteConfirmationData = {
      ...base,
      invariant_errors: ["Accounting period 'Jan 2026' is closed — posting is not permitted."],
    };
    render(<WriteConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText(/is closed/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
  });

  it("renders BOTH invariant_errors and unfillable_line_fields when both are present — neither hides the other", () => {
    const data: WriteConfirmationData = {
      ...base,
      unfillable_line_fields: ["rate"],
      invariant_errors: ["Accounting period 'Jan 2026' is closed — posting is not permitted."],
    };
    render(<WriteConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByText(/rate/i)).toBeInTheDocument();
    expect(screen.getByText(/is closed/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reject/i })).toBeInTheDocument();
  });

  it("does not render an invariant panel when invariant_errors is absent", () => {
    render(<WriteConfirmationCard data={base} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeEnabled();
  });
});

describe("WriteConfirmationCard — indeterminate outcome", () => {
  // A write that timed out is not a write that failed. Live on 2026-08-27,
  // sandbox 6738075-sb1: ns_createRecord exceeded the timeout, NetSuite
  // created customer 5264348 anyway, and the app told the operator it had
  // failed. The failed panel says "Nothing was written" — which was untrue,
  // and is the sentence that sends someone off to create a duplicate.
  const indeterminate: WriteConfirmationData = {
    ...baseCreate,
    status: "indeterminate",
    error: "Tool execution exceeded 60-second timeout limit",
  };

  it("does not claim the write failed or that nothing was written", () => {
    render(
      <WriteConfirmationCard data={indeterminate} onConfirm={vi.fn()} onReject={vi.fn()} />,
    );
    expect(screen.queryByText(/NetSuite rejected this write/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Nothing was written/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Failed$/)).not.toBeInTheDocument();
  });

  it("tells the operator the record may already exist and to check first", () => {
    render(
      <WriteConfirmationCard data={indeterminate} onConfirm={vi.fn()} onReject={vi.fn()} />,
    );
    expect(screen.getByText(/may already exist/i)).toBeInTheDocument();
    expect(screen.getByText(/check .* in NetSuite/i)).toBeInTheDocument();
  });

  it("surfaces the underlying reason", () => {
    render(
      <WriteConfirmationCard data={indeterminate} onConfirm={vi.fn()} onReject={vi.fn()} />,
    );
    expect(screen.getByText(/60-second timeout/i)).toBeInTheDocument();
  });

  it("offers no Approve button — re-running is exactly the wrong move", () => {
    render(
      <WriteConfirmationCard data={indeterminate} onConfirm={vi.fn()} onReject={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
  });
});
