import { renderHook, act } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: nav.push, replace: nav.replace }), useSearchParams: () => nav.params, usePathname: () => "/workspace" }));

import { readCeligoRoute, useCeligoRoute } from "../celigo-route";

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
});
