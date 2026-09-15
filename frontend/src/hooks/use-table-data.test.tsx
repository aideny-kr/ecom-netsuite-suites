import { expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { apiClient } from "@/lib/api-client";
import { useTableData } from "./use-table-data";

const auth = vi.hoisted(() => ({ user: { tenant_id: "tenant-a" } }));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => auth }));
vi.mock("@/lib/api-client", () => ({ apiClient: { get: vi.fn() } }));

it("never displays the previous tenant's cached rows while switching tenants", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  vi.mocked(apiClient.get).mockResolvedValueOnce({
    items: [{ order_number: "TENANT_A_PRIVATE" }],
    total: 1,
    pages: 1,
  });
  const { result, rerender, unmount } = renderHook(
    () => useTableData({ tableName: "orders" }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.data?.items).toHaveLength(1));
  vi.mocked(apiClient.get).mockImplementationOnce(() => new Promise(() => {}));
  auth.user = { tenant_id: "tenant-b" };
  rerender();
  expect(result.current.data?.items).toBeUndefined();
  unmount();
  client.clear();
});

it.each(["source", "search", "table"])(
  "does not retain unrelated rows when changing %s",
  async (change) => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    vi.mocked(apiClient.get).mockResolvedValueOnce({
      items: [{ id: "previous" }],
      total: 1,
      pages: 1,
    });
    const initialProps = {
      tableName: "orders",
      search: "R123",
      filters: { source_connection_id: "source-a" },
      page: 1,
    };
    const { result, rerender, unmount } = renderHook(
      (params) => useTableData(params),
      { wrapper, initialProps },
    );
    await waitFor(() => expect(result.current.data?.items).toHaveLength(1));
    vi.mocked(apiClient.get).mockImplementation(() => new Promise(() => {}));
    rerender({ ...initialProps, page: 2 });
    expect(result.current.data?.items).toHaveLength(1);
    rerender({
      ...initialProps,
      ...(change === "source"
        ? { filters: { source_connection_id: "source-b" } }
        : change === "search"
          ? { search: "R456" }
          : { tableName: "refunds" }),
    });
    expect(result.current.data).toBeUndefined();
    unmount();
    client.clear();
  },
);
