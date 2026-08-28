import { fireEvent, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const api = vi.hoisted(() => ({ put: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import { DashboardSwitcher } from "@/app/(dashboard)/dashboard/dashboard-switcher";
import type { ReportSummary } from "@/hooks/use-reports";
import type { DashboardSeriesResponse } from "@/hooks/use-dashboard";

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
    created_by: "creator-1",
    dashboard_pinned_at: "2026-07-22T09:00:00Z",
    ...over,
  };
}

function series(over: Partial<DashboardSeriesResponse>): DashboardSeriesResponse {
  return {
    id: "s-1",
    playbook_key: "income_statement",
    period: "Jun 2026",
    report_id: "r-1",
    ...over,
  };
}

// Snapshots (individually pinned reports) — the "Pinned months" group.
const published = [
  report({ id: "r-2", title: "Cash Flow — Q2", auto_refresh: "hourly" }),
  report({ id: "r-3", title: "Board Snapshot", auto_refresh: "off" }),
];

// Tracking series — the "Tracking the close" group (mock §5).
const publishedSeries = [
  series({ id: "s-1", playbook_key: "income_statement", period: "Jun 2026", report_id: "r-1" }),
  series({ id: "s-2", playbook_key: "balance_sheet", period: "Jun 2026", report_id: "r-4" }),
];

// Exposed so tests can spy on invalidateQueries against the exact instance the
// component is wired to (each renderSwitcher() call makes a fresh one).
let lastQueryClient: QueryClient | null = null;

function renderSwitcher(props: Partial<React.ComponentProps<typeof DashboardSwitcher>> = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  lastQueryClient = qc;
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return render(
    <DashboardSwitcher
      published={published}
      activeId="r-2"
      publishedSeries={publishedSeries}
      activeSeriesId="s-1"
      {...props}
    />,
    { wrapper: Wrapper }
  );
}

// Radix's DropdownMenuTrigger opens on pointerdown, not click — jsdom needs both
// events fired in sequence (fireEvent.click() alone silently no-ops).
function openSwitcher(trigger: HTMLElement) {
  fireEvent.pointerDown(trigger, { button: 0, pointerId: 1 });
  fireEvent.click(trigger);
}

beforeEach(() => {
  vi.clearAllMocks();
  api.put.mockResolvedValue({ published, active: published[0], active_is_fallback: false });
});

it("renders a Switch ▾ trigger", async () => {
  const { findByRole } = renderSwitcher();
  expect(await findByRole("button", { name: /switch/i })).toBeTruthy();
});

// --- Rolling-period Stage 1 (Task 5): "Tracking the close" group -------------------

it("opens to a Tracking the close group, humanizing the playbook key, with a ✓ on the active series", async () => {
  const { findByRole, findByText } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  expect(await findByText("Tracking the close")).toBeTruthy();
  const activeItem = await findByRole("menuitem", { name: /income statement/i });
  expect(activeItem.textContent).toContain("✓");
  const otherItem = await findByRole("menuitem", { name: /balance sheet/i });
  expect(otherItem.textContent).not.toContain("✓");
});

it("shows the series' current period as meta on a Tracking the close item", async () => {
  const { findByRole } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const item = await findByRole("menuitem", { name: /income statement/i });
  expect(item.textContent).toContain("Jun 2026");
});

it("shows a placeholder meta for a tracking series with no report composed yet", async () => {
  const { findByRole } = renderSwitcher({
    publishedSeries: [series({ id: "s-3", playbook_key: "trial_balance", period: null, report_id: null })],
  });
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const item = await findByRole("menuitem", { name: /trial balance/i });
  expect(item.textContent).toContain("—");
});

it("omits the Tracking the close group entirely when there are no tracking series", async () => {
  const { findByRole, queryByText } = renderSwitcher({ publishedSeries: [] });
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  await findByRole("menuitem", { name: /manage published set/i }); // menu is open
  expect(queryByText("Tracking the close")).toBeNull();
});

it("lists Tracking the close before Pinned months", async () => {
  const { findByRole } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);
  await findByRole("menuitem", { name: /income statement/i });

  // Radix portals DropdownMenuContent to document.body, not into the render
  // container — search the whole document for the two group labels' order.
  const labels = Array.from(document.body.querySelectorAll("[role='menu'] *")).filter(
    (el) => el.textContent === "Tracking the close" || el.textContent === "Pinned months"
  );
  const text = labels.map((el) => el.textContent);
  expect(text.indexOf("Tracking the close")).toBeGreaterThanOrEqual(0);
  expect(text.indexOf("Pinned months")).toBeGreaterThan(text.indexOf("Tracking the close"));
});

it("selecting a Tracking the close item PUTs its series_id to /api/v1/dashboard/active", async () => {
  const { findByRole } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const otherItem = await findByRole("menuitem", { name: /balance sheet/i });
  fireEvent.click(otherItem);

  await waitFor(() =>
    expect(api.put).toHaveBeenCalledWith("/api/v1/dashboard/active", { series_id: "s-2" })
  );
});

// --- Pinned months (renamed from "Published dashboards") ---------------------------

it("opens to a Pinned months group listing every published report with a ✓ on the active one", async () => {
  // activeSeriesId null => the PINNED selection is the active one, so the ✓ belongs
  // here. (With a series active the pinned group carries no ✓ at all — see the
  // mutual-exclusivity test below.)
  const { findByRole, findByText } = renderSwitcher({ activeSeriesId: null });
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  expect(await findByText("Pinned months")).toBeTruthy();
  const activeItem = await findByRole("menuitem", { name: /cash flow.*q2/i });
  expect(activeItem.textContent).toContain("✓");
  const otherItem = await findByRole("menuitem", { name: /board snapshot/i });
  expect(otherItem.textContent).not.toContain("✓");
});

it("shows exactly one ✓ when the active series' newest report is ALSO individually pinned", async () => {
  // T2 gate round 4: the caller passes activeId={report.id} unconditionally, and with a
  // tracking selection active `report` IS the series' newest report -- so a report that
  // is both pinned and newest-in-series lit up in BOTH groups, and a switcher whose one
  // job is saying what you're on said two things. activeId is documented as "meaningless
  // (and never matched) while a tracking selection is active"; enforce that in the
  // component rather than trusting every caller to remember it.
  const { findByRole } = renderSwitcher({ activeId: "r-2", activeSeriesId: "s-1" });
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const seriesItem = await findByRole("menuitem", { name: /income statement/i });
  expect(seriesItem.textContent).toContain("✓");
  const pinnedItem = await findByRole("menuitem", { name: /cash flow.*q2/i });
  expect(pinnedItem.textContent).not.toContain("✓");
});

it("shows 'snapshot' as meta on every Pinned months item, regardless of its own auto_refresh cadence", async () => {
  const { findByRole } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const hourlyItem = await findByRole("menuitem", { name: /cash flow/i }); // auto_refresh: "hourly"
  expect(hourlyItem.textContent).toContain("snapshot");
  expect(hourlyItem.textContent).not.toContain("hourly");
  const offItem = await findByRole("menuitem", { name: /board snapshot/i }); // auto_refresh: "off"
  expect(offItem.textContent).toContain("snapshot");
});

it("has a divider then a Manage published set… link to /reports", async () => {
  const { findByRole } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const manageLink = await findByRole("menuitem", { name: /manage published set/i });
  const anchor = manageLink.querySelector("a") ?? manageLink;
  expect(anchor.getAttribute("href")).toBe("/reports");
});

it("selecting a Pinned months item PUTs its report_id to /api/v1/dashboard/active", async () => {
  const { findByRole } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const otherItem = await findByRole("menuitem", { name: /board snapshot/i });
  fireEvent.click(otherItem);

  await waitFor(() =>
    expect(api.put).toHaveBeenCalledWith("/api/v1/dashboard/active", { report_id: "r-3" })
  );
});

it("disables report items while the switch mutation is pending", async () => {
  // Radix closes the menu on selection (default behavior) — the meaningful
  // case for "disabled while pending" is a user reopening the menu (e.g. a
  // second intent) before the in-flight PUT from the first selection resolves.
  let resolvePut!: (v: unknown) => void;
  api.put.mockReturnValue(
    new Promise((resolve) => {
      resolvePut = resolve;
    })
  );
  const { findByRole } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const otherItem = await findByRole("menuitem", { name: /board snapshot/i });
  fireEvent.click(otherItem);

  openSwitcher(trigger);
  const reopenedItem = await findByRole("menuitem", { name: /board snapshot/i });
  await waitFor(() => expect(reopenedItem.getAttribute("aria-disabled")).toBe("true"));

  resolvePut({ published, active: published[1], active_is_fallback: false });
});

it("still renders the menu (with Manage published set…) when published.length <= 1 and no series", async () => {
  const { findByRole } = renderSwitcher({ published: [published[0]], activeId: "r-2", publishedSeries: [] });
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  expect(await findByRole("menuitem", { name: /manage published set/i })).toBeTruthy();
});

// --- Review fix: surface a failed switch instead of failing silently -------

it("shows the backend's error message inline when the switch PUT fails, and it persists after the menu closes", async () => {
  api.put.mockRejectedValueOnce(new Error("That report isn't published to the dashboard"));
  const { findByRole, findByText, queryByRole } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const otherItem = await findByRole("menuitem", { name: /board snapshot/i });
  fireEvent.click(otherItem);

  expect(await findByText("That report isn't published to the dashboard")).toBeTruthy();
  // Radix closes the menu on selection — the message must still be visible after
  // the menu itself has unmounted, not just while it happens to still be open.
  await waitFor(() => expect(queryByRole("menuitem", { name: /board snapshot/i })).toBeNull());
  expect(await findByText("That report isn't published to the dashboard")).toBeTruthy();
});

it("falls back to a generic message when the backend error has no message", async () => {
  api.put.mockRejectedValueOnce(new Error());
  const { findByRole, findByText } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const otherItem = await findByRole("menuitem", { name: /board snapshot/i });
  fireEvent.click(otherItem);

  expect(await findByText("Couldn't switch dashboard")).toBeTruthy();
});

it("invalidates the dashboard query on a failed switch so the stale menu self-heals on retry", async () => {
  // Review fix M2: a 409 (another user unpublished the report in the meantime) left
  // the stale menu entry rendered and every retry re-erroring, because nothing ever
  // refetched ["dashboard"] to drop the now-unpublished entry from `published`.
  api.put.mockRejectedValueOnce(new Error("That report isn't published to the dashboard"));
  const { findByRole, findByText } = renderSwitcher();
  const invalidate = vi.spyOn(lastQueryClient!, "invalidateQueries");
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const otherItem = await findByRole("menuitem", { name: /board snapshot/i });
  fireEvent.click(otherItem);

  await findByText("That report isn't published to the dashboard");
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["dashboard"]));
});
