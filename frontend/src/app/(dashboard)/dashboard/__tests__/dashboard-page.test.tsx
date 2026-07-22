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
    title: "Report",
    status: "draft",
    version: 1,
    created_at: "2026-07-01T10:00:00Z",
    has_recipe: true,
    last_refreshed_at: "2026-07-01T10:00:00Z",
    auto_refresh: "daily",
    refresh_failure_count: 0,
    auto_refresh_paused_at: null,
    created_by: "u-1",
    dashboard_pinned_at: null,
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
  api.get.mockResolvedValue([]);
});

it("greets the user with the new sub-copy and no longer shows the placeholder stats row", async () => {
  const { findByText, queryByText } = renderPage();
  expect(await findByText(/here's where your business stands/i)).toBeTruthy();
  expect(queryByText("Integrations")).toBeNull();
  expect(queryByText("Data Synced")).toBeNull();
  expect(queryByText("--")).toBeNull();
});

it("shows the uppercase 'Pinned reports' section label", async () => {
  const { findByText } = renderPage();
  expect(await findByText("Pinned reports")).toBeTruthy();
});

it("shows the empty state when no reports are pinned", async () => {
  api.get.mockResolvedValue([report({ id: "r-1", dashboard_pinned_at: null })]);
  const { findByText } = renderPage();
  expect(
    await findByText(/no pinned reports yet.*pin to dashboard/i)
  ).toBeTruthy();
});

it("renders only pinned reports, sorted newest-first by dashboard_pinned_at", async () => {
  api.get.mockResolvedValue([
    report({ id: "old", title: "Older Pin", dashboard_pinned_at: "2026-07-10T00:00:00Z" }),
    report({ id: "unpinned", title: "Not Pinned", dashboard_pinned_at: null }),
    report({ id: "new", title: "Newer Pin", dashboard_pinned_at: "2026-07-20T00:00:00Z" }),
  ]);
  const { findByText, queryByText, container } = renderPage();
  await findByText("Newer Pin");
  await findByText("Older Pin");
  expect(queryByText("Not Pinned")).toBeNull();
  const titles = Array.from(container.querySelectorAll("h2, span")).map((n) => n.textContent);
  const newerIdx = titles.indexOf("Newer Pin");
  const olderIdx = titles.indexOf("Older Pin");
  expect(newerIdx).toBeGreaterThanOrEqual(0);
  expect(olderIdx).toBeGreaterThan(newerIdx);
});

it("Quick Access grid still renders below the pinned section", async () => {
  const { findByText } = renderPage();
  expect(await findByText("Quick Access")).toBeTruthy();
  expect(await findByText("Connections")).toBeTruthy();
});

it("fetches preview HTML for each pinned report", async () => {
  api.get.mockResolvedValue([report({ id: "r-9", dashboard_pinned_at: "2026-07-20T00:00:00Z" })]);
  renderPage();
  await waitFor(() => expect(api.getText).toHaveBeenCalledWith("/api/v1/reports/r-9/view"));
});
