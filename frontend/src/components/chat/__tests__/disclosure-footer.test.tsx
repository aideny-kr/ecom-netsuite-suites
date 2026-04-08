import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { DisclosureFooter } from "../disclosure-footer";
import type { DisclosureBlock } from "@/lib/types";

function makeBlock(overrides: Partial<DisclosureBlock> = {}): DisclosureBlock {
  return {
    source: "netsuite",
    interpretation: "This week (Mon–today)",
    implicit_filters: ["Excludes cancelled orders", "Excludes test orders"],
    can_switch_source: true,
    is_rerun: false,
    failure_mode: false,
    ...overrides,
  };
}

describe("DisclosureFooter", () => {
  it("renders collapsed by default with one line", () => {
    render(<DisclosureFooter disclosure={makeBlock()} />);
    expect(screen.getByText(/Read from/)).toBeInTheDocument();
    expect(screen.getByText(/NetSuite/)).toBeInTheDocument();
    expect(screen.getByText(/This week/)).toBeInTheDocument();
    expect(screen.queryByText(/Excludes cancelled orders/)).not.toBeInTheDocument();
  });

  it("expands on click to show implicit filters", () => {
    render(<DisclosureFooter disclosure={makeBlock()} />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/Excludes cancelled orders/)).toBeInTheDocument();
    expect(screen.getByText(/Excludes test orders/)).toBeInTheDocument();
  });

  it("shows switch hint when expanded and can_switch_source is true", () => {
    render(<DisclosureFooter disclosure={makeBlock()} />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/Say .use BigQuery. to switch source/i)).toBeInTheDocument();
  });

  it("does not show switch hint on re-run disclosures", () => {
    render(<DisclosureFooter disclosure={makeBlock({ is_rerun: true })} />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.queryByText(/to switch source/i)).not.toBeInTheDocument();
  });

  it("renders re-run label inline when is_rerun is true", () => {
    render(<DisclosureFooter disclosure={makeBlock({ is_rerun: true })} />);
    expect(screen.getByText(/re-ran after source switch/i)).toBeInTheDocument();
  });

  it("uses amber border in failure mode", () => {
    const { container } = render(
      <DisclosureFooter disclosure={makeBlock({ failure_mode: true, implicit_filters: [] })} />
    );
    expect(container.querySelector(".border-amber-500\\/20")).toBeTruthy();
  });

  it("is not expandable when no details", () => {
    render(
      <DisclosureFooter
        disclosure={makeBlock({ implicit_filters: [], can_switch_source: false })}
      />
    );
    const button = screen.getByRole("button");
    expect(button).toBeDisabled();
  });

  it("renders BigQuery source correctly", () => {
    render(<DisclosureFooter disclosure={makeBlock({ source: "bigquery" })} />);
    expect(screen.getByText("BigQuery")).toBeInTheDocument();
  });
});
