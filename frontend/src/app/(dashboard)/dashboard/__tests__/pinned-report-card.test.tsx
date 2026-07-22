import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ getText: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import { PinnedReportCard } from "@/app/(dashboard)/dashboard/pinned-report-card";
import type { ReportSummary } from "@/hooks/use-reports";

const baseReport: ReportSummary = {
  id: "r-1",
  title: "July Cash Flow",
  status: "draft",
  version: 3,
  created_at: "2026-07-01T10:00:00Z",
  has_recipe: true,
  last_refreshed_at: "2026-07-21T09:00:00Z",
  auto_refresh: "daily",
  refresh_failure_count: 0,
  auto_refresh_paused_at: null,
  created_by: "creator-1",
  dashboard_pinned_at: "2026-07-21T09:05:00Z",
};

class FakeResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

beforeEach(() => {
  vi.clearAllMocks();
  (URL as unknown as { createObjectURL: () => string }).createObjectURL = vi.fn(() => "blob:test");
  (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = vi.fn();
  (globalThis as unknown as { ResizeObserver: typeof FakeResizeObserver }).ResizeObserver =
    FakeResizeObserver;
  api.getText.mockResolvedValue("<!DOCTYPE html><html><body>REPORT</body></html>");
});

afterEach(() => {
  vi.restoreAllMocks();
});

it("renders the header with title and an Open report link to the report", async () => {
  const { findByRole, getAllByRole } = render(<PinnedReportCard report={baseReport} />);
  const links = getAllByRole("link");
  expect(links.some((l) => l.getAttribute("href") === "/reports/r-1")).toBe(true);
  expect(await findByRole("link", { name: /open report/i })).toBeTruthy();
});

it("fetches the frozen HTML and renders it in a fully sandboxed iframe", async () => {
  const { container } = render(<PinnedReportCard report={baseReport} />);
  await waitFor(() => expect(api.getText).toHaveBeenCalledWith("/api/v1/reports/r-1/view"));
  const iframe = await waitFor(() => {
    const el = container.querySelector("iframe");
    if (!el) throw new Error("no iframe yet");
    return el;
  });
  expect(iframe.getAttribute("sandbox")).toBe("");
  expect(iframe.getAttribute("title")).toBe("July Cash Flow");
});

it("revokes the object URL on unmount", async () => {
  const { unmount } = render(<PinnedReportCard report={baseReport} />);
  await waitFor(() => expect(api.getText).toHaveBeenCalled());
  unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
});

it("shows a quiet fallback when the preview fetch fails, but the header link still works", async () => {
  api.getText.mockRejectedValue(new Error("not found"));
  const { findByText, findByRole } = render(<PinnedReportCard report={baseReport} />);
  expect(await findByText(/preview unavailable/i)).toBeTruthy();
  expect(await findByRole("link", { name: /open report/i })).toBeTruthy();
});

it("shows the healthy freshness chip (green) for an auto-refreshing report", async () => {
  const { findByText } = render(<PinnedReportCard report={baseReport} />);
  expect(await findByText(/refreshed daily/i)).toBeTruthy();
});

it("shows the failing/paused freshness chip (amber) when refresh_failure_count > 0", async () => {
  const { findByText } = render(
    <PinnedReportCard report={{ ...baseReport, refresh_failure_count: 3 }} />
  );
  expect(await findByText(/refresh failing/i)).toBeTruthy();
});

it("shows the failing/paused freshness chip (amber) when auto_refresh_paused_at is set", async () => {
  const { findByText } = render(
    <PinnedReportCard report={{ ...baseReport, auto_refresh_paused_at: "2026-07-21T09:00:00Z" }} />
  );
  expect(await findByText(/refresh failing/i)).toBeTruthy();
});

it("shows a plain Snapshot chip for a non-recipe (snapshot) report", async () => {
  const { findByText } = render(
    <PinnedReportCard report={{ ...baseReport, has_recipe: false, auto_refresh: undefined }} />
  );
  expect(await findByText(/^snapshot/i)).toBeTruthy();
});

it("shows a plain Snapshot chip when auto_refresh is off", async () => {
  const { findByText } = render(
    <PinnedReportCard report={{ ...baseReport, auto_refresh: "off" }} />
  );
  expect(await findByText(/^snapshot/i)).toBeTruthy();
});
