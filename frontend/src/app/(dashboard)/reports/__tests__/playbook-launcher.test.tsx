import { render, screen, fireEvent } from "@testing-library/react";
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
});
