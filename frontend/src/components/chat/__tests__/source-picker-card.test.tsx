import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { SourcePickerCard } from "../source-picker-card";
import type { SourcePickerData } from "@/lib/types";

const basePayload: SourcePickerData = {
  type: "source_picker",
  recommended: "netsuite",
  confidence: 0.55,
  reason: "operational data available in both sources",
  user_question: "how many orders this week",
  options: [
    {
      source: "netsuite",
      label: "NetSuite",
      description: "Source of truth for daily operations and finance.",
      recommended: true,
    },
    {
      source: "bigquery",
      label: "BigQuery",
      description: "BI & analytics warehouse — fast for trends and large aggregations.",
      recommended: false,
    },
  ],
};

describe("SourcePickerCard", () => {
  it("renders the user question", () => {
    render(<SourcePickerCard data={basePayload} onPick={() => {}} />);
    expect(screen.getByText(/how many orders this week/)).toBeInTheDocument();
  });

  it("renders both option labels", () => {
    render(<SourcePickerCard data={basePayload} onPick={() => {}} />);
    expect(screen.getByText("NetSuite")).toBeInTheDocument();
    expect(screen.getByText("BigQuery")).toBeInTheDocument();
  });

  it("shows Recommended badge on the recommended option", () => {
    render(<SourcePickerCard data={basePayload} onPick={() => {}} />);
    const badges = screen.getAllByText(/recommended/i);
    expect(badges.length).toBeGreaterThanOrEqual(1);
  });

  it("calls onPick with netsuite when NetSuite button clicked", () => {
    const onPick = vi.fn();
    render(<SourcePickerCard data={basePayload} onPick={onPick} />);
    fireEvent.click(screen.getByRole("button", { name: /use netsuite/i }));
    expect(onPick).toHaveBeenCalledWith("netsuite");
  });

  it("calls onPick with bigquery when BigQuery button clicked", () => {
    const onPick = vi.fn();
    render(<SourcePickerCard data={basePayload} onPick={onPick} />);
    fireEvent.click(screen.getByRole("button", { name: /use bigquery/i }));
    expect(onPick).toHaveBeenCalledWith("bigquery");
  });

  it("disables both buttons when disabled prop is true", () => {
    render(<SourcePickerCard data={basePayload} onPick={() => {}} disabled />);
    expect(screen.getByRole("button", { name: /use netsuite/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /use bigquery/i })).toBeDisabled();
  });

  it("renders the reason text", () => {
    render(<SourcePickerCard data={basePayload} onPick={() => {}} />);
    expect(screen.getByText(/operational data available in both/i)).toBeInTheDocument();
  });
});
