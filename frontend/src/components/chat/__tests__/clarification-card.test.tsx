import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ClarificationCard } from "../clarification-card";
import type { ClarificationData } from "@/lib/types";


const _BASE: ClarificationData = {
  type: "clarification",
  status: "pending",
  options: [
    {
      id: "A",
      title: "NetSuite GL",
      rationale: "recognized revenue",
      source: "netsuite",
      is_default: true,
    },
    {
      id: "B",
      title: "BigQuery checkout",
      rationale: "ecommerce totals",
      source: "bigquery",
      is_default: false,
    },
  ],
  default_id: "A",
  ambiguity_summary: "Revenue can mean two things.",
  confirmation_token: "deadbeef".repeat(8),
  expires_at: "2099-01-01T00:00:00Z",
};


describe("ClarificationCard", () => {
  it("renders ambiguity summary", () => {
    render(<ClarificationCard data={_BASE} onChoose={() => {}} />);
    expect(screen.getByText(/Revenue can mean two things/)).toBeInTheDocument();
  });

  it("renders all options as radio buttons", () => {
    render(<ClarificationCard data={_BASE} onChoose={() => {}} />);
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(2);
    expect(screen.getByRole("radio", { name: /NetSuite GL/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /BigQuery checkout/ })).toBeInTheDocument();
  });

  it("highlights default with aria-checked=true", () => {
    render(<ClarificationCard data={_BASE} onChoose={() => {}} />);
    const defaultBtn = screen.getByRole("radio", { name: /NetSuite GL/ });
    expect(defaultBtn.getAttribute("aria-checked")).toBe("true");
    const otherBtn = screen.getByRole("radio", { name: /BigQuery checkout/ });
    expect(otherBtn.getAttribute("aria-checked")).toBe("false");
  });

  it("calls onChoose with option_id when option clicked", () => {
    const onChoose = vi.fn();
    render(<ClarificationCard data={_BASE} onChoose={onChoose} />);
    fireEvent.click(screen.getByRole("radio", { name: /BigQuery checkout/ }));
    expect(onChoose).toHaveBeenCalledWith("B");
  });

  it("renders chosen state collapsed (no radios)", () => {
    const chosen = {
      ..._BASE,
      status: "chosen" as const,
      chosen_id: "A" as const,
    };
    render(<ClarificationCard data={chosen} onChoose={() => {}} />);
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    // Chosen option's title is shown
    expect(screen.getByText(/NetSuite GL/)).toBeInTheDocument();
    // The "chosen" badge / indicator is shown
    expect(screen.getByText(/chosen/i)).toBeInTheDocument();
  });

  it("renders superseded state with replaced indicator", () => {
    const superseded = { ..._BASE, status: "superseded" as const };
    render(<ClarificationCard data={superseded} onChoose={() => {}} />);
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(screen.getByText(/Replaced by your follow-up/i)).toBeInTheDocument();
  });

  it("renders expired state when expired prop is true", () => {
    render(<ClarificationCard data={_BASE} onChoose={() => {}} expired />);
    expect(screen.getByText(/expired/i)).toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  it("supports A/B/C key presses to pick options", () => {
    const onChoose = vi.fn();
    render(<ClarificationCard data={_BASE} onChoose={onChoose} />);
    fireEvent.keyDown(window, { key: "B" });
    expect(onChoose).toHaveBeenCalledWith("B");
  });

  it("supports lowercase a/b/c key presses", () => {
    const onChoose = vi.fn();
    render(<ClarificationCard data={_BASE} onChoose={onChoose} />);
    fireEvent.keyDown(window, { key: "a" });
    expect(onChoose).toHaveBeenCalledWith("A");
  });

  it("Enter key picks the default option", () => {
    const onChoose = vi.fn();
    render(<ClarificationCard data={_BASE} onChoose={onChoose} />);
    fireEvent.keyDown(window, { key: "Enter" });
    expect(onChoose).toHaveBeenCalledWith("A");
  });

  it("ignores key events targeting input/textarea", () => {
    const onChoose = vi.fn();
    render(
      <div>
        <ClarificationCard data={_BASE} onChoose={onChoose} />
        <input data-testid="text-input" />
      </div>
    );
    const input = screen.getByTestId("text-input");
    input.focus();
    fireEvent.keyDown(input, { key: "B" });
    // Did NOT pick option B because key was inside an input
    expect(onChoose).not.toHaveBeenCalled();
  });

  it("free-text hint renders below options", () => {
    render(<ClarificationCard data={_BASE} onChoose={() => {}} />);
    expect(screen.getByText(/just type your answer/i)).toBeInTheDocument();
  });

  it("Enter on a focused option button does not also submit the default", () => {
    // Regression: the global keydown handler treated Enter on any focused
    // element as "pick default", overriding the focused button's native click
    // activation. A keyboard user who tabbed to option B and pressed Enter
    // ended up submitting option A (default) — exactly the wrong outcome.
    const onChoose = vi.fn();
    render(<ClarificationCard data={_BASE} onChoose={onChoose} />);
    const optionB = screen.getByRole("radio", { name: /BigQuery checkout/ });
    optionB.focus();
    // Simulate Enter on the focused button. The global handler should detect
    // a button target and skip its default-Enter shortcut.
    fireEvent.keyDown(optionB, { key: "Enter" });
    // The default option (A) MUST NOT be picked — that would be the bug.
    expect(onChoose).not.toHaveBeenCalledWith("A");
  });

  it("disables all options after first pick (prevents double-click)", () => {
    const onChoose = vi.fn();
    render(<ClarificationCard data={_BASE} onChoose={onChoose} />);
    fireEvent.click(screen.getByRole("radio", { name: /BigQuery checkout/ }));
    // First click registered
    expect(onChoose).toHaveBeenCalledTimes(1);
    // Second click does nothing
    fireEvent.click(screen.getByRole("radio", { name: /NetSuite GL/ }));
    expect(onChoose).toHaveBeenCalledTimes(1);
  });

  // Codex round 5 P3 Bug 3: stale closure in the keydown handler.
  // The effect's deps intentionally omit pendingPick/disabled to avoid
  // re-registering on every state change, so the listener closure holds a
  // stale reference to handlePick. After the first keypress, the React state
  // setter schedules pendingPick=A but the second synchronous keypress's
  // closure still sees pendingPick=null and fires onChoose("B"). Result: a
  // second resume request → backend 409 already_resolved → spurious toast.
  // Fix uses a ref to short-circuit re-entry without changing deps.
  it("keyboard picks only call onChoose once until response (no stale-closure race)", () => {
    const onChoose = vi.fn();
    render(<ClarificationCard data={_BASE} onChoose={onChoose} />);
    // Two synchronous keypresses, before parent has had any chance to update
    // pendingPick/disabled props. Stale closure would fire both.
    fireEvent.keyDown(window, { key: "A" });
    fireEvent.keyDown(window, { key: "B" });
    expect(onChoose).toHaveBeenCalledTimes(1);
    expect(onChoose).toHaveBeenCalledWith("A");
  });

  // Codex round 10 P3 Bug 3: handlePick sets pendingPick + pickingRef
  // synchronously, calls onChoose, but never resets these on failure. If
  // onChoose throws or its async resume POST rejects, the card is dead:
  //   - pickingRef stays true → next keypress is ignored
  //   - pendingPick stays set → buttons remain disabled
  // Fix: await onChoose, reset state in a try/catch on failure. On success
  // the parent unmounts/updates the message state, so we leave pendingPick
  // set — the parent owns the post-success transition.
  it("resets pending state when onChoose throws (so user can retry)", async () => {
    const error = new Error("network timeout");
    const onChoose = vi.fn(() => {
      throw error;
    });
    render(<ClarificationCard data={_BASE} onChoose={onChoose} />);

    fireEvent.click(screen.getByRole("radio", { name: /BigQuery checkout/ }));
    expect(onChoose).toHaveBeenCalledTimes(1);

    // Wait for the rejection to settle and the card to reset.
    await waitFor(() => {
      const optionA = screen.getByRole("radio", { name: /NetSuite GL/ });
      expect((optionA as HTMLButtonElement).disabled).toBe(false);
    });

    // After reset, a NEW pick should fire onChoose again (pickingRef cleared).
    fireEvent.keyDown(window, { key: "A" });
    expect(onChoose).toHaveBeenCalledTimes(2);
    expect(onChoose).toHaveBeenLastCalledWith("A");
  });

  it("resets pending state when onChoose returns a rejected promise", async () => {
    const onChoose = vi.fn(() => Promise.reject(new Error("server 500")));
    render(<ClarificationCard data={_BASE} onChoose={onChoose} />);

    fireEvent.click(screen.getByRole("radio", { name: /BigQuery checkout/ }));
    expect(onChoose).toHaveBeenCalledTimes(1);

    await waitFor(() => {
      const optionA = screen.getByRole("radio", { name: /NetSuite GL/ });
      expect((optionA as HTMLButtonElement).disabled).toBe(false);
    });

    // Retry must work
    fireEvent.keyDown(window, { key: "B" });
    expect(onChoose).toHaveBeenCalledTimes(2);
    expect(onChoose).toHaveBeenLastCalledWith("B");
  });

  it("does NOT reset on success (parent owns post-success transition)", async () => {
    // Successful pick: card stays in pending UI state until the parent
    // re-renders with status="chosen" (or unmounts the card). The card
    // itself MUST NOT clear pendingPick on success — that would briefly
    // re-enable the buttons and let a double-click fire onChoose twice.
    const onChoose = vi.fn(() => Promise.resolve());
    render(<ClarificationCard data={_BASE} onChoose={onChoose} />);

    fireEvent.click(screen.getByRole("radio", { name: /BigQuery checkout/ }));
    expect(onChoose).toHaveBeenCalledTimes(1);

    // Even after the resolved promise is settled, options remain disabled
    // and additional clicks/keypresses do NOT fire onChoose again.
    await Promise.resolve();
    await Promise.resolve();
    fireEvent.keyDown(window, { key: "A" });
    expect(onChoose).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// Manual clarification input (dogfood follow-up 2026-04-30): when the parent
// passes onManualClarify, the card renders a textarea + Send button below
// the A/B/C options. Submitting calls onManualClarify(text.trim()) and the
// card transitions through pending → resolved (parent flips status).
// ---------------------------------------------------------------------------

describe("ClarificationCard manual clarify", () => {
  it("renders manual textarea + Send button when onManualClarify is provided", () => {
    render(
      <ClarificationCard data={_BASE} onChoose={() => {}} onManualClarify={async () => {}} />,
    );
    expect(screen.getByPlaceholderText(/clarify/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /send/i })).toBeInTheDocument();
  });

  it("does NOT render textarea when onManualClarify is omitted (back-compat)", () => {
    render(<ClarificationCard data={_BASE} onChoose={() => {}} />);
    expect(screen.queryByPlaceholderText(/clarify/i)).not.toBeInTheDocument();
  });

  it("calls onManualClarify with trimmed text when Send is clicked", async () => {
    const onManualClarify = vi.fn(async () => {});
    render(
      <ClarificationCard data={_BASE} onChoose={() => {}} onManualClarify={onManualClarify} />,
    );

    const textarea = screen.getByPlaceholderText(/clarify/i);
    fireEvent.change(textarea, { target: { value: "  fiscal Q1 only  " } });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(onManualClarify).toHaveBeenCalledWith("fiscal Q1 only"));
  });

  it("disables Send when textarea is empty/whitespace", () => {
    const onManualClarify = vi.fn(async () => {});
    render(
      <ClarificationCard data={_BASE} onChoose={() => {}} onManualClarify={onManualClarify} />,
    );
    const sendBtn = screen.getByRole("button", { name: /send/i }) as HTMLButtonElement;
    expect(sendBtn.disabled).toBe(true);

    const textarea = screen.getByPlaceholderText(/clarify/i);
    fireEvent.change(textarea, { target: { value: "   " } });
    expect(sendBtn.disabled).toBe(true);

    fireEvent.change(textarea, { target: { value: "fiscal Q1" } });
    expect(sendBtn.disabled).toBe(false);
  });

  it("clears textarea after a successful submit", async () => {
    const onManualClarify = vi.fn(async () => {});
    render(
      <ClarificationCard data={_BASE} onChoose={() => {}} onManualClarify={onManualClarify} />,
    );
    const textarea = screen.getByPlaceholderText(/clarify/i) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "fiscal Q1" } });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(onManualClarify).toHaveBeenCalled());
    await waitFor(() => expect(textarea.value).toBe(""));
  });

  it("does not call onChoose when manual Send is clicked", async () => {
    const onChoose = vi.fn();
    const onManualClarify = vi.fn(async () => {});
    render(
      <ClarificationCard data={_BASE} onChoose={onChoose} onManualClarify={onManualClarify} />,
    );
    const textarea = screen.getByPlaceholderText(/clarify/i);
    fireEvent.change(textarea, { target: { value: "fiscal Q1" } });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(onManualClarify).toHaveBeenCalled());
    expect(onChoose).not.toHaveBeenCalled();
  });

  it("preserves textarea content if submit rejects (so user can retry)", async () => {
    const onManualClarify = vi.fn(async () => {
      throw new Error("network");
    });
    render(
      <ClarificationCard data={_BASE} onChoose={() => {}} onManualClarify={onManualClarify} />,
    );
    const textarea = screen.getByPlaceholderText(/clarify/i) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "fiscal Q1" } });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(onManualClarify).toHaveBeenCalled());
    // Textarea content stays so the user can retry without retyping
    expect(textarea.value).toBe("fiscal Q1");
  });
});
