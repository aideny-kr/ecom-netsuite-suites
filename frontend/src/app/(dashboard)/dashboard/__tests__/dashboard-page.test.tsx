import { render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const api = vi.hoisted(() => ({ get: vi.fn(), getText: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

const authState = vi.hoisted(() => ({
  user: { id: "u-1", full_name: "Jamie Rivera", roles: [] as string[] },
}));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => authState }));

import DashboardPage from "@/app/(dashboard)/dashboard/page";
import type { ReportSummary } from "@/hooks/use-reports";

class FakeResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return render(<DashboardPage />, { wrapper: Wrapper });
}

function report(over: Partial<ReportSummary>): ReportSummary {
  return {
    id: "r-1",
    title: "Income Statement — Jun 2026",
    status: "draft",
    version: 4,
    created_at: "2026-07-01T10:00:00Z",
    has_recipe: true,
    last_refreshed_at: "2026-07-24T07:04:00Z",
    auto_refresh: "daily",
    refresh_failure_count: 0,
    auto_refresh_paused_at: null,
    created_by: "u-1",
    dashboard_pinned_at: "2026-07-22T09:00:00Z",
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  (URL as unknown as { createObjectURL: () => string }).createObjectURL = vi.fn(() => "blob:test");
  (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = vi.fn();
  (globalThis as unknown as { ResizeObserver: typeof FakeResizeObserver }).ResizeObserver =
    FakeResizeObserver;
  api.getText.mockResolvedValue("<!DOCTYPE html><html><body>REPORT</body></html>");
  api.get.mockResolvedValue({ published: [], active: null, active_is_fallback: false });
});

it("renders the active report full-size on the wall, with the greeting demoted beneath the header", async () => {
  const active = report({});
  api.get.mockResolvedValue({ published: [active], active, active_is_fallback: false });
  const { findByText } = renderPage();
  expect(await findByText(active.title)).toBeTruthy();
  expect(await findByText(/welcome back, jamie/i)).toBeTruthy();
});

it("fetches the active report's frozen HTML into a fully sandboxed iframe", async () => {
  const active = report({ id: "r-9" });
  api.get.mockResolvedValue({ published: [active], active, active_is_fallback: false });
  const { container } = renderPage();
  await waitFor(() => expect(api.getText).toHaveBeenCalledWith("/api/v1/reports/r-9/view"));
  await waitFor(() => {
    const iframe = container.querySelector("iframe");
    expect(iframe?.getAttribute("sandbox")).toBe("");
  });
});

it("shows the real empty state (not the wall) when nothing is published", async () => {
  api.get.mockResolvedValue({ published: [], active: null, active_is_fallback: false });
  const { findByText, queryByText } = renderPage();
  expect(await findByText("No dashboard on the wall yet")).toBeTruthy();
  expect(queryByText("Open ↗")).toBeNull();
});

it("Quick Access grid renders below the wall when a report is active", async () => {
  const active = report({});
  api.get.mockResolvedValue({ published: [active], active, active_is_fallback: false });
  const { findByText } = renderPage();
  expect(await findByText("Quick Access")).toBeTruthy();
  expect(await findByText("Connections")).toBeTruthy();
});

it("Quick Access grid still renders when nothing is published", async () => {
  const { findByText } = renderPage();
  expect(await findByText("Quick Access")).toBeTruthy();
});

// Carried from Task 3's review: this outer isError branch (no active report AND
// the GET failed) previously had zero coverage.
it("shows a 'Couldn't load your dashboard' message when the dashboard query errors and nothing is active", async () => {
  api.get.mockRejectedValue(new Error("network down"));
  const { findByText } = renderPage();
  expect(await findByText(/couldn.t load your dashboard/i)).toBeTruthy();
  expect(await findByText(/try refreshing the page/i)).toBeTruthy();
});

it("shows a skeleton sized like the wall while the dashboard query is loading", async () => {
  let resolveGet!: (v: unknown) => void;
  api.get.mockReturnValue(
    new Promise((resolve) => {
      resolveGet = resolve;
    })
  );
  const { container, findByText } = renderPage();
  await waitFor(() => expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0));
  resolveGet({ published: [], active: null, active_is_fallback: false });
  expect(await findByText("No dashboard on the wall yet")).toBeTruthy();
});
