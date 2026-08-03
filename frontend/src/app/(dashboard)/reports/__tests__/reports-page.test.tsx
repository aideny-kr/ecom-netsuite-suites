import { fireEvent, render, screen, within } from "@testing-library/react";
import { it, expect, vi, beforeEach } from "vitest";

const deleteMutate = vi.fn();
const unpinMutate = vi.fn();
const unpinState = vi.hoisted(() => ({ isPending: false }));
const reportsData = vi.hoisted(() => ({
  current: [
    {
      id: "abc",
      title: "Q2 Review",
      status: "draft",
      version: 1,
      created_at: "2026-06-10T00:00:00Z",
      created_by: "creator-1",
    },
  ],
}));
vi.mock("@/hooks/use-reports", () => ({
  useReports: () => ({ data: reportsData.current, isLoading: false }),
  usePlaybooks: () => ({ data: [], isLoading: false }),
  useComposePlaybook: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteReport: () => ({ mutate: deleteMutate, isPending: false, error: null }),
  useUnpinReport: () => ({ mutate: unpinMutate, isPending: unpinState.isPending }),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

// Published-dashboards section (Task 5) reuses Task 3/4's useDashboard() rather than
// re-deriving "published" + "which one is mine" from useReports() — no new endpoint.
const dashboardData = vi.hoisted(() => ({
  current: {
    published: [] as Array<{ id: string; title: string; dashboard_pinned_at?: string | null }>,
    active: null as { id: string } | null,
    active_is_fallback: false,
  },
}));
vi.mock("@/hooks/use-dashboard", () => ({
  useDashboard: () => ({ data: dashboardData.current }),
}));

const authState = vi.hoisted(() => ({ user: { id: "creator-1", roles: [] as string[] } }));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => authState }));

import ReportsPage from "@/app/(dashboard)/reports/page";

beforeEach(() => {
  deleteMutate.mockClear();
  unpinMutate.mockClear();
  unpinState.isPending = false;
  authState.user = { id: "creator-1", roles: [] };
  reportsData.current = [
    {
      id: "abc",
      title: "Q2 Review",
      status: "draft",
      version: 1,
      created_at: "2026-06-10T00:00:00Z",
      created_by: "creator-1",
    },
  ];
  dashboardData.current = { published: [], active: null, active_is_fallback: false };
});

it("lists reports with a link to each", () => {
  render(<ReportsPage />);
  expect(screen.getByText("Q2 Review")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /q2 review/i })).toHaveAttribute("href", "/reports/abc");
});

it("shows the trash icon for the report's creator", () => {
  render(<ReportsPage />);
  expect(screen.getByRole("button", { name: /delete report/i })).toBeInTheDocument();
});

it("shows the trash icon for a tenant admin who did not create the report", () => {
  authState.user = { id: "someone-else", roles: ["admin"] };
  render(<ReportsPage />);
  expect(screen.getByRole("button", { name: /delete report/i })).toBeInTheDocument();
});

it("hides the trash icon for a non-creator, non-admin user", () => {
  authState.user = { id: "someone-else", roles: [] };
  render(<ReportsPage />);
  expect(screen.queryByRole("button", { name: /delete report/i })).toBeNull();
});

it("clicking the trash icon opens the confirm dialog without following the row link", () => {
  render(<ReportsPage />);
  fireEvent.click(screen.getByRole("button", { name: /delete report/i }));
  expect(screen.getByRole("heading", { name: "Delete this report?" })).toBeInTheDocument();
});

// --- Task 5: Published dashboards section --------------------------------------------

const publishedReport = (over: object = {}) => ({
  id: "p-1",
  title: "Board Snapshot",
  status: "final",
  version: 3,
  created_at: "2026-07-01T00:00:00Z",
  created_by: "creator-1",
  dashboard_pinned_at: "2026-07-20T10:00:00Z",
  ...over,
});

