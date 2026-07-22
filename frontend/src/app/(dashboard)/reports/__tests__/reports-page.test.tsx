import { fireEvent, render, screen } from "@testing-library/react";
import { it, expect, vi, beforeEach } from "vitest";

const deleteMutate = vi.fn();
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
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

const authState = vi.hoisted(() => ({ user: { id: "creator-1", roles: [] as string[] } }));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => authState }));

import ReportsPage from "@/app/(dashboard)/reports/page";

beforeEach(() => {
  deleteMutate.mockClear();
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
