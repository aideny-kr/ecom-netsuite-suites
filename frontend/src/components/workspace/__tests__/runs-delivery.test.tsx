import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RunsPanel } from "../runs-panel";
import type { WorkspaceRun } from "@/lib/types";

const mock = vi.hoisted(() => ({ deliver: vi.fn(), allowed: true, pending: false }));
vi.mock("@/hooks/use-permissions", () => ({ usePermissions: () => ({ hasPermission: () => mock.allowed }) }));
vi.mock("@/hooks/use-runs", () => ({
  useRunArtifacts: () => ({ data: [] }),
  useTriggerValidate: () => ({ mutate: vi.fn() }),
  useDeliverQueuedDeploy: () => ({ mutate: mock.deliver, isPending: false, data: { dispatch_pending: mock.pending } }),
}));

const run = { id: "saved-run", run_type: "deploy_sandbox", status: "queued", duration_ms: null } as WorkspaceRun;
beforeEach(() => { mock.deliver.mockReset(); mock.allowed = true; mock.pending = false; });

describe("confirmed deployment delivery", () => {
  it("redelivers the same saved run and explains unavailable delivery", () => {
    mock.pending = true;
    render(<RunsPanel runs={[run]} />);
    fireEvent.click(screen.getByText("Sandbox Deploy"));
    fireEvent.click(screen.getByRole("button", { name: "Retry delivery" }));
    expect(mock.deliver).toHaveBeenCalledWith("saved-run");
    expect(screen.getByRole("status")).toHaveTextContent("confirmed run is saved");
  });
  it.each(["running", "passed", "error"] as const)("never offers replay for a %s run", (status) => {
    render(<RunsPanel runs={[{ ...run, status }]} />);
    fireEvent.click(screen.getByText("Sandbox Deploy"));
    expect(screen.queryByRole("button", { name: "Retry delivery" })).not.toBeInTheDocument();
  });
  it("does not show delivery controls without permission", () => {
    mock.allowed = false;
    render(<RunsPanel runs={[run]} />);
    fireEvent.click(screen.getByText("Sandbox Deploy"));
    expect(screen.queryByRole("button", { name: "Retry delivery" })).not.toBeInTheDocument();
  });
});
