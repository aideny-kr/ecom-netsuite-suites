/**
 * The workspace hosts two surfaces, and they must stay mutually exclusive.
 *
 * "files" is an edit-and-deploy surface: file tree, diff viewer, changesets, and
 * a Deploy button. "celigo" is a read-only view of the customer's integrator.io
 * configuration -- those scripts belong to the customer's own integrators, and
 * the entire Celigo connector is built so nothing can modify them.
 *
 * The risk this file exists to pin: if the Celigo surface rendered ALONGSIDE the
 * panel group rather than INSTEAD OF it, the Deploy button would still be in the
 * tree -- hidden by a class, reachable by a keyboard user, and one refactor away
 * from being visible. Asserting "not visible" would not catch that. These tests
 * assert the deploy affordances are NOT MOUNTED AT ALL.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const mockUseFeature = vi.fn();
vi.mock("@/hooks/use-features", () => ({
  useFeature: (name: string) => mockUseFeature(name),
}));

// The flow map has its own dedicated suite; here it only needs to be locatable.
vi.mock("@/components/settings/celigo-flow-map", () => ({
  CeligoFlowMap: () => <div data-testid="celigo-flow-map">flow map</div>,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

describe("workspace surfaces", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseFeature.mockImplementation((name: string) => name === "celigo");
  });

  it("exposes a Celigo flows surface only when the feature flag is on", async () => {
    const { CeligoSurfaceToggle } = await import("../surface-toggle");
    const { rerender } = render(
      <CeligoSurfaceToggle surface="files" onChange={vi.fn()} enabled={true} />,
    );
    expect(screen.getByRole("button", { name: /celigo flows/i })).toBeTruthy();

    rerender(<CeligoSurfaceToggle surface="files" onChange={vi.fn()} enabled={false} />);
    expect(screen.queryByRole("button", { name: /celigo flows/i })).toBeNull();
  });

  it("reports which surface is active so the page can render one or the other", async () => {
    const { CeligoSurfaceToggle } = await import("../surface-toggle");
    const onChange = vi.fn();
    render(<CeligoSurfaceToggle surface="files" onChange={onChange} enabled={true} />);

    const celigoBtn = screen.getByRole("button", { name: /celigo flows/i });
    expect(celigoBtn.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(celigoBtn);
    expect(onChange).toHaveBeenCalledWith("celigo");
  });

  it("marks the active surface for assistive tech, not just visually", async () => {
    const { CeligoSurfaceToggle } = await import("../surface-toggle");
    render(<CeligoSurfaceToggle surface="celigo" onChange={vi.fn()} enabled={true} />);

    expect(
      screen.getByRole("button", { name: /celigo flows/i }).getAttribute("aria-pressed"),
    ).toBe("true");
    expect(screen.getByRole("button", { name: /^files$/i }).getAttribute("aria-pressed")).toBe(
      "false",
    );
  });
});
