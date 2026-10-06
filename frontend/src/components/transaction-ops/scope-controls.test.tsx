import React from "react";
import { expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ScopeControls } from "./scope-controls";
const mutate = vi.hoisted(() => vi.fn().mockResolvedValue({}));
const readiness = vi.hoisted(() => ({
  data: {
    ready: true,
    renewal_needed: false,
    review_by: null as string | null,
  },
  isPending: false,
  isError: false,
}));
vi.mock("@/hooks/use-transaction-ops", () => ({
  useTransactionAccess: () => ({ canManage: true }),
}));
vi.mock("@/hooks/use-transaction-setup", () => ({
  useScheduleReadiness: () => readiness,
  useControlTransactionConfig: () => ({
    mutateAsync: mutate,
    isPending: false,
  }),
}));
it("blocks activation and explains renewal while allowing pause", () => {
  readiness.data = {
    ready: false,
    renewal_needed: true,
    review_by: "2026-10-06T16:00:00Z",
  };
  const view = render(
    <ScopeControls
      config={{ id: "stale", enabled: true, schedule_enabled: false }}
    />,
  );
  expect(
    screen.getByRole("button", { name: "Enable schedule" }),
  ).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent(
    "Scheduled reads are blocked",
  );
  expect(screen.getByRole("status")).toHaveTextContent("successor scope");
  expect(screen.getByRole("button", { name: "Pause scope" })).toBeEnabled();
  view.rerender(
    <ScopeControls
      config={{ id: "stale", enabled: true, schedule_enabled: true }}
    />,
  );
  expect(screen.getByRole("button", { name: "Pause schedule" })).toBeEnabled();
  readiness.data = { ready: true, renewal_needed: false, review_by: null };
});
it("pauses a scope and its schedule together without changing mapping", async () => {
  render(
    <ScopeControls
      config={{ id: "scope", enabled: true, schedule_enabled: true }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Pause scope" }));
  await waitFor(() =>
    expect(mutate).toHaveBeenCalledWith({
      id: "scope",
      enabled: false,
      schedule_enabled: false,
    }),
  );
});
