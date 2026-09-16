import { expect, it } from "vitest";
import { workspaceChatContent } from "@/lib/workspace-chat-context";

it("preserves long messages and the exact current file prefix", () => {
  const content = "x".repeat(6000);
  expect(workspaceChatContent(content, "src/example.js", 32000))
    .toBe(`[Currently viewing file: src/example.js]\n\n${content}`);
  expect(workspaceChatContent(content, undefined, 6000)).toBe(content);
});
it("rejects an oversized enriched message without silently truncating", () => {
  expect(() => workspaceChatContent("x".repeat(100), "a.js", 100)).toThrow("including file context");
});
