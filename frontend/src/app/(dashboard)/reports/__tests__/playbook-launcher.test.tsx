import { act, render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const mutate = vi.fn();
vi.mock("@/hooks/use-reports", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  usePlaybooks: () => ({
    data: [
      {
        key: "income_statement",
        name: "Income Statement",
        description: "Statement-grade P&L",
        params: [{ key: "period", label: "Accounting period", example: "Jun 2026" }],
      },
    ],
    isLoading: false,
  }),
  useComposePlaybook: () => ({ mutate, isPending: false }),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import { PlaybookLauncher } from "../playbook-launcher";

describe("PlaybookLauncher", () => {
  it("launches a playbook with the entered period", () => {
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    fireEvent.change(screen.getByPlaceholderText("Jun 2026"), { target: { value: "Jun 2026" } });
    fireEvent.click(screen.getByRole("button", { name: /create report/i }));
    expect(mutate).toHaveBeenCalledWith(
      { key: "income_statement", params: { period: "Jun 2026" } },
      expect.anything(),
    );
  });

  it("renders each playbook as a native, keyboard-focusable button", () => {
    render(<PlaybookLauncher />);
    const card = screen.getByRole("button", { name: /income statement/i });
    expect(card.tagName).toBe("BUTTON");
  });

  it("surfaces the mutation error (e.g. malformed period or no NetSuite connection)", () => {
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    fireEvent.change(screen.getByPlaceholderText("Jun 2026"), { target: { value: "June 2026" } });
    fireEvent.click(screen.getByRole("button", { name: /create report/i }));

    const [, opts] = mutate.mock.calls[mutate.mock.calls.length - 1];
    act(() => opts.onError(new Error("period must be a NetSuite period name like 'Jun 2026'")));

    expect(screen.getByText("period must be a NetSuite period name like 'Jun 2026'")).toBeInTheDocument();
  });

  it("clears a prior error message when the playbook selection changes", () => {
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    fireEvent.click(screen.getByRole("button", { name: /create report/i }));
    const [, opts] = mutate.mock.calls[mutate.mock.calls.length - 1];
    act(() => opts.onError(new Error("No active NetSuite connection found")));
    expect(screen.getByText("No active NetSuite connection found")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Income Statement")); // reselect -> clears the stale error
    expect(screen.queryByText("No active NetSuite connection found")).not.toBeInTheDocument();
  });
});
