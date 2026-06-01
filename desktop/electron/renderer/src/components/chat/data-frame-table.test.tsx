// @vitest-environment jsdom
/**
 * Renderer reuse tests (rich-pipe slice 1, Task C2).
 *
 * Proves the reuse contract end-to-end, key-free: a webapp-shaped data_table
 * IPC event (the exact JSON the Python sidecar emits — see
 * desktop/runtime/orchestration/events.py DataTableEvent.to_dict) flows through
 * the REUSED chat-stream.ts normalizer into the data-frame-table card's props,
 * the card renders the tool's rows, and rendering is XSS-safe (text, not HTML).
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { normalizeStreamEvent, type DataTableData } from "@/lib/chat-stream";
import { DataFrameTable } from "@/components/chat/data-frame-table";

// The exact event shape the Python DataTableEvent.to_dict() emits for the
// sample_dataset tool (finance sample table).
const ipcDataTableEvent = {
  type: "data_table",
  data: {
    columns: ["Account", "Balance (USD)"],
    rows: [
      ["Cash & Equivalents", 1284500.0],
      ["Accounts Receivable", 842300.5],
      ["Inventory", 415900.0],
    ],
    row_count: 3,
    query: "",
    truncated: false,
  },
};

function normalizedData(): DataTableData {
  const event = normalizeStreamEvent(ipcDataTableEvent as unknown as Record<string, unknown>);
  if (!event || event.type !== "data_table") {
    throw new Error("normalizer did not produce a data_table event");
  }
  return event.data;
}

describe("data_table reuse: webapp normalizer -> card props", () => {
  it("normalizes the IPC data_table event into the webapp DataTableData shape", () => {
    const data = normalizedData();
    expect(data.columns).toEqual(["Account", "Balance (USD)"]);
    expect(data.rows).toEqual(ipcDataTableEvent.data.rows);
    expect(data.row_count).toBe(3);
    expect(data.query).toBe("");
    expect(data.truncated).toBe(false);
  });
});

describe("DataFrameTable renders the tool's rows", () => {
  it("renders the column header and the row cell values", () => {
    render(<DataFrameTable data={normalizedData()} />);
    expect(screen.getByText("Account")).toBeInTheDocument();
    expect(screen.getByText("Cash & Equivalents")).toBeInTheDocument();
    expect(screen.getByText("Accounts Receivable")).toBeInTheDocument();
    expect(screen.getByText("Inventory")).toBeInTheDocument();
  });

  it("shows the row count", () => {
    render(<DataFrameTable data={normalizedData()} />);
    expect(screen.getAllByText(/3 rows/i).length).toBeGreaterThan(0);
  });

  it("returns nothing when there are no columns", () => {
    const empty: DataTableData = { columns: [], rows: [], row_count: 0, query: "", truncated: false };
    const { container } = render(<DataFrameTable data={empty} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("DataFrameTable is XSS-safe", () => {
  it("renders a cell containing HTML as literal text, never as DOM", () => {
    const malicious = '<img src=x onerror="alert(1)">';
    const data: DataTableData = {
      columns: ["Account"],
      rows: [[malicious]],
      row_count: 1,
      query: "",
      truncated: false,
    };
    const { container } = render(<DataFrameTable data={data} />);
    // present as TEXT content...
    expect(screen.getByText(malicious)).toBeInTheDocument();
    // ...but NO real <img> element was injected.
    expect(container.querySelector("img")).toBeNull();
  });
});
