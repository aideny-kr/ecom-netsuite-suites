import { describe, it, expect } from "vitest";

import { detectDriveTrigger, insertDriveMention } from "../drive-mention-trigger";

describe("detectDriveTrigger", () => {
  it("returns the query when # is at start of input", () => {
    expect(detectDriveTrigger("#ret")).toBe("ret");
  });

  it("returns empty string when # is typed with nothing after it", () => {
    expect(detectDriveTrigger("#")).toBe("");
  });

  it("returns the query after # when preceded by a space", () => {
    expect(detectDriveTrigger("summarize #ret")).toBe("ret");
  });

  it("returns null when # is inside a word (no preceding whitespace)", () => {
    expect(detectDriveTrigger("foo#bar")).toBeNull();
    expect(detectDriveTrigger("prefix#ret")).toBeNull();
  });

  it("returns null when the value does not end at the mention token", () => {
    // User has already moved past the mention — we only fire on the trailing token.
    expect(detectDriveTrigger("#ret and then more")).toBeNull();
  });

  it("returns null when value is empty or has no #", () => {
    expect(detectDriveTrigger("")).toBeNull();
    expect(detectDriveTrigger("plain text")).toBeNull();
    expect(detectDriveTrigger("@workspace:foo")).toBeNull();
  });

  it("returns null for `/` command trigger (regression: don't hijack /)", () => {
    expect(detectDriveTrigger("/")).toBeNull();
    expect(detectDriveTrigger("/export_analytics")).toBeNull();
  });
});

describe("insertDriveMention", () => {
  it("replaces a trailing #query with the insertion + trailing space", () => {
    const result = insertDriveMention(
      "summarize #ret",
      "[Returns Policy](https://docs.google.com/document/d/abc/edit)",
    );
    expect(result).toBe(
      "summarize [Returns Policy](https://docs.google.com/document/d/abc/edit) ",
    );
  });

  it("replaces a bare # with the insertion + trailing space", () => {
    const result = insertDriveMention("#", "[Returns Policy](https://x)");
    expect(result).toBe("[Returns Policy](https://x) ");
  });

  it("leaves non-# content alone (caller should check detectDriveTrigger first)", () => {
    expect(insertDriveMention("no hash here", "[X](https://y)")).toBe(
      "no hash here",
    );
  });
});
