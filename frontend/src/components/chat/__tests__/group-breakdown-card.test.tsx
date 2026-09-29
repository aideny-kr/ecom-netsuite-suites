import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import { isGroupBreakdown, normalizeStreamEvent, type GroupBreakdownData } from "@/lib/chat-stream";
import { GroupBreakdownCard } from "../group-breakdown-card";

// Framework Inc "Order differences", 2026-09-29 (saved evidence + saved invoice reads).
const breakdown: GroupBreakdownData = {
  group_id: "7d4faf52d9cfd2a174d90da2aa580e6f",
  case_id: null,
  scope: { review_run_ids: ["run-a"], status: "needs_review", search: "" },
  pattern: "Order differences",
  currency: "USD",
  orders: 46,
  totals: { order_total: "-120206.11", tax: "0.00", refunds: "0.00" },
  causes: [
    {
      cause: "source_adjustment_not_in_netsuite",
      label: "Solidus adjustment never reached NetSuite",
      why: "Each order has a manual Solidus adjustment equal to the difference to the cent.",
      next_step: "fix_at_source",
      next_pill: "Fix at the source",
      next_label: "The order sync must carry Solidus order adjustments.",
      primary: { metric: "order_total", amount: "-90008.99" },
      orders: 21,
      order_references: ["R290684941", "R805763363", "R143085668", "R190994976"],
      amounts: { order_total: "-90008.99", tax: "0.00", refunds: "0.00", open_on_invoices: "27373.16" },
      facts: [{ fact: '"SKU Adjustment"', orders: 8 }],
    },
    {
      cause: "tax_left_after_credit",
      label: "Refund credit left the tax in place",
      why: "A credit memo refunded the order, but NetSuite still carries the tax.",
      next_step: "prepare_corrections",
      next_pill: "Group fix",
      next_label: "Run the group fix.",
      primary: { metric: "tax", amount: "-7.63" },
      orders: 1,
      order_references: ["R619946522"],
      case_ids: ["c-619"],
      amounts: { order_total: "-7.63", tax: "-7.63", refunds: "0.00" },
      facts: [],
    },
    {
      cause: "corrected_in_app",
      label: "Already corrected here, still shown as open",
      why: "Each order has an approved correction that the app verified.",
      next_step: "settings_change",
      next_pill: "Settings change",
      next_label: "Count refund reason 4 as a tax refund for this subsidiary. A person approves it.",
      primary: { metric: "order_total", amount: "-1002.83" },
      orders: 20,
      order_references: ["R431430593"],
      amounts: { order_total: "-1002.83", tax: "-1002.83", refunds: "0.00" },
      facts: [{ fact: "refund reason 4", orders: 20 }],
    },
    {
      cause: "no_shared_cause",
      label: "No shared cause",
      why: "No rule explains these orders.",
      next_step: "review_individually",
      next_pill: "Review",
      next_label: "Review one by one.",
      primary: { metric: "order_total", amount: "-1345.37" },
      orders: 4,
      order_references: ["R274027840", "R622812723", "R293712421", "R547556184"],
      case_ids: ["c-274", "c-622", "c-293", "c-547"],
      amounts: { order_total: "-1345.37", tax: "0.00", refunds: "0.00", open_on_invoices: "0.00" },
      facts: [{ fact: "no saved Solidus detail yet", orders: 2 }],
    },
  ],
  checked: { saved_evidence: 46, saved_source_orders: 44, netsuite: "timed_out", netsuite_orders: 0, seconds: 7.2 },
};

function compose(link: HTMLElement) {
  return new URL(link.getAttribute("href")!, "https://example.test").searchParams.get("compose")!;
}

it("shows each cause with the server's counts and amounts", () => {
  render(<GroupBreakdownCard data={breakdown} />);
  expect(screen.getByText("Order differences")).toBeVisible();
  expect(screen.getByText("4 causes found")).toBeVisible();
  expect(screen.getByText(/NetSuite higher by/)).toHaveTextContent("$120,206.11");
  const row = screen.getByText("Solidus adjustment never reached NetSuite").closest("li")!;
  expect(within(row).getByText("21")).toBeVisible();
  expect(within(row).getByText("$90,008.99")).toBeVisible();
  expect(within(row).getByText("open $27,373.16")).toBeVisible();
  expect(within(row).getByText("Fix at the source")).toBeVisible();
  expect(within(row).getByText(/\+1 more/)).toBeVisible();
  // The server names the amount each cause shows; the card never guesses.
  const tax = screen.getByText("Refund credit left the tax in place").closest("li")!;
  expect(within(tax).getByText("$7.63")).toBeVisible();
  expect(within(tax).getByText("Group fix")).toBeVisible();
});

