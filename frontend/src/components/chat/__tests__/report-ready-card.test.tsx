import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";

import { ReportReadyCard } from "@/components/chat/report-ready-card";

describe("ReportReadyCard", () => {
  it("links in-app to the report", () => {
    render(<ReportReadyCard data={{ report_id: "abc", title: "Q2 Review", url: "/reports/abc" }} />);
    const link = screen.getByRole("link", { name: /open.*report|q2 review/i });
    expect(link).toHaveAttribute("href", "/reports/abc"); // in-app, NOT target=_blank
  });

  it("does NOT open in a new tab (in-app navigation)", () => {
    render(<ReportReadyCard data={{ report_id: "abc", title: "Q2 Review", url: "/reports/abc" }} />);
    const link = screen.getByRole("link", { name: /open.*report|q2 review/i });
    expect(link).not.toHaveAttribute("target", "_blank");
  });

  it("renders the report title", () => {
    render(<ReportReadyCard data={{ report_id: "xyz", title: "Annual Summary", url: "/reports/xyz" }} />);
    expect(screen.getByText("Annual Summary")).toBeInTheDocument();
  });
});
