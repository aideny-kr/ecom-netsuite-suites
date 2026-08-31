import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const api = vi.hoisted(() => ({ getText: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import { DashboardWall } from "@/app/(dashboard)/dashboard/dashboard-wall";
import type { ReportSummary } from "@/hooks/use-reports";
import type { DashboardTrackingInfo } from "@/hooks/use-dashboard";

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

// A report's authored width depends on whether report_html.py applied the
// `report--wide` modifier class (financial-statement pages only — see
// backend/app/services/report/report_html.py's `report_cls` assembly). The default
// mock HTML below carries no such marker, so it represents the ORDINARY (non-wide,
// 840px) case that most of these tests exercise; WIDE_REPORT_HTML is used only by the
// tests that specifically cover the 1120px statement-page path.
const WIDE_REPORT_HTML =
  '<!DOCTYPE html><html><body><div class="report report--wide"><h1>Statement</h1></div></body></html>';

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
  api.post.mockResolvedValue({ published: [baseReport], active: baseReport, active_is_fallback: false });
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
  // 420 / 840 (the non-wide authored width) = 0.5.
  capturedCallback?.([{ contentRect: { width: 420, height: 600 } }]);
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

// --- Review fix M10: center the (never-upscaled) frame in a wider container ------

it("centers the frame horizontally when the container is wider than the report's authored width", async () => {
  const { container } = renderWall();
  await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());
  // Container is 2000px wide; the frame stays capped at its native (non-wide) 840px,
  // leaving 1160px of dead space split evenly (580px) on each side instead of all on
  // the right.
  capturedCallback?.([{ contentRect: { width: 2000, height: 800 } }]);
  await waitFor(() => {
    const iframe = container.querySelector("iframe") as HTMLIFrameElement;
    expect(iframe.style.marginLeft).toBe("580px");
  });
});

it("keeps zero centering offset when the container is narrower than the report's authored width", async () => {
  const { container } = renderWall();
  await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());
  capturedCallback?.([{ contentRect: { width: 420, height: 600 } }]);
  await waitFor(() => {
    const iframe = container.querySelector("iframe") as HTMLIFrameElement;
    expect(iframe.style.transform).toBe("scale(0.5)");
    expect(iframe.style.marginLeft).toBe("0px");
  });
});

// --- Review fix M?? (T2 gate MAJOR 2): the wall must derive the report's AUTHORED
// width from the fetched HTML (840 default / 1120 only for report--wide statement
// pages — see backend/app/services/report/report_html.py) instead of assuming every
// report was authored at 1120px. Assuming 1120 for an ordinary (non-wide) report
// centers its real 840px content inside a too-wide frame with ~140px of dead
// background on each side and renders it at ~75% of the intended fill. ------------

it("uses the authored 840px width for a non-wide report (not a hardcoded 1120)", async () => {
  const { container } = renderWall();
  await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());
  const iframe = container.querySelector("iframe") as HTMLIFrameElement;
  expect(iframe.style.width).toBe("840px");
});

it("uses the authored 1120px width for a report whose HTML carries the report--wide class", async () => {
  api.getText.mockResolvedValueOnce(WIDE_REPORT_HTML);
  const { container } = renderWall();
  await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());
  const iframe = container.querySelector("iframe") as HTMLIFrameElement;
  expect(iframe.style.width).toBe("1120px");
});

it("fills an 840px container for a non-wide report (scale=1, no dead margin) instead of the ~75% underfill", async () => {
  const { container } = renderWall();
  await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());
  capturedCallback?.([{ contentRect: { width: 840, height: 600 } }]);
  await waitFor(() => {
    const iframe = container.querySelector("iframe") as HTMLIFrameElement;
    expect(iframe.style.transform).toBe("scale(1)");
    expect(iframe.style.marginLeft).toBe("0px");
  });
});

