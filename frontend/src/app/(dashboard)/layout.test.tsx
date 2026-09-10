import React from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import DashboardLayout from "./layout";
const state = vi.hoisted(() => ({
  mobile: true,
  listener: undefined as (() => void) | undefined,
}));
vi.mock("@/providers/auth-provider", () => ({
  useAuth: () => ({
    user: { id: "user", onboarding_completed_at: "done" },
    isLoading: false,
    refreshUser: vi.fn(),
  }),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/tables/orders",
}));
vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn().mockResolvedValue({ valid: true }) },
}));
vi.mock("@/components/sidebar", () => ({
  Sidebar: ({
    collapsed,
    onToggle,
  }: {
    collapsed: boolean;
    onToggle: () => void;
  }) => (
    <aside data-testid="sidebar" data-collapsed={String(collapsed)}>
      <button onClick={onToggle}>Collapse sidebar</button>
    </aside>
  ),
}));
vi.mock("@/components/connection-alert-banner", () => ({
  ConnectionAlertBanner: () => null,
}));
vi.mock("@/components/onboarding/onboarding-wizard", () => ({
  OnboardingWizard: () => null,
}));
beforeEach(() => {
  state.mobile = true;
  state.listener = undefined;
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn(() => ({
      get matches() {
        return state.mobile;
      },
      addEventListener: (_name: string, fn: () => void) => {
        state.listener = fn;
      },
      removeEventListener: vi.fn(),
    })),
  });
});
it("collapses navigation on a narrow viewport and keeps navigation reopenable", () => {
  render(
    <DashboardLayout>
      <h1>Transactions</h1>
    </DashboardLayout>,
  );
  expect(screen.getByTestId("sidebar")).toHaveAttribute(
    "data-collapsed",
    "true",
  );
  fireEvent.click(screen.getByRole("button", { name: "Open sidebar" }));
  expect(screen.getByTestId("sidebar")).toHaveAttribute(
    "data-collapsed",
    "false",
  );
});
it("reacts when an open desktop viewport becomes narrow", () => {
  state.mobile = false;
  render(
    <DashboardLayout>
      <h1>Transactions</h1>
    </DashboardLayout>,
  );
  expect(screen.getByTestId("sidebar")).toHaveAttribute(
    "data-collapsed",
    "false",
  );
  act(() => {
    state.mobile = true;
    state.listener?.();
  });
  expect(screen.getByTestId("sidebar")).toHaveAttribute(
    "data-collapsed",
    "true",
  );
});
