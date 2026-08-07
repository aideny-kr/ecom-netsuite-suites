import { act, render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

const mutate = vi.fn();
// Mutable so individual tests can flip isPending to exercise the composing state.
const composeState = vi.hoisted(() => ({ isPending: false }));
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
  useComposePlaybook: () => ({ mutate, isPending: composeState.isPending }),
}));
const routerPush = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: routerPush }) }));

import { PlaybookLauncher } from "../playbook-launcher";

// Rolling-period Stage 1 (Task 5, mock §2): "Track the last closed period" is the
// default-selected chooser option, so the period Input is hidden until the operator
// explicitly switches to "One specific period" — this helper is that switch, shared
// by every test below that needs today's period-mode UI.
function chooseSpecificPeriod() {
  fireEvent.click(screen.getByLabelText(/one specific period/i));
}

beforeEach(() => {
  composeState.isPending = false;
  mutate.mockClear();
  routerPush.mockClear();
});

describe("PlaybookLauncher", () => {
  it("launches a playbook in period mode with the entered period", () => {
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    chooseSpecificPeriod();
    fireEvent.change(screen.getByPlaceholderText("Jun 2026"), { target: { value: "Jun 2026" } });
    fireEvent.click(screen.getByRole("button", { name: /create report/i }));
    expect(mutate).toHaveBeenCalledWith(
      { key: "income_statement", params: { period: "Jun 2026" }, mode: "period" },
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
    chooseSpecificPeriod();
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

// --- Rolling-period Stage 1 (Task 5): the compose chooser (mock §2) ----------------

describe("PlaybookLauncher compose chooser", () => {
  it("defaults to 'Track the last closed period' selected, with the period input hidden", () => {
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));

    expect(screen.getByLabelText(/track the last closed period/i)).toBeChecked();
    expect(screen.getByLabelText(/one specific period/i)).not.toBeChecked();
    expect(screen.queryByPlaceholderText("Jun 2026")).not.toBeInTheDocument();
  });

  it("selecting 'One specific period' reveals the period input", () => {
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    chooseSpecificPeriod();

    expect(screen.getByLabelText(/one specific period/i)).toBeChecked();
    expect(screen.getByPlaceholderText("Jun 2026")).toBeInTheDocument();
  });

  it("switching back to 'Track the last closed period' hides the period input again", () => {
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    chooseSpecificPeriod();
    expect(screen.getByPlaceholderText("Jun 2026")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText(/track the last closed period/i));
    expect(screen.queryByPlaceholderText("Jun 2026")).not.toBeInTheDocument();
  });

  it("reselecting the same or another playbook resets the chooser back to tracking", () => {
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    chooseSpecificPeriod();
    expect(screen.getByLabelText(/one specific period/i)).toBeChecked();

    fireEvent.click(screen.getByText("Income Statement")); // reselect
    expect(screen.getByLabelText(/track the last closed period/i)).toBeChecked();
    expect(screen.queryByPlaceholderText("Jun 2026")).not.toBeInTheDocument();
  });

  it("submitting in tracking mode (the default) sends mode: 'tracking' and empty params, ignoring any stale typed period", () => {
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    fireEvent.click(screen.getByRole("button", { name: /create report/i }));

    expect(mutate).toHaveBeenCalledWith(
      { key: "income_statement", params: {}, mode: "tracking" },
      expect.anything(),
    );
  });

  it("surfaces the backend's 400 message when a tracking compose can't resolve a closed period", () => {
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    fireEvent.click(screen.getByRole("button", { name: /create report/i }));

    const [, opts] = mutate.mock.calls[mutate.mock.calls.length - 1];
    act(() =>
      opts.onError(
        new Error("NetSuite doesn't have a closed accounting period yet — there's nothing to track."),
      ),
    );

    expect(
      screen.getByText("NetSuite doesn't have a closed accounting period yet — there's nothing to track."),
    ).toBeInTheDocument();
  });
});

describe("PlaybookLauncher composing state", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("replaces the parameter panel with a status card while pending, showing title and hint", () => {
    const { rerender } = render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    chooseSpecificPeriod();
    fireEvent.change(screen.getByPlaceholderText("Jun 2026"), { target: { value: "Jun 2026" } });
    composeState.isPending = true;
    rerender(<PlaybookLauncher />); // simulate the mutation flipping to pending after selection

    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent("Composing Income Statement · Jun 2026");
    expect(
      screen.getByText(
        "This usually takes 20–40 seconds. You'll land on the finished report automatically.",
      ),
    ).toBeInTheDocument();

    // The parameter <Input> and the chooser are gone — replaced by the composing
    // card, not shown alongside it.
    expect(screen.queryByPlaceholderText("Jun 2026")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/track the last closed period/i)).not.toBeInTheDocument();
    // "Create report" is not part of the composing card.
    expect(screen.queryByRole("button", { name: /create report/i })).not.toBeInTheDocument();
  });

  it("omits the period suffix while composing in tracking mode (no period is known until the compose resolves it)", () => {
    const { rerender } = render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    composeState.isPending = true;
    rerender(<PlaybookLauncher />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Composing Income Statement");
    expect(status.textContent).not.toContain("·");
  });

  it("locks and dims every playbook card while a compose is pending", () => {
    const { rerender } = render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    composeState.isPending = true;
    rerender(<PlaybookLauncher />);

    const card = screen.getByRole("button", { name: /income statement/i });
    expect(card).toBeDisabled();
    expect(card.className).toMatch(/opacity/);
  });

  it("ticks a live elapsed-seconds counter starting at 0", () => {
    const { rerender } = render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    composeState.isPending = true;
    rerender(<PlaybookLauncher />);

    expect(screen.getByText("0s elapsed")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByText("1s elapsed")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(2000));
    expect(screen.getByText("3s elapsed")).toBeInTheDocument();
  });

  it("restores the parameter panel with the inline error when the mutation fails", () => {
    // Pending is only true mid-flight — this exercises the settled/error state, where
    // composePlaybook.isPending has already flipped back to false.
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    chooseSpecificPeriod();
    fireEvent.change(screen.getByPlaceholderText("Jun 2026"), { target: { value: "June 2026" } });
    fireEvent.click(screen.getByRole("button", { name: /create report/i }));

    const [, opts] = mutate.mock.calls[mutate.mock.calls.length - 1];
    act(() => opts.onError(new Error("period must be a NetSuite period name like 'Jun 2026'")));

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("Jun 2026")).toBeInTheDocument();
    expect(screen.getByText("period must be a NetSuite period name like 'Jun 2026'")).toBeInTheDocument();
    // Cards are unlocked again once the compose has settled.
    expect(screen.getByRole("button", { name: /income statement/i })).not.toBeDisabled();
  });

  it("still navigates to the new report on success", () => {
    render(<PlaybookLauncher />);
    fireEvent.click(screen.getByText("Income Statement"));
    chooseSpecificPeriod();
    fireEvent.change(screen.getByPlaceholderText("Jun 2026"), { target: { value: "Jun 2026" } });
    fireEvent.click(screen.getByRole("button", { name: /create report/i }));

    const [, opts] = mutate.mock.calls[mutate.mock.calls.length - 1];
    act(() => opts.onSuccess({ id: "report-123" }));

    expect(routerPush).toHaveBeenCalledWith("/reports/report-123");
  });
});
