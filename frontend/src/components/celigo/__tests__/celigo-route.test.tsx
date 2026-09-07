import { renderHook, act } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: nav.push, replace: nav.replace }), useSearchParams: () => nav.params, usePathname: () => "/workspace" }));

import { readCeligoRoute, useCeligoRoute, isScriptsView } from "../celigo-route";

beforeEach(() => { nav.push.mockReset(); nav.replace.mockReset(); nav.params = new URLSearchParams(); });

describe("readCeligoRoute", () => {
  it("defaults to files and normalises bad values", () => {
    expect(readCeligoRoute(new URLSearchParams("")).surface).toBe("files");
    expect(readCeligoRoute(new URLSearchParams("surface=celigo&view=bogus&tab=bogus")).view).toBe("tiles");
    expect(readCeligoRoute(new URLSearchParams("surface=celigo&integration=i1&tab=scripts")).tab).toBe("scripts");
    expect(readCeligoRoute(new URLSearchParams("surface=celigo&flow=f1&step=s1&script=x1"))).toMatchObject({ flowId: "f1", stepId: "s1", scriptId: "x1" });
  });
});

describe("useCeligoRoute is the only writer", () => {
  it("entering the surface drops file/workspace params; leaving drops celigo params", () => {
    nav.params = new URLSearchParams("file=a.js&workspace=w1");
    const { result } = renderHook(() => useCeligoRoute());
    act(() => result.current.go.integrations());
    expect(nav.push).toHaveBeenCalledWith("/workspace?surface=celigo");
    nav.params = new URLSearchParams("surface=celigo&flow=f1&step=s1");
    const r2 = renderHook(() => useCeligoRoute());
    act(() => r2.result.current.go.files());
    expect(nav.push).toHaveBeenLastCalledWith("/workspace");
  });
  it("levels push, selection replaces", () => {
    nav.params = new URLSearchParams("surface=celigo&flow=f1");
    const { result } = renderHook(() => useCeligoRoute());
    act(() => result.current.go.step("s9"));
    expect(nav.replace).toHaveBeenCalledWith("/workspace?surface=celigo&flow=f1&step=s9");
    act(() => result.current.go.integration("i1", "errors"));
    expect(nav.push).toHaveBeenCalledWith("/workspace?surface=celigo&integration=i1&tab=errors");
    act(() => result.current.go.flow("f2"));
    expect(nav.push).toHaveBeenLastCalledWith("/workspace?surface=celigo&flow=f2");
  });

  it("go.tab and go.view replace instead of pushing, keeping the rest of the URL", () => {
    // Gate fix wave, item 7. Switching a tab or flipping tiles/list is a
    // selection change WITHIN the page already on screen -- the same category
    // as go.step/go.script -- but both went through a pushing `go.*`, so Back
    // walked one entry per tab a reader had glanced at instead of leaving the
    // page they came from.
    nav.params = new URLSearchParams("surface=celigo&integration=i1&tab=scripts");
    const { result } = renderHook(() => useCeligoRoute());
    act(() => result.current.go.tab("changes"));
    expect(nav.replace).toHaveBeenLastCalledWith("/workspace?surface=celigo&integration=i1&tab=changes");
    expect(nav.push).not.toHaveBeenCalled();

    nav.params = new URLSearchParams("surface=celigo&view=list");
    const r2 = renderHook(() => useCeligoRoute());
    act(() => r2.result.current.go.view("tiles"));
    expect(nav.replace).toHaveBeenLastCalledWith("/workspace?surface=celigo");
    act(() => r2.result.current.go.view("list"));
    expect(nav.replace).toHaveBeenLastCalledWith("/workspace?surface=celigo&view=list");
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("go.tab keeps a drilled-in flow/step/script and the current view", () => {
    nav.params = new URLSearchParams("surface=celigo&view=list&integration=i1&flow=f1&step=s1&script=x1");
    const { result } = renderHook(() => useCeligoRoute());
    act(() => result.current.go.tab("errors"));
    expect(nav.replace).toHaveBeenLastCalledWith(
      "/workspace?surface=celigo&view=list&integration=i1&tab=errors&flow=f1&step=s1&script=x1",
    );
  });

  it("go.flow uses the caller's own integration when given, the current one otherwise", () => {
    // Gate fix wave, item 5. `go.flow` attached whatever `?integration=` the
    // CURRENT page carried, so a ⌘K result from another integration opened
    // under the wrong one -- a breadcrumb and a sibling list belonging to an
    // integration that does not contain the flow on screen.
    nav.params = new URLSearchParams("surface=celigo&integration=iA");
    const { result } = renderHook(() => useCeligoRoute());

    act(() => result.current.go.flow("f1"));
    expect(nav.push).toHaveBeenLastCalledWith("/workspace?surface=celigo&integration=iA&flow=f1");

    act(() => result.current.go.flow("f2", "iB"));
    expect(nav.push).toHaveBeenLastCalledWith("/workspace?surface=celigo&integration=iB&flow=f2");
  });

  // Codex fix wave, item 25. One script is routinely attached at several
  // SITES on the same step (a preMap and a postMap, or two clones of one
  // family), and `flow_step_id` cannot tell them apart — so the drawer named
  // whichever site the backend returned first. The site the reader actually
  // clicked travels on the URL, which also means a pasted link reopens the
  // same one.
  it("item 25: go.script carries the attachment site, and reading it back gives the json_path", () => {
    nav.params = new URLSearchParams("surface=celigo&flow=f1&step=s1");
    const { result } = renderHook(() => useCeligoRoute());

    act(() => result.current.go.script("x1", { jsonPath: "66738c3d….hooks.preMap" }));
    expect(nav.replace).toHaveBeenLastCalledWith(
      "/workspace?surface=celigo&flow=f1&step=s1&script=x1&site=66738c3d%E2%80%A6.hooks.preMap",
    );

    expect(
      readCeligoRoute(new URLSearchParams("surface=celigo&script=x1&site=a.hooks.preMap")).scriptSite,
    ).toBe("a.hooks.preMap");
    expect(readCeligoRoute(new URLSearchParams("surface=celigo&script=x1")).scriptSite).toBeNull();
  });

  it("item 25: closing the drawer drops the site along with the script", () => {
    nav.params = new URLSearchParams("surface=celigo&flow=f1&step=s1&script=x1&site=a.hooks.preMap");
    const { result } = renderHook(() => useCeligoRoute());

    act(() => result.current.go.script(null));
    expect(nav.replace).toHaveBeenLastCalledWith("/workspace?surface=celigo&flow=f1&step=s1");
  });

  it("item 25: a same-page tab change keeps the open script AND its site", () => {
    nav.params = new URLSearchParams("surface=celigo&integration=i1&flow=f1&step=s1&script=x1&site=a.hooks.preMap");
    const { result } = renderHook(() => useCeligoRoute());

    act(() => result.current.go.tab("errors"));
    expect(nav.replace).toHaveBeenLastCalledWith(
      "/workspace?surface=celigo&integration=i1&tab=errors&flow=f1&step=s1&script=x1&site=a.hooks.preMap",
    );
  });
});

// Task 3 (Scripts view route params, spec §3.1). `family`/`copy`/`compare`/
// `filter`/`kind`/`q`/`in`, plus `isScriptsView` — the one place that tells
// the account-wide Scripts view (`?tab=scripts` with no `integration`) apart
// from the integration page's OWN Scripts tab (`?integration=X&tab=scripts`).
describe("Scripts view route params", () => {
  it("readCeligoRoute decodes every new param, defaulting the invalid ones", () => {
    const route = readCeligoRoute(
      new URLSearchParams("surface=celigo&tab=scripts&family=fam1&copy=cp1&compare=A..D&filter=diverged&kind=hook&q=sales&in=int1"),
    );
    expect(route).toMatchObject({
      familyKey: "fam1",
      copyId: "cp1",
      compare: { left: "A", right: "D" },
      scriptsFilter: "diverged",
      scriptsKind: "hook",
      scriptsIntegrationId: "int1",
      q: "sales",
    });
  });

  it("defaults: no params → all/null/null/''", () => {
    const route = readCeligoRoute(new URLSearchParams("surface=celigo&tab=scripts"));
    expect(route).toMatchObject({
      familyKey: null,
      copyId: null,
      compare: null,
      scriptsFilter: "all",
      scriptsKind: null,
      scriptsIntegrationId: null,
      q: "",
    });
  });

  it("invalid filter/kind/compare fall back instead of throwing", () => {
    const route = readCeligoRoute(new URLSearchParams("surface=celigo&filter=bogus&kind=bogus&compare=bogus"));
    expect(route.scriptsFilter).toBe("all");
    expect(route.scriptsKind).toBeNull();
    expect(route.compare).toBeNull();

    // malformed compare shapes: no separator, empty side either way
    expect(readCeligoRoute(new URLSearchParams("compare=A")).compare).toBeNull();
    expect(readCeligoRoute(new URLSearchParams("compare=..D")).compare).toBeNull();
    expect(readCeligoRoute(new URLSearchParams("compare=A..")).compare).toBeNull();
  });

  it("isScriptsView: tab=scripts with no integration/flow is the Scripts view; with an integration it is NOT", () => {
    expect(isScriptsView(readCeligoRoute(new URLSearchParams("surface=celigo&tab=scripts")))).toBe(true);
    expect(
      isScriptsView(readCeligoRoute(new URLSearchParams("surface=celigo&integration=i1&tab=scripts"))),
    ).toBe(false);
    expect(
      isScriptsView(readCeligoRoute(new URLSearchParams("surface=celigo&flow=f1&tab=scripts"))),
    ).toBe(false);
    expect(isScriptsView(readCeligoRoute(new URLSearchParams("surface=celigo")))).toBe(false);
  });

  it("go.scripts round-trips every param through the URL and never writes `integration`", () => {
    nav.params = new URLSearchParams("surface=celigo&integration=iA&flow=f1&step=s1&script=x1&site=a.b");
    const { result } = renderHook(() => useCeligoRoute());

    act(() =>
      result.current.go.scripts({
        family: "fam1",
        copy: "cp1",
        compare: { left: "A", right: "D" },
        filter: "diverged",
        kind: "hook",
        q: "sales order",
        in: "int1",
      }),
    );
    const url = nav.push.mock.calls.at(-1)![0] as string;
    expect(url).not.toMatch(/[?&]integration=/);
    expect(url).not.toMatch(/[?&]flow=/);
    expect(url).not.toMatch(/[?&]step=/);
    expect(url).not.toMatch(/[?&]script=/);
    expect(url).not.toMatch(/[?&]site=/);

    const [, qs] = url.split("?");
    const roundTripped = readCeligoRoute(new URLSearchParams(qs));
    expect(roundTripped).toMatchObject({
      tab: "scripts",
      familyKey: "fam1",
      copyId: "cp1",
      compare: { left: "A", right: "D" },
      scriptsFilter: "diverged",
      scriptsKind: "hook",
      q: "sales order",
      scriptsIntegrationId: "int1",
    });
    expect(isScriptsView(roundTripped)).toBe(true);
  });

  it("go.scripts({ in }) sets the integration FILTER, never the `integration` page param", () => {
    nav.params = new URLSearchParams("");
    const { result } = renderHook(() => useCeligoRoute());
    act(() => result.current.go.scripts({ in: "int1" }));
    expect(nav.push).toHaveBeenLastCalledWith("/workspace?surface=celigo&tab=scripts&in=int1");
  });

  it("go.scripts with no options lands on the plain Scripts view", () => {
    nav.params = new URLSearchParams("surface=celigo&integration=iA&tab=flows");
    const { result } = renderHook(() => useCeligoRoute());
    act(() => result.current.go.scripts());
    expect(nav.push).toHaveBeenLastCalledWith("/workspace?surface=celigo&tab=scripts");
  });

  it("go.scripts drops the default filter (\"all\") and a null kind/compare from the URL", () => {
    nav.params = new URLSearchParams("");
    const { result } = renderHook(() => useCeligoRoute());
    act(() => result.current.go.scripts({ family: "fam1", filter: "all", kind: null, compare: null }));
    expect(nav.push).toHaveBeenLastCalledWith("/workspace?surface=celigo&tab=scripts&family=fam1");
  });
});
