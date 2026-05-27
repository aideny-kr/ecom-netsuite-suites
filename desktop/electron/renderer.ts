/**
 * Renderer-side wiring for Suite Studio Desktop (B0 spike, /goal #5).
 *
 * Bare vanilla TS — no React, no framework. Next.js integration is a
 * separate /goal (likely #7); this file MUST stay small enough that
 * swapping it out for the real renderer is trivial.
 *
 * XSS guardrail (non-negotiable #5): every text node inserted into the
 * DOM uses `textContent`, never `innerHTML`. Markdown rendering is not
 * implemented in v0; if added later, it must run through a vetted
 * sanitizer first.
 */

// `AgentResult`, `SuiteStudioBridge`, and `Window.suiteStudio` come from
// the ambient declarations in `types.d.ts` — no imports needed because
// the renderer is loaded as a plain `<script>` and must compile to a
// non-module JS file.

type TurnRole = "user" | "agent" | "error";

function appendTurn(history: HTMLElement, role: TurnRole, text: string): void {
  const turn = document.createElement("div");
  turn.classList.add("turn", role);

  const roleLabel = document.createElement("span");
  roleLabel.classList.add("role");
  roleLabel.textContent = role === "user" ? "You" : role === "agent" ? "Agent" : "Error";

  const body = document.createElement("span");
  body.classList.add("body");
  // textContent — NEVER innerHTML. Non-negotiable #5.
  body.textContent = text;

  turn.appendChild(roleLabel);
  turn.appendChild(body);
  history.appendChild(turn);

  // Keep the most recent turn in view
  history.scrollTop = history.scrollHeight;
}

function wireComposer(): void {
  const form = document.getElementById("composer") as HTMLFormElement | null;
  const input = document.getElementById("prompt") as HTMLInputElement | null;
  const sendBtn = document.getElementById("send") as HTMLButtonElement | null;
  const history = document.getElementById("history") as HTMLElement | null;

  if (!form || !input || !history) {
    // If the renderer DOM is missing required nodes the spike is broken
    // structurally — log it and bail.
    console.error("renderer: required DOM nodes missing (#composer, #prompt, #history)");
    return;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const raw = input.value;
    const query = raw.trim();
    if (!query) {
      return;
    }
    appendTurn(history, "user", query);
    input.value = "";
    if (sendBtn) {
      sendBtn.disabled = true;
    }
    try {
      const result = await window.suiteStudio.runAgent(query);
      if (result.error) {
        appendTurn(history, "error", result.error);
      } else if (typeof result.response === "string") {
        appendTurn(history, "agent", result.response);
      } else {
        appendTurn(history, "error", "sidecar returned an empty response");
      }
    } catch (err) {
      appendTurn(history, "error", (err as Error).message ?? String(err));
    } finally {
      if (sendBtn) {
        sendBtn.disabled = false;
      }
    }
  });

  // Surface sidecar crashes to the UI explicitly — non-negotiable signal
  // for gate #6. Renderer treats them as agent-side errors so the user
  // sees a clear failure instead of a silent hang.
  if (typeof window.suiteStudio.onSidecarCrashed === "function") {
    window.suiteStudio.onSidecarCrashed((info) => {
      appendTurn(
        history,
        "error",
        `Sidecar crashed (code=${info.code} signal=${info.signal ?? "null"}). ` +
          "Restart the app to reconnect.",
      );
    });
  }
}

// Tests import this module directly into a jsdom DOM that's already
// loaded, so we must run the wiring synchronously — but in the real
// Electron renderer the script tag is at end-of-body so the DOM is
// already parsed too. Either way, no DOMContentLoaded wait is needed.
wireComposer();
