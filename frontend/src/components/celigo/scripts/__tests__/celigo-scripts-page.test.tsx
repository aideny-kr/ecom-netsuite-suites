import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { resolved, pending, errored } from "../../__tests__/query-fixtures";
import type { CeligoScriptFamiliesList } from "@/hooks/use-celigo-flows";

// Task 3 — a MINIMAL shell: the crumb, the "Scripts" heading, and the three
// query states (`queryState()`, `lib/query-state.ts` — a pending query is
// never rendered as empty, an errored one never as loading or "0 scripts").
// Task 4 fills this in with the stat tiles, the list|detail split, and the
// real empty states (spec §3.3) — this file only has to prove the shell
// exists and gates correctly, not the full page.

const mocks = vi.hoisted(() => ({ families: vi.fn() }));
vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoScriptFamilies: () => mocks.families(),
}));

const routeMocks = vi.hoisted(() => ({
  go: { integrations: vi.fn() },
}));
vi.mock("../../celigo-route", () => ({
  useCeligoRoute: () => ({ go: routeMocks.go }),
}));

import { CeligoScriptsPage } from "../celigo-scripts-page";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const EMPTY_LIST: CeligoScriptFamiliesList = {
  totals: {
    scripts: 0,
    families: 0,
    attached_families: 0,
    unattached_families: 0,
    diverged_families: 0,
    sites: 0,
    flows_with_sites: 0,
    flows_total: 0,
    integrations_with_sites: 0,
    sites_with_open_errors: 0,
  },
  families: [],
  synced_at: null,
};

beforeEach(() => {
  routeMocks.go.integrations.mockReset();
});

describe("CeligoScriptsPage — minimal shell", () => {
  it("always renders the Celigo › Scripts crumb and the Scripts heading", () => {
    mocks.families.mockReturnValue(pending());
    wrap(<CeligoScriptsPage />);
    expect(screen.getByText("Celigo")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Scripts" })).toBeInTheDocument();
  });

  it("pending renders a loading state, never an empty one", () => {
    mocks.families.mockReturnValue(pending());
    wrap(<CeligoScriptsPage />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it("error renders a retry notice, never loading or a confident zero", () => {
    const refetch = vi.fn();
    mocks.families.mockReturnValue(errored(refetch));
    wrap(<CeligoScriptsPage />);
    expect(screen.getByText(/couldn.?t load/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });

  it("success renders real data, not the pending/error branches", () => {
    mocks.families.mockReturnValue(resolved({ ...EMPTY_LIST, totals: { ...EMPTY_LIST.totals, families: 7 } }));
    wrap(<CeligoScriptsPage />);
    expect(screen.queryByText(/loading/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/couldn.?t load/i)).not.toBeInTheDocument();
  });

  it("clicking the Celigo crumb goes back to the integrations list", () => {
    mocks.families.mockReturnValue(resolved(EMPTY_LIST));
    wrap(<CeligoScriptsPage />);
    fireEvent.click(screen.getByText("Celigo"));
    expect(routeMocks.go.integrations).toHaveBeenCalled();
  });
});
