import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { ValidationHitsTable } from "../validation-hits-table";
import type { ValidationHit } from "@/lib/types";

const hits: ValidationHit[] = [
  {
    id: "h1",
    run_id: "r1",
    file_path: "src/Suitelets/foo.js",
    line: 42,
    severity: "error",
    code: "OWASP-A03",
    rule_id: null,
    message: "Unsanitized user input flowed into N/query",
    fingerprint: "f1",
  },
  {
    id: "h2",
    run_id: "r1",
    file_path: "src/Suitelets/foo.js",
    line: 67,
    severity: "warning",
    code: "SUITESCRIPT-DEPRECATED-2X",
    rule_id: null,
    message: "nlapi deprecated",
    fingerprint: "f2",
  },
];

describe("ValidationHitsTable", () => {
  it("renders one row per hit with file:line + severity badge + code + message", () => {
    render(<ValidationHitsTable hits={hits} />);
    expect(screen.getByText("src/Suitelets/foo.js:42")).toBeInTheDocument();
    expect(screen.getByText("src/Suitelets/foo.js:67")).toBeInTheDocument();
    expect(screen.getByText("OWASP-A03")).toBeInTheDocument();
    expect(screen.getByText("SUITESCRIPT-DEPRECATED-2X")).toBeInTheDocument();
    expect(
      screen.getByText("Unsanitized user input flowed into N/query"),
    ).toBeInTheDocument();
    expect(screen.getByText("nlapi deprecated")).toBeInTheDocument();
    expect(screen.getAllByTestId("severity-badge")).toHaveLength(2);
  });

  it("shows empty state when no hits", () => {
    render(<ValidationHitsTable hits={[]} />);
    expect(screen.getByText(/no validate hits/i)).toBeInTheDocument();
  });

  it("shows em dash for hit with null file_path", () => {
    const noFile: ValidationHit = {
      id: "h3",
      run_id: "r1",
      file_path: null,
      line: null,
      severity: "parser_error",
      code: null,
      rule_id: null,
      message: "raw output preserved",
      fingerprint: "f3",
    };
    render(<ValidationHitsTable hits={[noFile]} />);
    // Both location (null file_path) and code (null) render as em dash
    expect(screen.getAllByText("—")).toHaveLength(2);
    expect(screen.getByText("raw output preserved")).toBeInTheDocument();
  });

  it("renders parser_error severity with its label", () => {
    const parserError: ValidationHit = {
      id: "h4",
      run_id: "r1",
      file_path: null,
      line: null,
      severity: "parser_error",
      code: null,
      rule_id: null,
      message: "stdout did not match expected format",
      fingerprint: "f4",
    };
    render(<ValidationHitsTable hits={[parserError]} />);
    const badge = screen.getByTestId("severity-badge");
    expect(badge).toHaveTextContent("parser_error");
  });
});
