import { describe, it, expect } from "vitest";

import { primarySlash } from "@/lib/skills";
import type { AgentSkillMetadata } from "@/lib/types";

const base: AgentSkillMetadata = {
  name: "X",
  description: "x",
  triggers: [],
  slug: "x",
};

describe("primarySlash", () => {
  it("returns the only slash trigger", () => {
    expect(primarySlash({ ...base, triggers: ["/flux"] })).toBe("/flux");
  });

  it("prefers the first slash-prefixed trigger over a non-slash one", () => {
    expect(primarySlash({ ...base, triggers: ["aging", "/aging"] })).toBe("/aging");
  });

  it("falls back to the first trigger when none start with a slash", () => {
    expect(primarySlash({ ...base, triggers: ["export"] })).toBe("export");
  });
});
