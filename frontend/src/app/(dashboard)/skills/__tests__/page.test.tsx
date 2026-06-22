import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import type { AgentSkillMetadata } from "@/lib/types";

const skills: AgentSkillMetadata[] = [
  {
    name: "Flux Analysis",
    description: "Period-over-period variance commentary.",
    triggers: ["/flux"],
    slug: "flux",
  },
  {
    name: "AR Aging",
    description: "Receivables aging buckets.",
    triggers: ["/aging"],
    slug: "aging",
  },
  {
    name: "Margin Bridge",
    description: "Gross margin walk between periods.",
    triggers: ["/margin-bridge"],
    slug: "margin-bridge",
  },
];

const useAgentSkillsMock = vi.fn();
vi.mock("@/hooks/use-agent-skills", () => ({
  useAgentSkills: () => useAgentSkillsMock(),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import SkillsPage from "@/app/(dashboard)/skills/page";

beforeEach(() => {
  useAgentSkillsMock.mockReset();
});

describe("Skills page", () => {
  it("renders one card per skill", () => {
    useAgentSkillsMock.mockReturnValue({ data: skills, isLoading: false, error: null });
    render(<SkillsPage />);
    expect(screen.getByText("Flux Analysis")).toBeInTheDocument();
    expect(screen.getByText("AR Aging")).toBeInTheDocument();
    expect(screen.getByText("Margin Bridge")).toBeInTheDocument();
  });

  it("filters by name, case-insensitively", () => {
    useAgentSkillsMock.mockReturnValue({ data: skills, isLoading: false, error: null });
    render(<SkillsPage />);
    fireEvent.change(screen.getByPlaceholderText(/search/i), {
      target: { value: "FLUX" },
    });
    expect(screen.getByText("Flux Analysis")).toBeInTheDocument();
    expect(screen.queryByText("AR Aging")).not.toBeInTheDocument();
    expect(screen.queryByText("Margin Bridge")).not.toBeInTheDocument();
  });

  it("filters by description substring", () => {
    useAgentSkillsMock.mockReturnValue({ data: skills, isLoading: false, error: null });
    render(<SkillsPage />);
    fireEvent.change(screen.getByPlaceholderText(/search/i), {
      target: { value: "receivables" },
    });
    expect(screen.getByText("AR Aging")).toBeInTheDocument();
    expect(screen.queryByText("Flux Analysis")).not.toBeInTheDocument();
  });

  it("filters by trigger substring", () => {
    useAgentSkillsMock.mockReturnValue({ data: skills, isLoading: false, error: null });
    render(<SkillsPage />);
    fireEvent.change(screen.getByPlaceholderText(/search/i), {
      target: { value: "margin-bridge" },
    });
    expect(screen.getByText("Margin Bridge")).toBeInTheDocument();
    expect(screen.queryByText("Flux Analysis")).not.toBeInTheDocument();
  });

  it("shows an empty state when no skill matches the search", () => {
    useAgentSkillsMock.mockReturnValue({ data: skills, isLoading: false, error: null });
    render(<SkillsPage />);
    fireEvent.change(screen.getByPlaceholderText(/search/i), {
      target: { value: "zzzznotaskill" },
    });
    expect(screen.getByText(/no skills match/i)).toBeInTheDocument();
  });

  it("shows the loading skeleton while fetching", () => {
    useAgentSkillsMock.mockReturnValue({ data: undefined, isLoading: true, error: null });
    render(<SkillsPage />);
    expect(screen.getByTestId("skills-loading")).toBeInTheDocument();
  });

  it("shows an error card when the catalog fails to load", () => {
    useAgentSkillsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error("boom"),
    });
    render(<SkillsPage />);
    expect(screen.getByText(/failed to load/i)).toBeInTheDocument();
  });

  it("shows an empty state when the catalog has no skills", () => {
    useAgentSkillsMock.mockReturnValue({ data: [], isLoading: false, error: null });
    render(<SkillsPage />);
    expect(screen.getByText(/no skills available/i)).toBeInTheDocument();
  });
});
