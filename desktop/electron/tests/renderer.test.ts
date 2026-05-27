// @vitest-environment jsdom
/**
 * Tests for the renderer (electron/renderer.ts + renderer.html).
 *
 * Runs in jsdom — no Electron, no DOM rendering engine. We mount the
 * renderer.html structure, attach the renderer.ts wiring, drive UI
 * events, and assert behavior.
 *
 * Plan gate #5 covers the renderer; the XSS guardrail (textContent NOT
 * innerHTML) is enforced by non-negotiable #5 and asserted explicitly
 * below — a malicious agent response containing `<script>` MUST render
 * as literal text.
 */
import { describe, expect, it, beforeEach, vi } from "vitest";

// Minimal mirror of renderer.html so the test never depends on file I/O.
// If the real renderer.html changes structure, this string is what to
// update to match (and the tests will fail loudly otherwise).
const RENDERER_DOM = `
  <main>
    <header>
      <h1 id="title">Suite Studio Desktop v0 — spike</h1>
    </header>
    <div id="history" role="log" aria-live="polite"></div>
    <form id="composer" aria-label="Send a chat query to the Suite Studio agent">
      <label for="prompt" class="visually-hidden">Chat query</label>
      <input id="prompt" type="text" autocomplete="off" aria-label="Chat query" />
      <button id="send" type="submit">Send</button>
    </form>
  </main>
`;

function mountDOM(): void {
  document.body.innerHTML = RENDERER_DOM;
}

interface SuiteStudioBridge {
  runAgent: (query: string) => Promise<{ response?: string; error?: string }>;
  onSidecarCrashed?: (cb: (info: { code: number | null; signal: string | null }) => void) => void;
}

declare global {
  interface Window {
    suiteStudio: SuiteStudioBridge;
  }
}

beforeEach(() => {
  document.body.innerHTML = "";
  // Reset the global bridge between tests
  (globalThis as { window: Window }).window.suiteStudio = {
    runAgent: vi.fn(async (q: string) => ({ response: `stub:${q}` })),
  };
  vi.resetModules();
});

describe("renderer wiring", () => {
  it("submitting the composer form calls window.suiteStudio.runAgent with the input value", async () => {
    mountDOM();
    const runAgentSpy = vi.fn(async (q: string) => ({ response: `ok:${q}` }));
    window.suiteStudio = { runAgent: runAgentSpy };

    await import("../renderer");

    const input = document.getElementById("prompt") as HTMLInputElement;
    const form = document.getElementById("composer") as HTMLFormElement;
    input.value = "what are my subsidiaries";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await new Promise((r) => setTimeout(r, 0));

    expect(runAgentSpy).toHaveBeenCalledWith("what are my subsidiaries");
  });

  it("appends the user prompt and the agent response into #history", async () => {
    mountDOM();
    window.suiteStudio = {
      runAgent: vi.fn(async () => ({ response: "the response text" })),
    };

    await import("../renderer");

    const input = document.getElementById("prompt") as HTMLInputElement;
    const form = document.getElementById("composer") as HTMLFormElement;
    input.value = "hello";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await new Promise((r) => setTimeout(r, 0));

    const history = document.getElementById("history") as HTMLElement;
    expect(history.textContent).toContain("hello");
    expect(history.textContent).toContain("the response text");
  });

  it("renders agent responses with textContent — NOT innerHTML — so <script> is literal text (XSS guardrail / non-negotiable #5)", async () => {
    mountDOM();
    window.suiteStudio = {
      runAgent: vi.fn(async () => ({ response: "<script>window.pwned=true</script>" })),
    };

    await import("../renderer");

    const input = document.getElementById("prompt") as HTMLInputElement;
    const form = document.getElementById("composer") as HTMLFormElement;
    input.value = "exfiltrate me";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await new Promise((r) => setTimeout(r, 0));

    const history = document.getElementById("history") as HTMLElement;
    // The history must contain the script tag as a TEXT NODE — not as a
    // parsed <script> element. If renderer used innerHTML, this would be
    // parsed and either executed (in a real browser) or appended as a
    // dead element (in jsdom). Either way, the text-node assertion would
    // fail.
    expect(history.textContent).toContain("<script>");
    // There must be no actual <script> element in the rendered history
    expect(history.querySelector("script")).toBeNull();
    // The XSS payload must NOT have executed
    expect((window as unknown as { pwned?: boolean }).pwned).toBeUndefined();
  });

  it("renders error responses distinctly from successful responses", async () => {
    mountDOM();
    window.suiteStudio = {
      runAgent: vi.fn(async () => ({ error: "ANTHROPIC_API_KEY not set" })),
    };

    await import("../renderer");

    const input = document.getElementById("prompt") as HTMLInputElement;
    const form = document.getElementById("composer") as HTMLFormElement;
    input.value = "any prompt";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await new Promise((r) => setTimeout(r, 0));

    const history = document.getElementById("history") as HTMLElement;
    // Error must surface to the user (renderer is a UI; if errors are
    // silently swallowed gate #6 is broken). The marker the renderer
    // applies — class "error", role attribute, or visible "Error:"
    // prefix — is implementation-defined; we test the user-visible text.
    expect(history.textContent).toContain("ANTHROPIC_API_KEY not set");
    // Some visible indicator that THIS line is an error, distinguishable
    // from a normal response. We accept either an explicit "Error" string
    // or an element with a class containing "error".
    const errorMarkerVisible =
      history.textContent!.toLowerCase().includes("error") ||
      history.querySelector('[class*="error"]') !== null;
    expect(errorMarkerVisible).toBe(true);
  });

  it("clears the input after submission", async () => {
    mountDOM();
    window.suiteStudio = { runAgent: vi.fn(async () => ({ response: "ok" })) };

    await import("../renderer");

    const input = document.getElementById("prompt") as HTMLInputElement;
    const form = document.getElementById("composer") as HTMLFormElement;
    input.value = "should be cleared";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await new Promise((r) => setTimeout(r, 0));

    expect(input.value).toBe("");
  });

  it("does NOT submit when the input is empty (whitespace-only)", async () => {
    mountDOM();
    const runAgentSpy = vi.fn(async () => ({ response: "ok" }));
    window.suiteStudio = { runAgent: runAgentSpy };

    await import("../renderer");

    const input = document.getElementById("prompt") as HTMLInputElement;
    const form = document.getElementById("composer") as HTMLFormElement;
    input.value = "   ";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await new Promise((r) => setTimeout(r, 0));

    expect(runAgentSpy).not.toHaveBeenCalled();
  });
});
