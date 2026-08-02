import { fireEvent, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const api = vi.hoisted(() => ({ put: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import { DashboardSwitcher } from "@/app/(dashboard)/dashboard/dashboard-switcher";
import type { ReportSummary } from "@/hooks/use-reports";

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

const published = [
  report({ id: "r-1", title: "Income Statement — Jun 2026", auto_refresh: "daily" }),
  report({ id: "r-2", title: "Cash Flow — Q2", auto_refresh: "hourly" }),
  report({ id: "r-3", title: "Board Snapshot", auto_refresh: "off" }),
];

function renderSwitcher(props: Partial<React.ComponentProps<typeof DashboardSwitcher>> = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }
  return render(
    <DashboardSwitcher published={published} activeId="r-1" {...props} />,
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

it("opens to a Published dashboards group listing every published report with a ✓ on the active one", async () => {
  const { findByRole, findByText } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  expect(await findByText("Published dashboards")).toBeTruthy();
  const activeItem = await findByRole("menuitem", { name: /income statement.*jun 2026/i });
  expect(activeItem.textContent).toContain("✓");
  const otherItem = await findByRole("menuitem", { name: /cash flow.*q2/i });
  expect(otherItem.textContent).not.toContain("✓");
});

it("shows the auto_refresh value as right-aligned meta, rendering off as snapshot", async () => {
  const { findByRole } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const dailyItem = await findByRole("menuitem", { name: /income statement/i });
  expect(dailyItem.textContent).toContain("daily");
  const hourlyItem = await findByRole("menuitem", { name: /cash flow/i });
  expect(hourlyItem.textContent).toContain("hourly");
  const offItem = await findByRole("menuitem", { name: /board snapshot/i });
  expect(offItem.textContent).toContain("snapshot");
  expect(offItem.textContent).not.toContain("off");
});

it("has a divider then a Manage published set… link to /reports", async () => {
  const { findByRole } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const manageLink = await findByRole("menuitem", { name: /manage published set/i });
  const anchor = manageLink.querySelector("a") ?? manageLink;
  expect(anchor.getAttribute("href")).toBe("/reports");
});

it("selecting an item PUTs its id to /api/v1/dashboard/active", async () => {
  const { findByRole } = renderSwitcher();
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  const otherItem = await findByRole("menuitem", { name: /cash flow.*q2/i });
  fireEvent.click(otherItem);

  await waitFor(() =>
    expect(api.put).toHaveBeenCalledWith("/api/v1/dashboard/active", { report_id: "r-2" })
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

  const otherItem = await findByRole("menuitem", { name: /cash flow.*q2/i });
  fireEvent.click(otherItem);

  openSwitcher(trigger);
  const reopenedItem = await findByRole("menuitem", { name: /cash flow.*q2/i });
  await waitFor(() => expect(reopenedItem.getAttribute("aria-disabled")).toBe("true"));

  resolvePut({ published, active: published[1], active_is_fallback: false });
});

it("still renders the menu (with Manage published set…) when published.length <= 1", async () => {
  const { findByRole } = renderSwitcher({ published: [published[0]], activeId: "r-1" });
  const trigger = await findByRole("button", { name: /switch/i });
  openSwitcher(trigger);

  expect(await findByRole("menuitem", { name: /manage published set/i })).toBeTruthy();
});
