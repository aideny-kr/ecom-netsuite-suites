import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { DashboardEmptyState } from "@/app/(dashboard)/dashboard/dashboard-empty-state";

it("shows the empty-state heading and body copy verbatim", () => {
  render(<DashboardEmptyState />);
  expect(screen.getByText("No reports in Command Center yet")).toBeInTheDocument();
  expect(
    screen.getByText(
      "Compose a report, then publish it to Command Center. You can publish several and switch between them anytime."
    )
  ).toBeInTheDocument();
});

it("links Browse reports → to /reports", () => {
  render(<DashboardEmptyState />);
  const link = screen.getByRole("link", { name: /browse reports/i });
  expect(link).toHaveAttribute("href", "/reports");
});
