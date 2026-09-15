import { describe, expect, it } from "vitest";
import { tokenUsageSummary } from "./token-usage";
import { normalizeStreamMessage } from "./chat-stream";

describe("provider token usage", () => {
  it("retains usage from the actual streaming message shape", () => {
    const message = normalizeStreamMessage({ id: "message", role: "assistant", content: "Done", provider_used: "anthropic", input_tokens: 21, output_tokens: 263, cache_creation_tokens: 91539, cache_read_tokens: 85853 });
    expect(message).not.toBeNull();
    expect(tokenUsageSummary(message!)?.label).toBe("177,676 tokens");
  });
  it("includes Anthropic cache creation and reads instead of showing the small input/output subtotal", () => {
    const value = tokenUsageSummary({ provider_used: "anthropic", input_tokens: 21, output_tokens: 263, cache_creation_tokens: 91539, cache_read_tokens: 85853 });
    expect(value?.label).toBe("177,676 tokens");
    expect(value?.detail).toContain("cache creation: 91,539");
  });
  it("does not add OpenAI cached input twice", () => {
    expect(tokenUsageSummary({ provider_used: "openai", input_tokens: 1000, output_tokens: 100, cache_creation_tokens: 0, cache_read_tokens: 600 })?.label).toBe("1,100 tokens");
  });
  it("does not claim complete usage for historical Anthropic messages missing cache counts", () => {
    expect(tokenUsageSummary({ provider_used: "anthropic", input_tokens: 21, output_tokens: 263 })?.label).toBe("284 input/output tokens · cache not reported");
  });
  it("preserves unknown provider semantics and rejects invalid counts", () => {
    expect(tokenUsageSummary({ provider_used: "other", input_tokens: 10, output_tokens: 20 })?.label).toBe("30 input/output tokens");
    expect(tokenUsageSummary({ input_tokens: -1, output_tokens: 10 })).toBeNull();
    expect(tokenUsageSummary({ input_tokens: 1 })).toBeNull();
  });
});
