import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ getText: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import { DashboardWall } from "@/app/(dashboard)/dashboard/dashboard-wall";
import type { ReportSummary } from "@/hooks/use-reports";

const baseReport: ReportSummary = {
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
  created_by: "creator-1",
  dashboard_pinned_at: "2026-07-22T09:00:00Z",
};

type ROCallback = (entries: Array<{ contentRect: { width: number; height: number } }>) => void;

let capturedCallback: ROCallback | null = null;

class CapturingResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
  constructor(cb: ROCallback) {
    capturedCallback = cb;
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  capturedCallback = null;
  (URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL = vi.fn(() => "blob:test");
  (URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL = vi.fn();
  (globalThis as unknown as { ResizeObserver: typeof CapturingResizeObserver }).ResizeObserver =
    CapturingResizeObserver;
  api.getText.mockResolvedValue("<!DOCTYPE html><html><body>REPORT</body></html>");
});

afterEach(() => {
  vi.restoreAllMocks();
});

it("renders the header row with title, freshness chip, and an Open ↗ link to the report", async () => {
  const { findByText, getAllByRole } = render(<DashboardWall report={baseReport} />);
  expect(await findByText(baseReport.title)).toBeTruthy();
  expect(await findByText(/refreshed daily/i)).toBeTruthy();
  const links = getAllByRole("link");
  expect(links.some((l) => l.getAttribute("href") === "/reports/r-1")).toBe(true);
  expect(await findByText("Open ↗")).toBeTruthy();
});

it("fetches the frozen HTML and renders it in a fully sandboxed iframe", async () => {
  const { container } = render(<DashboardWall report={baseReport} />);
  await waitFor(() => expect(api.getText).toHaveBeenCalledWith("/api/v1/reports/r-1/view"));
  const iframe = await waitFor(() => {
    const el = container.querySelector("iframe");
    if (!el) throw new Error("no iframe yet");
    return el;
  });
  expect(iframe.getAttribute("sandbox")).toBe("");
  expect(iframe.getAttribute("title")).toBe(baseReport.title);
});

it("revokes the object URL on unmount", async () => {
  const { unmount } = render(<DashboardWall report={baseReport} />);
  await waitFor(() => expect(api.getText).toHaveBeenCalled());
  unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
});

it("revokes the previous blob and refetches when the displayed report changes (a switch)", async () => {
  (URL.createObjectURL as ReturnType<typeof vi.fn>)
    .mockReturnValueOnce("blob:one")
    .mockReturnValueOnce("blob:two");
  const { rerender } = render(<DashboardWall report={baseReport} />);
  await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(1));

  rerender(
    <DashboardWall
      report={{ ...baseReport, id: "r-2", version: 1, last_refreshed_at: "2026-07-24T08:00:00Z" }}
    />
  );
  await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(2));
  expect(api.getText).toHaveBeenLastCalledWith("/api/v1/reports/r-2/view");
  await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:one"));
});

it("fits the report down on a narrow container (scale < 1)", async () => {
  const { container } = render(<DashboardWall report={baseReport} />);
  await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());
  capturedCallback?.([{ contentRect: { width: 560, height: 600 } }]);
  await waitFor(() => {
    const iframe = container.querySelector("iframe") as HTMLIFrameElement;
    expect(iframe.style.transform).toBe("scale(0.5)");
  });
});

it("never scales up past 1:1 even when the container is wider than the report's authored width", async () => {
  const { container } = render(<DashboardWall report={baseReport} />);
  await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());
  capturedCallback?.([{ contentRect: { width: 2000, height: 800 } }]);
  await waitFor(() => {
    const iframe = container.querySelector("iframe") as HTMLIFrameElement;
    const match = iframe.style.transform.match(/scale\(([\d.]+)\)/);
    const scale = match ? parseFloat(match[1]) : 1;
    expect(scale).toBeLessThanOrEqual(1);
  });
});

it("shows a quiet 'Preview unavailable' fallback on fetch failure, header link still works", async () => {
  api.getText.mockRejectedValue(new Error("not found"));
  const { findByText, findByRole } = render(<DashboardWall report={baseReport} />);
  expect(await findByText(/preview unavailable/i)).toBeTruthy();
  expect(await findByRole("link", { name: new RegExp(baseReport.title) })).toBeTruthy();
});

it("shows the healthy freshness chip (green) for an auto-refreshing report", async () => {
  const { findByText } = render(<DashboardWall report={baseReport} />);
  expect(await findByText(/refreshed daily/i)).toBeTruthy();
});

it("shows the failing freshness chip (amber) when refresh_failure_count > 0", async () => {
  const { findByText } = render(
    <DashboardWall report={{ ...baseReport, refresh_failure_count: 3 }} />
  );
  expect(await findByText(/refresh failing/i)).toBeTruthy();
});

it("shows the failing freshness chip (amber) when auto_refresh_paused_at is set", async () => {
  const { findByText } = render(
    <DashboardWall report={{ ...baseReport, auto_refresh_paused_at: "2026-07-24T07:00:00Z" }} />
  );
  expect(await findByText(/refresh failing/i)).toBeTruthy();
});

it("shows a plain Snapshot chip for a non-recipe report", async () => {
  const { findByText } = render(
    <DashboardWall report={{ ...baseReport, has_recipe: false, auto_refresh: undefined }} />
  );
  expect(await findByText(/^snapshot/i)).toBeTruthy();
});

it("shows a plain Snapshot chip when auto_refresh is off", async () => {
  const { findByText } = render(<DashboardWall report={{ ...baseReport, auto_refresh: "off" }} />);
  expect(await findByText(/^snapshot/i)).toBeTruthy();
});

it("renders an optional subtitle slot beneath the header row, above the display", async () => {
  const { container, findByText } = render(
    <DashboardWall report={baseReport} subtitle={<p>Welcome back, Aiden</p>} />
  );
  await findByText("Welcome back, Aiden");
  const whead = container.firstElementChild as HTMLElement;
  const headerRow = whead.children[0];
  const subtitleWrap = whead.children[1];
  const display = whead.children[2];
  expect(headerRow.textContent).toContain(baseReport.title);
  expect(subtitleWrap.textContent).toBe("Welcome back, Aiden");
  expect(display.className).toContain("overflow-hidden");
});

it("refetches when the report advances to a new version (auto-refresh) with the same id", async () => {
  const { rerender } = render(<DashboardWall report={baseReport} />);
  await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(1));
  rerender(
    <DashboardWall report={{ ...baseReport, version: 5, last_refreshed_at: "2026-07-24T09:00:00Z" }} />
  );
  await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(2));
});
