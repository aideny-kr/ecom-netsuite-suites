import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const api = vi.hoisted(() => ({ getText: vi.fn(), put: vi.fn() }));
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

function renderWall(props: Partial<React.ComponentProps<typeof DashboardWall>> = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return render(
    <DashboardWall report={baseReport} published={[baseReport]} {...props} />,
    { wrapper: Wrapper }
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  capturedCallback = null;
  (URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL = vi.fn(() => "blob:test");
  (URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL = vi.fn();
  (globalThis as unknown as { ResizeObserver: typeof CapturingResizeObserver }).ResizeObserver =
    CapturingResizeObserver;
  api.getText.mockResolvedValue("<!DOCTYPE html><html><body>REPORT</body></html>");
  api.put.mockResolvedValue({ published: [baseReport], active: baseReport, active_is_fallback: false });
});

it("renders the header row with title, freshness chip, and an Open ↗ link to the report", async () => {
  const { findByText, getAllByRole } = renderWall();
  expect(await findByText(baseReport.title)).toBeTruthy();
  expect(await findByText(/refreshed daily/i)).toBeTruthy();
  const links = getAllByRole("link");
  expect(links.some((l) => l.getAttribute("href") === "/reports/r-1")).toBe(true);
  expect(await findByText("Open ↗")).toBeTruthy();
});

it("fetches the frozen HTML and renders it in a fully sandboxed iframe", async () => {
  const { container } = renderWall();
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
  const { unmount } = renderWall();
  await waitFor(() => expect(api.getText).toHaveBeenCalled());
  unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test");
});

it("revokes the previous blob and refetches when the displayed report changes (a switch)", async () => {
  (URL.createObjectURL as ReturnType<typeof vi.fn>)
    .mockReturnValueOnce("blob:one")
    .mockReturnValueOnce("blob:two");
  const { rerender } = renderWall();
  await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(1));

  rerender(
    <DashboardWall
      report={{ ...baseReport, id: "r-2", version: 1, last_refreshed_at: "2026-07-24T08:00:00Z" }}
      published={[baseReport]}
    />
  );
  await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(2));
  expect(api.getText).toHaveBeenLastCalledWith("/api/v1/reports/r-2/view");
  await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:one"));
});

it("clears report A's iframe to a Skeleton immediately on switch, BEFORE report B's fetch resolves", async () => {
  const { container, rerender } = renderWall();
  await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());

  // Hold report B's fetch open so we can inspect the DOM mid-transition — the bug
  // this guards against is report A's frozen financials staying on screen under
  // report B's title/freshness chip until this promise resolves.
  let resolveNext!: (html: string) => void;
  api.getText.mockReturnValueOnce(
    new Promise((resolve) => {
      resolveNext = resolve;
    })
  );

  rerender(
    <DashboardWall
      report={{ ...baseReport, id: "r-2", version: 1, last_refreshed_at: "2026-07-24T08:00:00Z" }}
      published={[baseReport]}
    />
  );

  // Intermediate assertion — before B's fetch resolves, report A's iframe must
  // already be gone and the Skeleton must be showing.
  await waitFor(() => expect(container.querySelector("iframe")).toBeNull());
  expect(container.querySelector(".animate-pulse")).toBeTruthy();

  await act(async () => {
    resolveNext("<!DOCTYPE html><html><body>REPORT B</body></html>");
  });
  await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());
});

it("fits the report down on a narrow container (scale < 1)", async () => {
  const { container } = renderWall();
  await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());
  capturedCallback?.([{ contentRect: { width: 560, height: 600 } }]);
  await waitFor(() => {
    const iframe = container.querySelector("iframe") as HTMLIFrameElement;
    expect(iframe.style.transform).toBe("scale(0.5)");
  });
});

it("never scales up past 1:1 even when the container is wider than the report's authored width", async () => {
  const { container } = renderWall();
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
  const { findByText, findByRole } = renderWall();
  expect(await findByText(/preview unavailable/i)).toBeTruthy();
  expect(await findByRole("link", { name: new RegExp(baseReport.title) })).toBeTruthy();
});

it("shows the healthy freshness chip (green) for an auto-refreshing report", async () => {
  const { findByText } = renderWall();
  expect(await findByText(/refreshed daily/i)).toBeTruthy();
});

