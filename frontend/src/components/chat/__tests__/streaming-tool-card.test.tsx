import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { StreamingToolCard } from "../streaming-tool-card";
import type { StreamingToolCall } from "@/lib/types";

vi.mock("lucide-react", () => {
  const MockIcon = ({ className }: { className?: string }) => <span data-testid="icon" className={className} />;
  return {
    Database: MockIcon, Search: MockIcon, FileText: MockIcon, Table: MockIcon,
    BookOpen: MockIcon, Globe: MockIcon, Code2: MockIcon, Wrench: MockIcon,
    Check: MockIcon, X: MockIcon, ChevronDown: MockIcon, ChevronUp: MockIcon,
    Loader2: MockIcon,
  };
});

const runningTool: StreamingToolCall = {
  tool_name: "netsuite_suiteql",
  tool_input: { query: "SELECT t.tranid FROM transaction t FETCH FIRST 5 ROWS ONLY" },
  step: 1,
  status: "running",
};

const completeTool: StreamingToolCall = {
  tool_name: "bigquery_sql",
  tool_input: { query: "SELECT * FROM `project.dataset.table` LIMIT 10" },
  step: 2,
  status: "complete",
  duration_ms: 1234,
  success: true,
  result_summary: "24 rows returned",
};

const errorTool: StreamingToolCall = {
  tool_name: "netsuite_suiteql",
  tool_input: { query: "SELECT bad_column FROM transaction" },
  step: 3,
  status: "error",
  duration_ms: 500,
  success: false,
  result_summary: "Error",
};

describe("StreamingToolCard", () => {
  it("renders running state with spinner", () => {
    render(<StreamingToolCard tool={runningTool} />);
    expect(screen.getByText("SuiteQL Query")).toBeInTheDocument();
    expect(screen.getByTestId("streaming-tool-card")).toHaveClass("border-primary/30");
  });

  it("renders complete state with checkmark and duration", () => {
    render(<StreamingToolCard tool={completeTool} />);
    expect(screen.getByText("BigQuery Query")).toBeInTheDocument();
    expect(screen.getByText("1.2s")).toBeInTheDocument();
    expect(screen.getByTestId("streaming-tool-card")).toHaveClass("border-emerald-500/30");
  });

  it("renders error state", () => {
    render(<StreamingToolCard tool={errorTool} />);
    expect(screen.getByTestId("streaming-tool-card")).toHaveClass("border-red-500/30");
  });

  it("expands to show input and result on click", () => {
    render(<StreamingToolCard tool={completeTool} />);
    // Click header to expand
    fireEvent.click(screen.getByText("BigQuery Query"));
    expect(screen.getByText("24 rows returned")).toBeInTheDocument();
  });

  it("shows truncated input preview when expanded", () => {
    render(<StreamingToolCard tool={runningTool} />);
    fireEvent.click(screen.getByText("SuiteQL Query"));
    expect(screen.getByText(/SELECT t.tranid/)).toBeInTheDocument();
  });

  it("maps unknown tool names to formatted label", () => {
    const unknownTool: StreamingToolCall = {
      tool_name: "some_custom_tool",
      tool_input: {},
      step: 1,
      status: "running",
    };
    render(<StreamingToolCard tool={unknownTool} />);
    expect(screen.getByText("some custom tool")).toBeInTheDocument();
  });
});