it("hides the Published dashboards section when nothing is published", () => {
  dashboardData.current = { published: [], active: null, active_is_fallback: false };
  render(<ReportsPage />);
  expect(screen.queryByText("Published dashboards")).toBeNull();
});

it("shows the Published dashboards section with title, publish stamp, and the ON YOUR WALL pill for the viewer's active pick", () => {
  const pub = publishedReport();
  dashboardData.current = { published: [pub], active: { id: pub.id }, active_is_fallback: false };
  render(<ReportsPage />);
  expect(screen.getByRole("heading", { name: "Published dashboards" })).toBeInTheDocument();
  const row = screen.getByText("Board Snapshot").closest("a")!.parentElement!;
  expect(within(row).getByText(/on your wall/i)).toBeInTheDocument();
  expect(within(row).getByText(/2026|jul/i)).toBeInTheDocument(); // the publish stamp
});

it("shows the PUBLISHED pill (not ON YOUR WALL) for a published report that isn't the viewer's active pick", () => {
  const active = publishedReport({ id: "p-1", title: "Board Snapshot" });
  const other = publishedReport({ id: "p-2", title: "Q2 Cash Flow" });
  dashboardData.current = {
    published: [active, other],
    active: { id: active.id },
    active_is_fallback: false,
  };
  render(<ReportsPage />);
  const row = screen.getByText("Q2 Cash Flow").closest("a")!.parentElement!;
  expect(within(row).getByText("Published")).toBeInTheDocument();
  expect(within(row).queryByText(/on your wall/i)).toBeNull();
});

it("shows Unpublish for the report's creator and fires the unpin mutation", () => {
  const pub = publishedReport();
  dashboardData.current = { published: [pub], active: { id: pub.id }, active_is_fallback: false };
  render(<ReportsPage />);
  fireEvent.click(screen.getByRole("button", { name: /unpublish/i }));
  expect(unpinMutate).toHaveBeenCalled();
});

it("hides Unpublish for a non-creator, non-admin user", () => {
  authState.user = { id: "someone-else", roles: [] };
  const pub = publishedReport();
  dashboardData.current = { published: [pub], active: { id: pub.id }, active_is_fallback: false };
  render(<ReportsPage />);
  expect(screen.queryByRole("button", { name: /unpublish/i })).toBeNull();
});

it("shows Unpublish for a tenant admin who did not create the report", () => {
  authState.user = { id: "someone-else", roles: ["admin"] };
  const pub = publishedReport();
  dashboardData.current = { published: [pub], active: { id: pub.id }, active_is_fallback: false };
  render(<ReportsPage />);
  expect(screen.getByRole("button", { name: /unpublish/i })).toBeInTheDocument();
});

// --- Review fix M4: a failed Unpublish must surface, not fail silently -----------

it("shows the backend's error message inline when Unpublish fails", () => {
  const pub = publishedReport();
  dashboardData.current = { published: [pub], active: { id: pub.id }, active_is_fallback: false };
  unpinMutate.mockImplementation((_vars?: unknown, opts?: { onError?: (e: Error) => void }) =>
    opts?.onError?.(new Error("That report isn't published to the dashboard"))
  );
  render(<ReportsPage />);
  fireEvent.click(screen.getByRole("button", { name: /unpublish/i }));
  expect(screen.getByText("That report isn't published to the dashboard")).toBeInTheDocument();
});

it("falls back to a generic message when Unpublish fails with no message", () => {
  const pub = publishedReport();
  dashboardData.current = { published: [pub], active: { id: pub.id }, active_is_fallback: false };
  unpinMutate.mockImplementation((_vars?: unknown, opts?: { onError?: (e: Error) => void }) =>
    opts?.onError?.(new Error())
  );
  render(<ReportsPage />);
  fireEvent.click(screen.getByRole("button", { name: /unpublish/i }));
  expect(screen.getByText(/couldn.t unpublish/i)).toBeInTheDocument();
});
