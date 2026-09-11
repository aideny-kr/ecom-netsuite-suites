import type { ChatMessage } from "./types";

/** Provider counts have different cache semantics; this is usage, not billing. */
export function tokenUsageSummary(message: Pick<ChatMessage, "input_tokens" | "output_tokens" | "cache_creation_tokens" | "cache_read_tokens" | "provider_used">) {
  const valid = (n: number | undefined): n is number => n != null && Number.isSafeInteger(n) && n >= 0;
  const { input_tokens: input, output_tokens: output, cache_creation_tokens: write, cache_read_tokens: read } = message;
  if (!valid(input) || !valid(output)) return null;
  const fmt = (n: number) => n.toLocaleString();
  const provider = message.provider_used?.toLowerCase();
  const cacheKnown = valid(write) && valid(read);
  const anthropic = provider === "anthropic";
  const detail = `Provider input: ${fmt(input)}; output: ${fmt(output)}; cache creation: ${valid(write) ? fmt(write) : "not reported"}; cache reads: ${valid(read) ? fmt(read) : "not reported"}. `
    + (anthropic ? "Anthropic reports cached tokens separately from input. " : "Cached input may already be included in provider input. ")
    + "Token usage is not a monetary charge.";
  if (anthropic && cacheKnown) return { label: `${fmt(input + output + write + read)} tokens`, detail };
  if (provider === "openai" || provider === "gemini") return { label: `${fmt(input + output)} tokens`, detail };
  return { label: `${fmt(input + output)} input/output tokens${anthropic ? " · cache not reported" : ""}`, detail };
}
