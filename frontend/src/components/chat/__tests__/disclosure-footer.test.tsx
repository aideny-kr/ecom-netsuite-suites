import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { DisclosureFooter } from "../disclosure-footer";

describe("DisclosureFooter", () => {
  it("renders source and date range", () => {
    render(
      <DisclosureFooter
        data={{
          source: "netsuite",
          interpretation: "recognized revenue",
          date_range: "2026-05-01 to 2026-07-31",
          implicit_filters: [],
        }}
      />
    );
    expect(screen.getByText(/netsuite/i)).toBeInTheDocument();
    expect(screen.getByText(/2026-05-01 to 2026-07-31/)).toBeInTheDocument();
  });

  it("renders implicit filters when present", () => {
    render(
      <DisclosureFooter
        data={{
          source: "bigquery",
          interpretation: "checkout totals",
          date_range: "Jan 1 — today",
          implicit_filters: ["Excludes status: Cancelled, Voided"],
        }}
      />
    );
    expect(screen.getByText(/Excludes status/)).toBeInTheDocument();
  });

  it("does not render when data is null", () => {
    const { container } = render(<DisclosureFooter data={null} />);
    expect(container.firstChild).toBeNull();
  });
});
