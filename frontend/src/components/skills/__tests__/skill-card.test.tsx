import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import type { AgentSkillMetadata } from "@/lib/types";

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pushMock }) }));

import { SkillCard } from "@/components/skills/skill-card";

const fluxSkill: AgentSkillMetadata = {
  name: "Flux Analysis",
  description: "Period-over-period variance commentary on the P&L.",
  triggers: ["/flux"],
  slug: "flux",
};

beforeEach(() => {
  pushMock.mockReset();
});

describe("SkillCard", () => {
  it("renders the name, description, and primary slash trigger", () => {
    render(<SkillCard skill={fluxSkill} />);
    expect(screen.getByText("Flux Analysis")).toBeInTheDocument();
    expect(
      screen.getByText("Period-over-period variance commentary on the P&L."),
    ).toBeInTheDocument();
    expect(screen.getByText("/flux")).toBeInTheDocument();
  });

  it("uses the first slash-prefixed trigger as the primary slash", () => {
    const skill: AgentSkillMetadata = {
      name: "Aging",
      description: "AR/AP aging buckets.",
      triggers: ["aging", "/aging"],
      slug: "aging",
    };
    render(<SkillCard skill={skill} />);
    expect(screen.getByText("/aging")).toBeInTheDocument();
  });

  it("'Use in chat' pushes /chat with a compose param that does not auto-send", () => {
    render(<SkillCard skill={fluxSkill} />);

    fireEvent.click(screen.getByRole("button", { name: /use in chat/i }));

    expect(pushMock).toHaveBeenCalledWith(
      "/chat?compose=%2Fflux%20&new_session=true",
    );
  });
});
