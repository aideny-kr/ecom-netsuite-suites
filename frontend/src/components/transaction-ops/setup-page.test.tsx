import React from "react";
import { beforeEach, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { TransactionSetupPage } from "./setup-page";
const mocks = vi.hoisted(() => ({
  tenant: "tenant-a",
  canManage: true,
  create: vi.fn(),
}));
vi.mock("@/hooks/use-transaction-ops", () => ({
  useTransactionAccess: () => ({
    allowed: true,
    canManage: mocks.canManage,
    tenantId: mocks.tenant,
  }),
}));
vi.mock("@/hooks/use-transaction-setup", () => ({
  useTransactionSetupOptions: () => ({
    data: {
      pages: [
        {
          source_steps: [
            {
              id: "source",
              reference_name: "Orders",
              flow_name: "EU",
              integration_name: "Commerce",
              sandbox: false,
            },
          ],
          target_steps: [],
          netsuite_connections: [
            { id: "ns", label: "NS sandbox", account_id: "6738075_SB1" },
          ],
        },
      ],
    },
    hasNextPage: false,
  }),
  useCreateTransactionConfig: () => ({
    mutateAsync: mocks.create,
    isPending: false,
  }),
}));
beforeEach(() => {
  vi.clearAllMocks();
  mocks.canManage = true;
  mocks.tenant = "tenant-a";
});
function fillRequired() {
  fireEvent.change(screen.getByLabelText("Scope name"), {
    target: { value: "EU orders" },
  });
  fireEvent.change(screen.getByLabelText("Framework source candidate"), {
    target: { value: "source" },
  });
  fireEvent.change(screen.getByLabelText("NetSuite connection"), {
    target: { value: "ns" },
  });
  fireEvent.change(screen.getByLabelText("Destination subsidiary ID"), {
    target: { value: "5" },
  });
  fireEvent.change(screen.getByLabelText("Full order reference field"), {
    target: { value: "tranid" },
  });
}
it("saves an explicit scope without silently enabling actions or a schedule", async () => {
  mocks.create.mockResolvedValue({ id: "created", name: "EU orders" });
  render(<TransactionSetupPage />);
  fillRequired();
  expect(
    screen.getByLabelText("Prepare actions for human review"),
  ).not.toBeChecked();
  expect(
    screen.getByLabelText("Enable scheduled investigations"),
  ).not.toBeChecked();
  fireEvent.click(
    screen.getByRole("button", { name: "Create investigation scope" }),
  );
  await waitFor(() => expect(mocks.create).toHaveBeenCalledOnce());
  expect(mocks.create.mock.calls[0][0]).toMatchObject({
    netsuite_account_id: "6738075_SB1",
    subsidiary_id: "5",
    schedule_enabled: false,
    mapping_json: { action_mode: "detect_only" },
  });
  expect(
    await screen.findByText("EU orders is ready for investigations."),
  ).toBeInTheDocument();
});
it("requires connection-management permission before showing configuration", () => {
  mocks.canManage = false;
  render(<TransactionSetupPage />);
  expect(screen.queryByLabelText("Scope name")).not.toBeInTheDocument();
  expect(
    screen.getByText(/connection-management permission/i),
  ).toBeInTheDocument();
});
it("discards a delayed creation response after the tenant changes", async () => {
  let done: (value: object) => void = () => {};
  mocks.create.mockImplementation(
    () =>
      new Promise((resolve) => {
        done = resolve;
      }),
  );
  const view = render(<TransactionSetupPage />);
  fillRequired();
  fireEvent.click(
    screen.getByRole("button", { name: "Create investigation scope" }),
  );
  mocks.tenant = "tenant-b";
  view.rerender(<TransactionSetupPage />);
  await act(async () => done({ id: "old", name: "Old tenant" }));
  expect(screen.queryByText(/Old tenant is ready/)).not.toBeInTheDocument();
});
