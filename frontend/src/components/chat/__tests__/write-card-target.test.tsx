/**
 * The confirmation card names WHERE the write lands.
 *
 * A tenant can hold both a sandbox and a production NetSuite connector. The
 * operator approving this card is the last gate; without the target they are
 * approving a real ERP write with no way to tell which books it hits — the
 * blank-cheque defect one level up.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { WriteConfirmationCard } from "@/components/chat/write-confirmation-card";
import type { WriteConfirmationData } from "@/lib/types";

const base = {
  type: "write_confirmation",
  mutation_type: "create",
  record_type: "customer",
  record_id: null,
  proposed_fields: { companyName: "Acme" },
  proposed_lines: [],
  current_record: null,
  tool_name: "ext__aaaa__ns_createRecord",
  tool_input: {},
  confirmation_token: "t",
  status: "pending",
  editable_slots: [],
  unvalidated: false,
} as unknown as WriteConfirmationData;

const noop = () => {};

describe("write confirmation card — target", () => {
  it("shows PRODUCTION and the account", () => {
    render(
      <WriteConfirmationCard
        data={{ ...base, target_environment: "PRODUCTION", target_account: "6738075" }}
        onConfirm={noop}
        onReject={noop}
      />,
    );
    expect(screen.getByText("PRODUCTION")).toBeInTheDocument();
    expect(screen.getByText("6738075")).toBeInTheDocument();
  });

  it("shows SANDBOX for a sandbox account", () => {
    render(
      <WriteConfirmationCard
        data={{ ...base, target_environment: "SANDBOX", target_account: "6738075_SB1" }}
        onConfirm={noop}
        onReject={noop}
      />,
    );
    expect(screen.getByText("SANDBOX")).toBeInTheDocument();
    expect(screen.getByText("6738075_SB1")).toBeInTheDocument();
  });

  it("renders nothing when the target is unknown, rather than guessing", () => {
    render(<WriteConfirmationCard data={base} onConfirm={noop} onReject={noop} />);
    expect(screen.queryByText("PRODUCTION")).not.toBeInTheDocument();
    expect(screen.queryByText("SANDBOX")).not.toBeInTheDocument();
  });
});