it("routes each next step to the right action", () => {
  render(<GroupBreakdownCard data={breakdown} />);
  // Packet review F3: each order goes with its exact case, which the case tools need.
  const review = compose(screen.getByRole("link", { name: "Review one by one →" }));
  expect(review).toContain("R274027840 (case c-274)");
  expect(review).toContain("R547556184 (case c-547)");
  // One order of 46: prepare that order, never the whole mixed group (review round 1 of #356).
  const fix = compose(screen.getByRole("link", { name: "Prepare these fixes →" }));
  expect(fix).toContain("R619946522 (case c-619)");
  expect(fix).toContain("transaction_ops_accounting_evidence");
  expect(fix).not.toContain("transaction_ops.accounting_group");
  expect(screen.getByRole("button", { name: "Propose settings change" })).toBeDisabled();
});

it("says when the NetSuite check was skipped", () => {
  render(<GroupBreakdownCard data={breakdown} />);
  expect(screen.getByText(/NetSuite invoice check timed out/)).toBeVisible();
});

it("keeps a breakdown as its own stream event and drops a malformed one", () => {
  expect(normalizeStreamEvent({ type: "group_breakdown", data: breakdown })).toEqual({
    type: "group_breakdown",
    data: breakdown,
  });
  expect(normalizeStreamEvent({ type: "group_breakdown", data: { ...breakdown, causes: "nope" } })).toBeNull();
  const broken = { ...breakdown, causes: [{ ...breakdown.causes[0], amounts: undefined }] };
  expect(isGroupBreakdown(broken)).toBe(false);
});

it("sends a cause that covers the whole group to the group fix with the exact scope", () => {
  const whole: GroupBreakdownData = {
    ...breakdown,
    orders: 1,
    causes: [breakdown.causes[1]],
  };
  render(<GroupBreakdownCard data={whole} />);
  const fix = compose(screen.getByRole("link", { name: "Prepare these fixes →" }));
  expect(fix).toContain('group_id "7d4faf52d9cfd2a174d90da2aa580e6f"');
  expect(fix).toContain('"review_run_ids":["run-a"]');
  expect(fix).not.toContain('"search"');
});

it("offers the fix for a single order too", () => {
  const single: GroupBreakdownData = { ...breakdown, group_id: null, case_id: "c1", scope: null, orders: 1, causes: [breakdown.causes[1]] };
  render(<GroupBreakdownCard data={single} />);
  expect(compose(screen.getByRole("link", { name: "Prepare these fixes →" }))).toContain("R619946522");
});

it("shows every non-zero amount of a cause, not just the one it leads with", () => {
  // Review round 2 of #356: the model is told every amount is on the card, so it must be.
  const mixed: GroupBreakdownData = {
    ...breakdown,
    causes: [
      {
        ...breakdown.causes[0],
        amounts: { order_total: "-100.00", tax: "-10.00", refunds: "25.00" },
        primary: { metric: "order_total", amount: "-100.00" },
      },
    ],
  };
  render(<GroupBreakdownCard data={mixed} />);
  const row = screen.getByText("Solidus adjustment never reached NetSuite").closest("li")!;
  expect(within(row).getByText("$100.00")).toBeVisible();
  expect(within(row).getByText("tax $10.00")).toBeVisible();
  expect(within(row).getByText("refunds $25.00")).toBeVisible();
});

it("says when the saved Solidus orders could not be read", () => {
  render(<GroupBreakdownCard data={{ ...breakdown, checked: { ...breakdown.checked, saved_source: "unavailable" } }} />);
  expect(screen.getByText(/saved Solidus orders could not be read/)).toBeVisible();
});

it("shows an unknown amount as unknown, never as zero", () => {
  // Packet review F2: the card used to show a sum with a missing amount as a complete $0.00.
  const unknown: GroupBreakdownData = {
    ...breakdown,
    totals: { order_total: null as unknown as string, tax: null as unknown as string, refunds: "0.00" },
    causes: [
      {
        ...breakdown.causes[3],
        amounts: { order_total: null as unknown as string, tax: null as unknown as string, refunds: "0.00" },
        primary: null as unknown as { metric: string; amount: string },
      },
    ],
  };
  render(<GroupBreakdownCard data={unknown} />);
  const row = screen.getByText("No shared cause").closest("li")!;
  expect(within(row).getByText("—")).toBeVisible();
  expect(within(row).getByText("tax unknown")).toBeVisible();
  expect(screen.queryByText(/\$0\.00/)).toBeNull();
  expect(screen.getByText(/difference not fully known/)).toBeVisible();
});
