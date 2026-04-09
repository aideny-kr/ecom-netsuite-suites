import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { DisclosureFooter } from "../disclosure-footer";
import type { DisclosureBlock } from "@/lib/types";

vi.mock("lucide-react", () => {
  const MockIcon = ({ className }: { className?: string }) => (
    <span data-testid="chevron-icon" className={className} />
  );
  return { ChevronDown: MockIcon };
});

const baseDisclosure: DisclosureBlock = {
  source: "netsuite",
  interpretation: '"This week" = current week',
  implicit_filters: ["Excludes cancelled records", "Excludes test records"],
  can_switch_source: true,
  is_rerun: false,
  failure_mode: false,
};

describe("DisclosureFooter", () => {
  it("renders collapsed by default with a single line", () => {
    render(<DisclosureFooter disclosure={baseDisclosure} />);
    expect(screen.getByText(/Read from/i)).toBeInTheDocument();
    expect(screen.getByText(/NetSuite/)).toBeInTheDocument();
    expect(screen.getByText(/current week/)).toBeInTheDocument();
    // Bullets hidden
    expect(screen.queryByText(/Excludes cancelled records/)).not.toBeInTheDocument();
  });

  it("expands on click, showing bullets and switch hint", () => {
    render(<DisclosureFooter disclosure={baseDisclosure} />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/Excludes cancelled records/)).toBeInTheDocument();
    expect(screen.getByText(/Excludes test records/)).toBeInTheDocument();
    expect(screen.getByText(/use BigQuery/i)).toBeInTheDocument();
  });

  it("omits chevron and is not clickable when there are no details", () => {
    render(
      <DisclosureFooter
        disclosure={{
          ...baseDisclosure,
          implicit_filters: [],
          can_switch_source: false,
        }}
      />
    );
    const btn = screen.getByRole("button");
    expect(btn).toBeDisabled();
    expect(screen.queryByTestId("chevron-icon")).not.toBeInTheDocument();
  });

  it("applies amber border in failure mode", () => {
    const { container } = render(
      <DisclosureFooter
        disclosure={{
          ...baseDisclosure,
          failure_mode: true,
          interpretation: "Tried NetSuite.",
          implicit_filters: [],
        }}
      />
    );
    const root = container.firstChild as HTMLElement;
    expect(root.className).toMatch(/border-amber/);
  });

  it("renders inline re-run label when is_rerun is true", () => {
    render(
      <DisclosureFooter
        disclosure={{
          ...baseDisclosure,
          is_rerun: true,
        }}
      />
    );
    expect(screen.getByText(/re-ran after source switch/i)).toBeInTheDocument();
  });

  it("hides switch hint in expanded view when is_rerun is true", () => {
    render(
      <DisclosureFooter
        disclosure={{
          ...baseDisclosure,
          is_rerun: true,
        }}
      />
    );
    fireEvent.click(screen.getByRole("button"));
    expect(screen.queryByText(/use BigQuery/i)).not.toBeInTheDocument();
  });

  it("shows bigquery source label correctly", () => {
    render(
      <DisclosureFooter
        disclosure={{
          ...baseDisclosure,
          source: "bigquery",
        }}
      />
    );
    expect(screen.getByText(/BigQuery/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/use NetSuite/i)).toBeInTheDocument();
  });
});