it("shows the failing freshness chip (amber) when refresh_failure_count > 0", async () => {
  const { findByText } = renderWall({ report: { ...baseReport, refresh_failure_count: 3 } });
  expect(await findByText(/refresh failing/i)).toBeTruthy();
});

it("shows the failing freshness chip (amber) when auto_refresh_paused_at is set", async () => {
  const { findByText } = renderWall({
    report: { ...baseReport, auto_refresh_paused_at: "2026-07-24T07:00:00Z" },
  });
  expect(await findByText(/refresh failing/i)).toBeTruthy();
});

it("shows a plain Snapshot chip for a non-recipe report", async () => {
  const { findByText } = renderWall({
    report: { ...baseReport, has_recipe: false, auto_refresh: undefined },
  });
  expect(await findByText(/^snapshot/i)).toBeTruthy();
});

it("shows a plain Snapshot chip when auto_refresh is off", async () => {
  const { findByText } = renderWall({ report: { ...baseReport, auto_refresh: "off" } });
  expect(await findByText(/^snapshot/i)).toBeTruthy();
});

it("renders an optional subtitle slot beneath the header row, above the display", async () => {
  const { container, findByText } = renderWall({ subtitle: <p>Welcome back, Aiden</p> });
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
  const { rerender } = renderWall();
  await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(1));
  rerender(
    <DashboardWall
      report={{ ...baseReport, version: 5, last_refreshed_at: "2026-07-24T09:00:00Z" }}
      published={[baseReport]}
    />
  );
  await waitFor(() => expect(api.getText).toHaveBeenCalledTimes(2));
});

// --- Task 4: switcher + fallback notice -----------------------------------

it("renders the Switch ▾ trigger in the header row, immediately left of Open ↗", async () => {
  const { container, findByRole } = renderWall();
  const switchBtn = await findByRole("button", { name: /switch/i });
  const openLink = await findByRole("link", { name: "Open ↗" });
  const headerRow = container.firstElementChild!.children[0];
  const kids = Array.from(headerRow.querySelectorAll("button, a"));
  expect(kids.indexOf(switchBtn)).toBeGreaterThanOrEqual(0);
  expect(kids.indexOf(switchBtn)).toBeLessThan(kids.indexOf(openLink));
});

it("does not show a fallback notice when activeIsFallback is false or omitted", async () => {
  const { queryByText } = renderWall();
  await waitFor(() => expect(api.getText).toHaveBeenCalled());
  expect(queryByText(/no longer available/i)).toBeNull();
});

it("shows the exact dismissible fallback notice above the wall when activeIsFallback is true", async () => {
  const { findByText } = renderWall({ activeIsFallback: true });
  expect(
    await findByText(
      "The dashboard you had chosen is no longer available — showing Income Statement — Jun 2026 instead."
    )
  ).toBeTruthy();
});

it("dismissing the fallback notice hides it (per-session component state, not persisted)", async () => {
  const { findByRole, queryByText } = renderWall({ activeIsFallback: true });
  const dismissBtn = await findByRole("button", { name: /dismiss/i });
  fireEvent.click(dismissBtn);
  await waitFor(() => expect(queryByText(/no longer available/i)).toBeNull());
});

it("resets the dismissed fallback notice when the displayed report changes, so a later distinct fallback event still shows", async () => {
  const { rerender, findByRole, findByText, queryByText } = renderWall({ activeIsFallback: true });
  const dismissBtn = await findByRole("button", { name: /dismiss/i });
  fireEvent.click(dismissBtn);
  await waitFor(() => expect(queryByText(/no longer available/i)).toBeNull());

  // The user's new pick (report B) gets deleted/unpublished by someone else —
  // a distinct fallback event that must not be swallowed by the earlier dismissal.
  rerender(
    <DashboardWall
      report={{ ...baseReport, id: "r-2", title: "Cash Flow — Q2", version: 1, last_refreshed_at: "2026-07-24T08:00:00Z" }}
      published={[baseReport]}
      activeIsFallback={true}
    />
  );

  expect(
    await findByText(
      "The dashboard you had chosen is no longer available — showing Cash Flow — Q2 instead."
    )
  ).toBeTruthy();
});
