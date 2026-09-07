import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

// Task 3 — the "Flow map | Scripts" segmented toggle `celigo-surface.tsx`
// grows on top of its existing flow/integration/integrations-index switch
// (Task 9/10/12/14), plus routing to the new Scripts view when active
// (spec §3.2). Sub-pages and the command palette are mocked to lightweight
// stubs (Task 10's established pattern, `celigo-integration-page.test.tsx`)
// so this file exercises ONLY the surface's own switch and toggle, not any
// sub-page's internals.

vi.mock("../celigo-integrations-page", () => ({
  CeligoIntegrationsPage: () => <div data-testid="integrations-page" />,
}));
vi.mock("../celigo-integration-page", () => ({
  CeligoIntegrationPage: () => <div data-testid="integration-page" />,
}));
vi.mock("../celigo-flow-page", () => ({
  CeligoFlowPage: () => <div data-testid="flow-page" />,
}));
vi.mock("../celigo-command-palette", () => ({
  CeligoCommandPalette: () => <div data-testid="command-palette" />,
}));
vi.mock("../scripts/celigo-scripts-page", () => ({
  CeligoScriptsPage: () => <div data-testid="scripts-page" />,
}));

const routeMocks = vi.hoisted(() => ({
  tab: "flows" as "flows" | "scripts" | "errors" | "changes",
  integrationId: null as string | null,
  flowId: null as string | null,
  go: {
    files: vi.fn(),
    integrations: vi.fn(),
    integration: vi.fn(),
    tab: vi.fn(),
    view: vi.fn(),
    flow: vi.fn(),
    step: vi.fn(),
    script: vi.fn(),
    scripts: vi.fn(),
  },
}));

// `isScriptsView` is real (imported via `importActual`) rather than
// re-derived here — the surface must use the SAME predicate the route
// module exports (single source of truth for "what counts as the Scripts
// view"), not a copy that could drift.
vi.mock("../celigo-route", async () => {
  const actual = await vi.importActual<typeof import("../celigo-route")>("../celigo-route");
  return {
    ...actual,
    useCeligoRoute: () => ({
      surface: "celigo" as const,
      view: "tiles" as const,
      integrationId: routeMocks.integrationId,
      tab: routeMocks.tab,
      flowId: routeMocks.flowId,
      stepId: null,
      scriptId: null,
      scriptSite: null,
      familyKey: null,
      copyId: null,
      compare: null,
      scriptsFilter: "all" as const,
      scriptsKind: null,
      scriptsIntegrationId: null,
      q: "",
      go: routeMocks.go,
    }),
  };
});

import { CeligoSurface } from "../celigo-surface";

beforeEach(() => {
  routeMocks.tab = "flows";
  routeMocks.integrationId = null;
  routeMocks.flowId = null;
  Object.values(routeMocks.go).forEach((fn) => fn.mockReset());
});

describe("CeligoSurface — Flow map | Scripts toggle", () => {
  it("renders a role=group toggle whose aria-pressed reflects the route", () => {
    render(<CeligoSurface />);
    const group = screen.getByRole("group", { name: /flow map.*scripts/i });
    expect(within(group).getByRole("button", { name: "Flow map" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(group).getByRole("button", { name: "Scripts" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("Scripts active (tab=scripts, no integration/flow) mounts the scripts page shell", () => {
    routeMocks.tab = "scripts";
    render(<CeligoSurface />);
    expect(screen.getByTestId("scripts-page")).toBeInTheDocument();
    expect(screen.queryByTestId("integrations-page")).not.toBeInTheDocument();
    const group = screen.getByRole("group", { name: /flow map.*scripts/i });
    expect(within(group).getByRole("button", { name: "Scripts" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(group).getByRole("button", { name: "Flow map" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("integration=X&tab=scripts is NOT the Scripts view — stays the integration page, toggle reads Flow map", () => {
    routeMocks.tab = "scripts";
    routeMocks.integrationId = "int-1";
    render(<CeligoSurface />);
    expect(screen.getByTestId("integration-page")).toBeInTheDocument();
    expect(screen.queryByTestId("scripts-page")).not.toBeInTheDocument();
    const group = screen.getByRole("group", { name: /flow map.*scripts/i });
    expect(within(group).getByRole("button", { name: "Flow map" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("a flow selected (even with tab=scripts) stays on the flow page, toggle reads Flow map", () => {
    routeMocks.tab = "scripts";
    routeMocks.flowId = "flow-1";
    render(<CeligoSurface />);
    expect(screen.getByTestId("flow-page")).toBeInTheDocument();
  });

  it("clicking Scripts navigates there via go.scripts()", () => {
    render(<CeligoSurface />);
    fireEvent.click(screen.getByRole("button", { name: "Scripts" }));
    expect(routeMocks.go.scripts).toHaveBeenCalledWith();
    expect(routeMocks.go.integrations).not.toHaveBeenCalled();
  });

  it("clicking Flow map from the Scripts view navigates to the integrations list", () => {
    routeMocks.tab = "scripts";
    render(<CeligoSurface />);
    fireEvent.click(screen.getByRole("button", { name: "Flow map" }));
    expect(routeMocks.go.integrations).toHaveBeenCalledWith();
    expect(routeMocks.go.scripts).not.toHaveBeenCalled();
  });

  it("plain integrations index (no integration/flow/scripts) still renders as before", () => {
    render(<CeligoSurface />);
    expect(screen.getByTestId("integrations-page")).toBeInTheDocument();
  });
});
