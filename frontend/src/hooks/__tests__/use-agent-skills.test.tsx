import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const get = vi.fn();

vi.mock("@/lib/api-client", () => ({
  apiClient: {
    get: (...a: unknown[]) => get(...a),
  },
}));

import { useAgentSkills } from "@/hooks/use-agent-skills";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  get.mockReset();
});

describe("use-agent-skills", () => {
  it("GETs the skills catalog endpoint and returns the array", async () => {
    const skills = [
      {
        name: "Flux Analysis",
        description: "Period-over-period variance commentary",
        triggers: ["/flux"],
        slug: "flux",
      },
    ];
    get.mockResolvedValue(skills);

    const { result } = renderHook(() => useAgentSkills(), { wrapper });

    await waitFor(() => expect(result.current.data).toEqual(skills));
    expect(get).toHaveBeenCalledWith("/api/v1/skills/catalog");
  });
});
