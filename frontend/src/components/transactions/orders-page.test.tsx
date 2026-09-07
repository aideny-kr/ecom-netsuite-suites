import React from "react";
import { beforeEach, expect, it, vi } from "vitest";
import {
  fireEvent,
  act,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { OrdersPage } from "./orders-page";

const state = vi.hoisted(() => ({
  push: vi.fn(),
  table: vi.fn(),
  fail: false,
  extraSource: false,
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: state.push }) }));
vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), getText: vi.fn() },
}));
vi.mock("@/providers/auth-provider", () => ({
  useAuth: () => ({ user: { tenant_id: "tenant-a" } }),
}));
vi.mock("@/hooks/use-permissions", () => ({
  usePermissions: () => ({ hasPermission: () => true }),
}));
vi.mock("@/hooks/use-connections", () => ({
  useConnections: () => ({
    data: [
      {
        id: "solidus-1",
        provider: "solidus",
        label: "Solidus",
        status: "active",
        metadata_json: { api_profile: "framework_sync" },
      },
      ...(state.extraSource
        ? [
            {
              id: "solidus-2",
              provider: "solidus",
              label: "Second source",
              status: "active",
              metadata_json: { api_profile: "framework_sync" },
            },
          ]
        : []),
    ],
  }),
}));
vi.mock("@/hooks/use-transaction-ops", () => ({
  useTransactionAccess: () => ({
    allowed: true,
    canManage: true,
    tenantId: "tenant-a",
  }),
  useTransactionConfigs: () => ({ data: [] }),
}));
vi.mock("@/hooks/use-table-data", () => ({
  useTableData: (args: unknown) => {
    state.table(args);
    return {
      error: state.fail ? new Error("Read failed") : null,
      refetch: vi.fn(),
      data: {
        total: 1,
        pages: 1,
        items: [
          {
            id: "order-1",
            order_number: "R100120031",
            source_connection_id: "solidus-1",
            currency: "USD",
            total_amount: "1234567890123.45",
            tax_amount: "12.34",
            source_created_at: "2026-09-01T10:00:00Z",
            reconciliation: {
              status: "incomplete",
              balance: {
                currency: "USD",
                amounts: {
                  order_total: {
                    source: "1234567890123.45",
                    target: "1234567890123.45",
                    delta: "0.00",
                  },
                  tax: { source: "12.34", target: "12.34", delta: "0.00" },
                  refunds: { source: null, target: null, delta: null },
                },
              },
            },
          },
        ],
      },
    };
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
  state.fail = false;
  state.extraSource = false;
  vi.mocked(apiClient.get).mockResolvedValue({
    status: "never_synced",
    records_imported: 0,
  });
  vi.mocked(apiClient.post).mockResolvedValue({ id: "investigation-1" });
});
function show() {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <OrdersPage />
    </QueryClientProvider>,
  );
}

it("shows exact amounts and keeps missing refunds visibly unverified", () => {
  show();
  expect(screen.getByText("1,234,567,890,123.45")).toBeInTheDocument();
  fireEvent.click(
    screen.getByRole("button", { name: "Open order R100120031" }),
  );
  const drawer = screen.getByRole("dialog");
  expect(within(drawer).getByText("Completed refunds")).toBeInTheDocument();
  expect(within(drawer).getByText(/Unknown amounts/)).toBeInTheDocument();
  expect(within(drawer).queryByText("Matched")).not.toBeInTheDocument();
});

it("sends reconciliation filters to the server and uses the exact order investigation endpoint", async () => {
  show();
  fireEvent.click(screen.getByRole("button", { name: "Needs review" }));
  expect(state.table).toHaveBeenLastCalledWith(
    expect.objectContaining({
      filters: expect.objectContaining({
        reconciliation_status: "needs_review",
      }),
    }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Open order R100120031" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Investigate order" }));
  await waitFor(() =>
    expect(state.push).toHaveBeenCalledWith(
      "/transaction-operations/runs/investigation-1",
    ),
  );
  expect(apiClient.post).toHaveBeenCalledWith(
    "/api/v1/transaction-ops/orders/order-1/investigate",
    { evaluation_key: expect.any(String) },
  );
});

it("shows a failed table request rather than an empty data message", () => {
  state.fail = true;
  show();
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Orders could not be loaded",
  );
  expect(
    screen.queryByText("Your orders will appear here"),
  ).not.toBeInTheDocument();
});

it("opens a fresh agent conversation grounded in the saved order investigation", async () => {
  show();
  fireEvent.click(
    screen.getByRole("button", { name: "Open order R100120031" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Work with agent" }));
  await waitFor(() => expect(state.push).toHaveBeenCalled());
  const url = new URL(state.push.mock.calls[0][0], "https://example.test");
  expect(url.pathname).toBe("/chat");
  expect(url.searchParams.get("new_session")).toBe("true");
  expect(url.searchParams.get("compose")).toContain("investigation-1");
  expect(url.searchParams.get("compose")).toContain("approval");
});

it("ignores a reconcile response after the user switches sources", async () => {
  state.extraSource = true;
  let resolve!: (value: unknown) => void;
  vi.mocked(apiClient.post).mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  show();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile now" }));
  await waitFor(() => expect(apiClient.post).toHaveBeenCalled());
  fireEvent.change(screen.getByLabelText("Order source"), {
    target: { value: "solidus-2" },
  });
  await act(async () => {
    resolve([
      { id: "old-source-run", config_snapshot: { name: "Old source" } },
    ]);
  });
  await waitFor(() =>
    expect(
      screen.queryByRole("link", { name: /Old source/ }),
    ).not.toBeInTheDocument(),
  );
  expect(state.push).not.toHaveBeenCalled();
});
