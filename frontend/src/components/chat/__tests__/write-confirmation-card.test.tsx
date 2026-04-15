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
});
