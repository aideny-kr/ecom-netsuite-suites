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
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
// the page destructures. Most of them never need to return anything but
// empty/undefined; the workspace ones below are the exception.
//
// Configurable per test: the deep-link tests at the bottom need a real
// workspace and a real file in the tree, and observe which file the page ended
// up asking for (`useFileContent(workspaceId, fileId)`) rather than digging
// through the editor's DOM for it.
const ws = vi.hoisted(() => ({
  workspaces: [] as Array<{ id: string; name: string }>,
  files: [] as Array<{ id: string; path: string; is_directory: boolean }>,
  fileContent: vi.fn(),
}));
/** An inert react-query mutation result — enough shape for a control to render
 * and stay disabled. Nothing in this file drives a mutation. */
const stubMutation = vi.hoisted(
  () => () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false, isError: false, error: null, data: undefined }),
);
vi.mock("@/hooks/use-workspace", () => ({
  useWorkspaces: () => ({ data: ws.workspaces }),
  useCreateWorkspace: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useWorkspaceFiles: () => ({ data: ws.files }),
  useFileContent: (workspaceId: string | null, fileId: string | null) => {
    ws.fileContent(workspaceId, fileId);
    return { data: undefined };
  },
  useSearchFiles: () => ({ data: undefined }),
  // The rest of the module. Untouched by the older tests (an empty workspace
  // list keeps the Files chrome from mounting at all), but the deep-link tests
  // below DO give the page a workspace, which brings the import dialog and the
  // changeset controls into the tree with it.
  useWorkspace: () => ({ data: undefined }),
  useDeleteWorkspace: () => stubMutation(),
  useReorganizeWorkspace: () => stubMutation(),
  useImportWorkspace: () => stubMutation(),
}));
vi.mock("@/hooks/use-changesets", () => ({
  useChangesets: () => ({ data: [] }),
  useChangesetDiff: () => ({ data: undefined }),
  useChangeset: () => ({ data: undefined }),
  useCreateChangeset: () => stubMutation(),
  useTransitionChangeset: () => stubMutation(),
  useApplyChangeset: () => stubMutation(),
}));
vi.mock("@/hooks/use-runs", () => ({
  useRuns: () => ({ data: [] }),
  useRun: () => ({ data: undefined }),
  useRunArtifacts: () => ({ data: undefined }),
  useUATReport: () => ({ data: undefined }),
  useTriggerValidate: () => stubMutation(),
  useTriggerUnitTests: () => stubMutation(),
  useTriggerAssertions: () => stubMutation(),
  useDeployPreview: () => stubMutation(),
  useConfirmDeploy: () => stubMutation(),
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
  ws.workspaces = [];
  ws.files = [];
  ws.fileContent.mockReset();
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

  it("with the flag off, ?surface=celigo&file=… still restores the deep-linked file", async () => {
    // Final-review finding I6. The Files deep-link effect bailed on the RAW
    // `route.surface`, but the surface the page actually renders is the
    // FLAG-GATED one. With the flag off, a stale `?surface=celigo&file=…`
    // bookmark rendered Files while the effect stood aside for a Celigo
    // surface that was never mounted -- so the file deep link was dropped and
    // the editor came up empty.
    ws.workspaces = [{ id: "ws-1", name: "Main" }];
    ws.files = [{ id: "f-1", path: "SuiteScripts/order.js", is_directory: false }];
    nav.params = new URLSearchParams("surface=celigo&file=SuiteScripts/order.js");
    features.mockReturnValue(false);

    render(wrap(<WorkspacePage />));

    await waitFor(() => expect(ws.fileContent).toHaveBeenCalledWith("ws-1", "f-1"));
  });

  it("with the flag ON, ?surface=celigo&file=… leaves the file alone — Celigo owns the URL", async () => {
    // The other half of the same rule, so the fix above cannot quietly turn
    // into "the Files effect always runs": while the Celigo surface really is
    // mounted, it owns these params and the Files effect must stay out.
    ws.workspaces = [{ id: "ws-1", name: "Main" }];
    ws.files = [{ id: "f-1", path: "SuiteScripts/order.js", is_directory: false }];
    nav.params = new URLSearchParams("surface=celigo&file=SuiteScripts/order.js");
    features.mockReturnValue(true);

    render(wrap(<WorkspacePage />));

    expect(await screen.findByTestId("celigo-surface")).toBeInTheDocument();
    expect(ws.fileContent).not.toHaveBeenCalledWith("ws-1", "f-1");
  });
});
