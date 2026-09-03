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
 *
 * Task 9 adds: the surface itself is now URL-driven (`?surface=celigo`) rather
 * than component `useState`, so a second pair of tests below renders the whole
 * `WorkspacePage` and checks the real switch — flag-on + the URL param mounts
 * `CeligoSurface`; flag-off ignores the URL param and stays on Files.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: nav.push, replace: nav.replace }),
  useSearchParams: () => nav.params,
  usePathname: () => "/workspace",
}));

const features = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/use-features", () => ({
  useFeature: () => features(),
}));

// The Celigo surface has its own dedicated test suite (Tasks 10/12/14 as they
// land); here it only needs to be locatable, so the page tests below don't
// also depend on whatever it renders internally.
vi.mock("@/components/celigo/celigo-surface", () => ({
  CeligoSurface: () => <div data-testid="celigo-surface" />,
}));

// Every hook `page.tsx` calls at module scope, mocked to the minimal shape
// the page destructures — none of these tests select a workspace, so no
// hook's data ever needs to be more than empty/undefined.
vi.mock("@/hooks/use-workspace", () => ({
  useWorkspaces: () => ({ data: [] }),
  useCreateWorkspace: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useWorkspaceFiles: () => ({ data: [] }),
  useFileContent: () => ({ data: undefined }),
  useSearchFiles: () => ({ data: undefined }),
}));
vi.mock("@/hooks/use-changesets", () => ({
  useChangesets: () => ({ data: [] }),
  useChangesetDiff: () => ({ data: undefined }),
}));
vi.mock("@/hooks/use-runs", () => ({
  useRuns: () => ({ data: [] }),
}));
vi.mock("@/hooks/use-ai-settings", () => ({
  useAiSettings: () => ({ data: undefined }),
}));
vi.mock("@/hooks/use-connections", () => ({
  useConnections: () => ({ data: [] }),
}));
vi.mock("@/hooks/use-mcp-connectors", () => ({
  useMcpConnectors: () => ({ data: [] }),
}));
vi.mock("@/hooks/use-suitescript-sync", () => ({
  useSuiteScriptSyncStatus: () => ({ data: undefined }),
  useTriggerSuiteScriptSync: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/use-netsuite-api-logs", () => ({
  useNetSuiteApiLogs: () => ({ data: [] }),
}));
vi.mock("@/hooks/use-netsuite-file-ops", () => ({
  usePullFile: () => ({ mutate: vi.fn(), isPending: false }),
  usePushFile: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/use-mock-data", () => ({
  useMockData: () => ({ mutateAsync: vi.fn(), isPending: false, data: undefined, error: null }),
}));

import WorkspacePage from "../page";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

beforeEach(() => {
  vi.clearAllMocks();
  nav.push.mockReset();
  nav.replace.mockReset();
  nav.params = new URLSearchParams();
});

describe("workspace surfaces", () => {
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

  it("with the flag on and ?surface=celigo the Celigo surface is mounted and no deploy affordance is in the tree", async () => {
    nav.params = new URLSearchParams("surface=celigo");
    features.mockReturnValue(true);
    render(wrap(<WorkspacePage />));
    expect(await screen.findByTestId("celigo-surface")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /deploy/i })).toBeNull();
    expect(screen.queryByText(/changeset/i)).toBeNull();
  });

  it("with the flag off ?surface=celigo renders Files", () => {
    nav.params = new URLSearchParams("surface=celigo");
    features.mockReturnValue(false);
    render(wrap(<WorkspacePage />));
    expect(screen.queryByTestId("celigo-surface")).toBeNull();
  });
});
