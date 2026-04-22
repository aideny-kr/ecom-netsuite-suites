import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { RunProgressBar } from "../run-progress-bar";

describe("RunProgressBar", () => {
  it("shows '0/N' when cases_completed is 0", () => {
    render(<RunProgressBar casesCompleted={0} totalCases={18} runningCost={0} status="running" />);
    expect(screen.getByText(/0 \/ 18 complete/i)).toBeInTheDocument();
  });

  it("shows running cost with $", () => {
    render(<RunProgressBar casesCompleted={5} totalCases={18} runningCost={1.75} status="running" />);
    expect(screen.getByText(/\$1\.75/)).toBeInTheDocument();
  });

  it("shows 'Preparing' when status is preparing", () => {
    render(<RunProgressBar casesCompleted={0} totalCases={0} runningCost={0} status="preparing" preparingPhase="mining" />);
    expect(screen.getByText(/preparing/i)).toBeInTheDocument();
    expect(screen.getByText(/mining/i)).toBeInTheDocument();
  });
});
