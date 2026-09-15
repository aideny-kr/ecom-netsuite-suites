import { validateInput } from "@/lib/webmcp";
import type { WebMcpAction } from "@/hooks/use-webmcp-tools";

export const uuidSchema = { type: "string", format: "uuid" };
export function uuidArgument(value: unknown, name: string): string {
  if (typeof value !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) {
    throw new Error(`${name} must be a UUID.`);
  }
  return value;
}
export function integerArgument(value: unknown, fallback: number, min: number, max: number) {
  if (value === undefined) return fallback;
  if (typeof value !== "number" || !Number.isInteger(value) || value < min || value > max) {
    throw new Error(`Expected an integer between ${min} and ${max}.`);
  }
  return value;
}

export function actionTool(
  name: string, description: string, readOnly: boolean,
  properties: Record<string, unknown>, required: string[],
  execute: (input: Record<string, unknown>, assertCurrent: () => void) => unknown | Promise<unknown>,
): WebMcpAction {
  return { name: `suitestudio_${name}`, description,
    inputSchema: { type: "object", properties, required, additionalProperties: false },
    annotations: { readOnlyHint: readOnly, untrustedContentHint: true },
    execute: (input, assertCurrent = () => {}) => {
      const args = validateInput(input, Object.keys(properties));
      if (required.some((key) => args[key] === undefined)) throw new Error("Missing required arguments.");
      return execute(args, assertCurrent);
    },
  };
}

/** Bounded preview, with explicit truncation. Confirmation credentials and
 * provider credentials are never part of the browser-agent inspection surface. */
export function boundedPreview(value: unknown, maxChars = 24000) {
  let remaining = maxChars;
  let truncated = false;
  function visit(item: unknown, depth: number): unknown {
    if (remaining <= 0 || depth > 10) { truncated = true; return "[truncated]"; }
    remaining -= 16;
    if (typeof item === "string") {
      const size = Math.max(0, Math.min(remaining, 8000));
      remaining -= Math.min(item.length, size);
      if (item.length > size) { truncated = true; return item.slice(0, size) + "[truncated]"; }
      return item;
    }
    if (Array.isArray(item)) {
      if (item.length > 50) truncated = true;
      return item.slice(0, 50).map((entry) => visit(entry, depth + 1));
    }
    if (item && typeof item === "object") {
      const entries = Object.entries(item);
      if (entries.length > 80) truncated = true;
      return Object.fromEntries(entries.slice(0, 80).filter(([key]) =>
        !/(token|secret|password|authorization|api[_-]?key|hmac)/i.test(key),
      ).map(([key, entry]) => [key, visit(entry, depth + 1)]));
    }
    return item;
  }
  const data = visit(value, 0);
  return { data, truncated };
}
