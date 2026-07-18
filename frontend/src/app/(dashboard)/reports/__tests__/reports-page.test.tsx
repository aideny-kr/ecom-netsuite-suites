import { render, screen } from "@testing-library/react";
import { it, expect, vi } from "vitest";
vi.mock("@/hooks/use-reports", () => ({
  useReports: () => ({ data: [{ id: "abc", title: "Q2 Review", status: "draft", version: 1, created_at: "2026-06-10T00:00:00Z" }], isLoading: false }),
  usePlaybooks: () => ({ data: [], isLoading: false }),
  useComposePlaybook: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
import ReportsPage from "@/app/(dashboard)/reports/page";

it("lists reports with a link to each", () => {
  render(<ReportsPage />);
  expect(screen.getByText("Q2 Review")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /q2 review/i })).toHaveAttribute("href", "/reports/abc");
});
