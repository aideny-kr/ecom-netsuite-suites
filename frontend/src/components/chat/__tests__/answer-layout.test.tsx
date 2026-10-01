import { describe, expect, it } from "vitest";
import { extractFollowups, splitLead, stripRanQueryLabels } from "../answer-layout";

describe("answer layout helpers", () => {
  it("lifts the followups fence out of the answer", () => {
    const { text, followups } = extractFollowups(
      "Yucca orders shipped to many countries.\n\n```followups\n- Compare with Metabase\nBreak down by SKU\n```\n",
    );
    expect(text).toBe("Yucca orders shipped to many countries.");
    expect(followups).toEqual(["Compare with Metabase", "Break down by SKU"]);
  });

  it("drops the 'Query I ran' label that only introduces the SQL block", () => {
    // Verbatim shape from the Yucca thread (2026-10-01).
    const text = "Yes. NetSuite shows sales orders.\n\n**Query I ran (SuiteQL):**\n```sql\nSELECT 1\n```\n\n**Caveats:**\n- x";
    const stripped = stripRanQueryLabels(text);
    expect(stripped).not.toContain("Query I ran");
    expect(stripped).toContain("```sql\nSELECT 1\n```");
    expect(stripped).toContain("**Caveats:**");
    expect(stripRanQueryLabels("**Queries run (SuiteQL):**\n```sql\nSELECT 1\n```")).toBe("```sql\nSELECT 1\n```");
  });

  it("keeps a label that is not followed by SQL", () => {
    expect(stripRanQueryLabels("**Query I ran:** none needed.")).toBe("**Query I ran:** none needed.");
  });

  it("splits the opening paragraph from the rest, but never a table or list", () => {
    expect(splitLead("Lead sentence.\n\nRest of it.")).toEqual({ lead: "Lead sentence.", rest: "Rest of it." });
    expect(splitLead("| a | b |\n| - | - |")).toEqual({ lead: "", rest: "| a | b |\n| - | - |" });
    expect(splitLead("- one\n- two")).toEqual({ lead: "", rest: "- one\n- two" });
  });
});
