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
});