it("never scales a wide (statement) report up past 1:1 either", async () => {
  api.getText.mockResolvedValueOnce(WIDE_REPORT_HTML);
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

it("centers a wide (statement) report's frame using 1120px as the denominator", async () => {
  api.getText.mockResolvedValueOnce(WIDE_REPORT_HTML);
  const { container } = renderWall();
  await waitFor(() => expect(container.querySelector("iframe")).toBeTruthy());
  capturedCallback?.([{ contentRect: { width: 2000, height: 800 } }]);
  await waitFor(() => {
    const iframe = container.querySelector("iframe") as HTMLIFrameElement;
    expect(iframe.style.marginLeft).toBe("440px"); // (2000 - 1120) / 2
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

it("dismissing the fallback notice hides it locally", async () => {
  const { findByRole, queryByText } = renderWall({ activeIsFallback: true });
  const dismissBtn = await findByRole("button", { name: /dismiss/i });
  fireEvent.click(dismissBtn);
  await waitFor(() => expect(queryByText(/no longer available/i)).toBeNull());
});

// --- Round-3 T2-gate fix: GET no longer consumes the tombstone as a read side
// effect (a visit to /reports sharing the same ["dashboard"] query key must not
// silently rob /dashboard of the notice) — so dismissal must now persist via an
// explicit call, not rely on the next GET to self-heal.

it("dismissing the fallback notice also POSTs /dashboard/notice/dismiss so the dismissal persists server-side", async () => {
  const { findByRole } = renderWall({ activeIsFallback: true });
  const dismissBtn = await findByRole("button", { name: /dismiss/i });
  fireEvent.click(dismissBtn);
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/dashboard/notice/dismiss"));
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

// --- Rolling-period Stage 1 (Task 5): the tracking ribbon (mock §3) ----------------

function tracking(over: Partial<DashboardTrackingInfo>): DashboardTrackingInfo {
  return {
    series_id: "s-1",
    playbook_key: "income_statement",
    period: "Jun 2026",
    period_check_ok: true,
    resolved_period: "Jun 2026",
    next_open_period: "Jul 2026",
    ...over,
  };
}

it("shows no ribbon and no TRACKING pill for a plain snapshot selection (activeTracking omitted)", async () => {
  const { queryByText } = renderWall();
  await waitFor(() => expect(api.getText).toHaveBeenCalled());
  expect(queryByText(/last closed period/i)).toBeNull();
  expect(queryByText(/couldn.t reach netsuite/i)).toBeNull();
  expect(queryByText("TRACKING")).toBeNull();
});

it("shows the green ribbon (caught up) verbatim, plus a TRACKING pill, when resolved_period matches the active report's period", async () => {
  const { findByText } = renderWall({ activeTracking: tracking({}) });
  expect(await findByText("Last closed period · Jun 2026")).toBeTruthy();
  expect(
    await findByText("— Jul 2026 is still open in NetSuite. This wall moves to July the day it closes.")
  ).toBeTruthy();
  expect(await findByText("TRACKING")).toBeTruthy();
});

it("green ribbon omits the still-open clause when there's no later open period to name", async () => {
  const { findByText, queryByText } = renderWall({
    activeTracking: tracking({ next_open_period: null }),
  });
  expect(await findByText("Last closed period · Jun 2026")).toBeTruthy();
  expect(queryByText(/is still open in NetSuite/i)).toBeNull();
});

it("shows the grey ribbon (can't tell) verbatim, dated from the report's own freshness stamp, when the live check degraded", async () => {
  const { findByText } = renderWall({
    activeTracking: tracking({ period_check_ok: false, resolved_period: null, next_open_period: null }),
  });
  // baseReport.last_refreshed_at = "2026-07-24T07:04:00Z" -> "Jul 24".
  expect(
    await findByText("Couldn't reach NetSuite to check the close — showing Jun 2026 from Jul 24.")
  ).toBeTruthy();
});

it("grey ribbon falls back to the report's created_at when it has never been refreshed", async () => {
  const { findByText } = renderWall({
    report: { ...baseReport, last_refreshed_at: null }, // created_at = "2026-07-01T10:00:00Z" -> "Jul 1"
    activeTracking: tracking({ period_check_ok: false, resolved_period: null, next_open_period: null }),
  });
  expect(
    await findByText("Couldn't reach NetSuite to check the close — showing Jun 2026 from Jul 1.")
  ).toBeTruthy();
});

// Forward-compat only: Stage 1's backend never sends `closed_days_ago` (see the
// DashboardTrackingInfo docstring in use-dashboard.ts) — this proves the ribbon CAN
// render the amber copy the day a real backend starts reporting it, without claiming
// today's traffic can ever produce it.
it("renders the amber (building) copy verbatim when the API reports the forward-compat closed_days_ago shape", async () => {
  const { findByText } = renderWall({
    activeTracking: tracking({
      period: "Jun 2026",
      resolved_period: "Jul 2026",
      next_open_period: "Aug 2026",
      closed_days_ago: 2,
    }),
  });
  expect(
    await findByText("Jul 2026 ended 2 days ago — July's statement is scheduled and will appear within a day.")
  ).toBeTruthy();
});

it("amber copy pluralizes singular days correctly", async () => {
  const { findByText } = renderWall({
    activeTracking: tracking({
      period: "Jun 2026",
      resolved_period: "Jul 2026",
      closed_days_ago: 1,
    }),
  });
  expect(
    await findByText("Jul 2026 ended 1 day ago — July's statement is scheduled and will appear within a day.")
  ).toBeTruthy();
});

it("renders no ribbon (rather than a misleading green or a false 'couldn't reach') when the check succeeded but found a newer period than the active report's own, and there's no amber data yet", async () => {
  const { queryByText } = renderWall({
    activeTracking: tracking({ period: "Jun 2026", resolved_period: "Jul 2026", next_open_period: "Aug 2026" }),
  });
  await waitFor(() => expect(api.getText).toHaveBeenCalled());
  expect(queryByText(/last closed period/i)).toBeNull();
  expect(queryByText(/couldn.t reach netsuite/i)).toBeNull();
  expect(queryByText(/closed.*days? ago/i)).toBeNull();
  // The TRACKING pill still shows — this IS a tracking selection, just with no ribbon
  // copy for this particular in-between state yet.
  expect(await queryByText("TRACKING")).toBeTruthy();
});

it("defensively shows no ribbon when tracking is present but has no period yet (an empty series — shouldn't reach DashboardWall in practice, but must not crash if it does)", async () => {
  const { queryByText } = renderWall({
    activeTracking: tracking({ period: null, period_check_ok: false, resolved_period: null, next_open_period: null }),
  });
  await waitFor(() => expect(api.getText).toHaveBeenCalled());
  expect(queryByText(/last closed period/i)).toBeNull();
  expect(queryByText(/couldn.t reach netsuite/i)).toBeNull();
});
