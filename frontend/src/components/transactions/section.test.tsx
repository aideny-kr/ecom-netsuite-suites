import React from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { TransactionsSection } from "./section";
const state = vi.hoisted(() => ({ path: "/tables/orders", query: "view=records", orders: true, payments: true, error: null as Error | null }));
vi.mock("next/navigation", () => ({ usePathname: () => state.path, useSearchParams: () => new URLSearchParams(state.query) }));
vi.mock("@/hooks/use-transaction-ops", () => ({ useTransactionAccess: () => ({ allowed: state.orders, loading: false, error: state.error }) }));
vi.mock("@/hooks/use-features", () => ({ useFeatures: () => ({ data: { reconciliation: state.payments } }) }));
vi.mock("@/hooks/use-permissions", () => ({ usePermissions: () => ({ hasPermission: () => state.payments }) }));
beforeEach(() => { state.path = "/tables/orders"; state.query = "view=records"; state.orders = true; state.payments = true; state.error = null; });
it("preserves existing URL context while switching order views", () => {
  state.query = "view=cases&source=abc";
  render(<TransactionsSection>Evidence</TransactionsSection>);
  const nav = within(screen.getByRole("navigation", {name:"Transaction sections"}));
  expect(nav.getByRole("link", {name:"Cases"})).toHaveAttribute("aria-current", "page");
  expect(nav.getByRole("link", {name:"Approvals"})).toHaveAttribute("href", "/tables/orders?view=approvals&source=abc");
  expect(screen.getByRole("link", {name:"Investigations"})).toHaveAttribute("href", "/transaction-operations");
});
it("keeps payment approvals and history reachable without Celigo order access", () => {
  state.orders = false; state.path = "/reconciliation"; state.query = "view=approvals";
  render(<TransactionsSection>Payment controls</TransactionsSection>);
  const nav = within(screen.getByRole("navigation", {name:"Transaction sections"}));
  expect(nav.getByRole("link", {name:"Approvals"})).toHaveAttribute("aria-current", "page");
  expect(nav.getByRole("link", {name:"History"})).toHaveAttribute("href", "/reconciliation?view=history");
  expect(nav.queryByRole("link", {name:"Cases"})).not.toBeInTheDocument();
  expect(screen.getByText("Payment controls")).toBeInTheDocument();
});
it("does not mount payment controls when access is unavailable", () => {
  state.path = "/reconciliation"; state.payments = false; state.orders = false;
  const child = vi.fn(() => <button>Approve match</button>);
  const Child = child;
  render(<TransactionsSection><Child /></TransactionsSection>);
  expect(child).not.toHaveBeenCalled();
  expect(screen.getByRole("status")).toHaveTextContent("requires reconciliation");
  expect(screen.getByRole("link", {name:"Records"})).toBeInTheDocument();
});
