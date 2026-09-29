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
      next_label: "The order sync must carry Solidus order adjustments.",
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
      next_label: "Run the group fix.",
      orders: 1,
      order_references: ["R619946522"],
      amounts: { order_total: "-7.63", tax: "-7.63", refunds: "0.00" },
      facts: [],
    },
    {
      cause: "corrected_in_app",
      label: "Already corrected here, still shown as open",
      why: "Each order has an approved correction that the app verified.",
      next_step: "settings_change",
      next_label: "Count refund reason 4 as a tax refund for this subsidiary. A person approves it.",
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
      next_label: "Review one by one.",
      orders: 4,
      order_references: ["R274027840", "R622812723", "R293712421", "R547556184"],
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
  // A tax-only cause shows its tax amount rather than a zero.
  const tax = screen.getByText("Refund credit left the tax in place").closest("li")!;
  expect(within(tax).getByText("$7.63")).toBeVisible();
});

it("routes each next step to the right action", () => {
  render(<GroupBreakdownCard data={breakdown} />);
  const review = compose(screen.getByRole("link", { name: "Review one by one →" }));
  expect(review).toContain("R274027840, R622812723, R293712421, R547556184");
  const fix = compose(screen.getByRole("link", { name: "Prepare these fixes →" }));
  expect(fix).toContain('group_id "7d4faf52d9cfd2a174d90da2aa580e6f"');
  expect(fix).toContain('"review_run_ids":["run-a"]');
  expect(fix).not.toContain('"search"');
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
