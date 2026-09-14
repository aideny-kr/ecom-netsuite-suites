import React from "react";
import { expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ScopeControls } from "./scope-controls";
const mutate = vi.hoisted(() => vi.fn().mockResolvedValue({}));
vi.mock("@/hooks/use-transaction-ops", () => ({
  useTransactionAccess: () => ({ canManage: true }),
}));
vi.mock("@/hooks/use-transaction-setup", () => ({
  useControlTransactionConfig: () => ({
    mutateAsync: mutate,
    isPending: false,
  }),
}));
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
