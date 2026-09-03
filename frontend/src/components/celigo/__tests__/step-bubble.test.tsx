import { render, screen, within, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CeligoAttachment, CeligoFlowStep } from "@/hooks/use-celigo-flows";
import { StepBubble } from "../step-bubble";

// Task 15 — one flow-step bubble on the canvas (mockup screen 3's `.bubble`).
// `node` only needs the geometry StepBubble actually reads.
const NODE = { x: 20, y: 40, w: 212, h: 162 };

function makeAttachment(overrides: Partial<CeligoAttachment> = {}): CeligoAttachment {
  return {
    id: "att-1",
    flow_id: "flow-1",
    flow_step_id: "step",
    script_id: "script-1",
    script_celigo_id: "script-celigo-1",
    function_name: null,
    json_path: "x.hooks.fn",
    site_type: "hook",
    script_name: null,
    script_size_chars: null,
    script_copies_count: null,
    script_versions_count: null,
    script_version_letter: null,
    script_content_diverged: null,
    ...overrides,
  };
}

function makeStep(overrides: Partial<CeligoFlowStep> = {}): CeligoFlowStep {
  return {
    id: "step",
    celigo_id: "cel-step",
    role: "processor",
    router_id: null,
    branch_id: null,
    branch_key: "$root",
    sequence: 0,
    adaptor_type: "HTTPExport",
    connection_celigo_id: null,
    reference_name: null,
    filter_json: null,
    mapping_json: null,
    proceed_on_failure: null,
    skip_retries: null,
    kind: "destination",
    record_type: null,
    operation: null,
    search_id: null,
    attachments: [],
    error_count: 0,
    ...overrides,
  };
}

function renderBubble(step: CeligoFlowStep, extra: Partial<Parameters<typeof StepBubble>[0]> = {}) {
  const onSelect = vi.fn();
  render(
    <StepBubble step={step} node={NODE} selected={extra.selected ?? false} paused={extra.paused ?? false} onSelect={extra.onSelect ?? onSelect} />,
  );
  return { onSelect: extra.onSelect ?? onSelect };
}

describe("StepBubble — eyebrow, title, fact line", () => {
  it("eyebrow reads 'Lookup · HTTP' with an 'H' app glyph", () => {
    renderBubble(makeStep({ id: "s1", kind: "lookup", role: "processor", adaptor_type: "HTTPExport" }));
    const bubble = screen.getByTestId("step-bubble-s1");
    expect(within(bubble).getByText("Lookup · HTTP")).toBeInTheDocument();
    expect(within(bubble).getByText("H")).toBeInTheDocument();
  });

  it("title falls back to the honest description with data-unsynced=\"true\" when reference_name is unset", () => {
    renderBubble(makeStep({ id: "s1", kind: "lookup", role: "processor", adaptor_type: "HTTPExport" }));
    const bubble = screen.getByTestId("step-bubble-s1");
    const title = within(bubble).getByText("HTTP lookup · name not synced");
    expect(title).toHaveAttribute("data-unsynced", "true");
  });

  it("title uses reference_name verbatim when Celigo has synced one, with no data-unsynced", () => {
    renderBubble(makeStep({ id: "s1", kind: "lookup", role: "processor", adaptor_type: "HTTPExport", reference_name: "Lookup Sales Orders" }));
    const bubble = screen.getByTestId("step-bubble-s1");
    const title = within(bubble).getByText("Lookup Sales Orders");
    expect(title).not.toHaveAttribute("data-unsynced");
  });

  it("a NetSuite destination's fallback title states the real fact — no data-unsynced", () => {
    renderBubble(
      makeStep({ id: "s1", kind: "destination", role: "processor", adaptor_type: "NetSuiteDistributedImport", operation: "add", record_type: "salesorder" }),
    );
    const bubble = screen.getByTestId("step-bubble-s1");
    const title = within(bubble).getByText("add salesorder");
    expect(title).not.toHaveAttribute("data-unsynced");
    expect(within(bubble).getByText("import · add · salesorder")).toBeInTheDocument();
  });

  it("a NetSuite lookup's fact line reads 'export · saved search {id} · {record_type}'", () => {
    renderBubble(
      makeStep({ id: "s1", kind: "lookup", role: "processor", adaptor_type: "NetSuiteDistributedExport", search_id: "5090", record_type: "customer" }),
    );
    const bubble = screen.getByTestId("step-bubble-s1");
    expect(within(bubble).getByText("export · saved search 5090 · customer")).toBeInTheDocument();
  });

  it("a non-NetSuite fact line reads '{family} {export|import} · conn {first 8 chars}…'", () => {
    renderBubble(makeStep({ id: "s1", kind: "source", role: "generator", adaptor_type: "HTTPExport", connection_celigo_id: "648bd44c1234567" }));
    const bubble = screen.getByTestId("step-bubble-s1");
    expect(within(bubble).getByText("http export · conn 648bd44c…")).toBeInTheDocument();
  });
});

describe("StepBubble — affordance chips", () => {
  it("a source's transform chip also selects the step at the scripts tab, same as a hooks chip", () => {
    const onSelect = vi.fn();
    renderBubble(makeStep({ id: "s1", kind: "source", role: "generator", adaptor_type: "HTTPExport" }), { onSelect });
    const bubble = screen.getByTestId("step-bubble-s1");
    // source order: transform (scripts) · hooks (scripts) · output_filter (filter)
    fireEvent.click(within(bubble).getByText("no transform"));
    expect(onSelect).toHaveBeenCalledWith("s1", "scripts");
  });

  it("renders every chip affordanceChips returns, in order, and each click selects the step at the matching tab", () => {
    const onSelect = vi.fn();
    renderBubble(makeStep({ id: "s1", kind: "destination", role: "processor", adaptor_type: "NetSuiteDistributedImport" }), { onSelect });
    const bubble = screen.getByTestId("step-bubble-s1");
    // destination order: input_filter (filter) · ns_mapping (mapping) · response_mapping (mapping) · hooks (scripts)
    fireEvent.click(within(bubble).getByText("no input filter"));
    fireEvent.click(within(bubble).getByText("⇄ NS field map · not synced"));
    fireEvent.click(within(bubble).getByText("no resp. map"));
    fireEvent.click(within(bubble).getByText("no hooks"));
    expect(onSelect.mock.calls).toEqual([
      ["s1", "filter"],
      ["s1", "mapping"],
      ["s1", "mapping"],
      ["s1", "scripts"],
    ]);
  });

  it("a diverged hook chip shows 'HK {fn}' + a 'C/3' badge + an amber divergence dot", () => {
    renderBubble(
      makeStep({
        id: "s1",
        kind: "destination",
        role: "processor",
        adaptor_type: "NetSuiteDistributedImport",
        attachments: [
          makeAttachment({
            function_name: "preMap",
            script_version_letter: "C",
            script_versions_count: 3,
            script_copies_count: 7,
            script_content_diverged: true,
          }),
        ],
      }),
    );
    const bubble = screen.getByTestId("step-bubble-s1");
    expect(within(bubble).getByText("preMap")).toBeInTheDocument();
    expect(within(bubble).getByText("C/3")).toBeInTheDocument();
    expect(bubble.querySelector('[title="copies of this script differ"]')).toBeInTheDocument();
  });

  it("a single-copy hook chip shows '×1', not a version letter, and no divergence dot", () => {
    renderBubble(
      makeStep({
        id: "s1",
        kind: "destination",
        role: "processor",
        adaptor_type: "NetSuiteDistributedImport",
        attachments: [makeAttachment({ function_name: "preSavePage", script_copies_count: 1, script_versions_count: 1, script_content_diverged: false })],
      }),
    );
    const bubble = screen.getByTestId("step-bubble-s1");
    expect(within(bubble).getByText("preSavePage")).toBeInTheDocument();
    expect(within(bubble).getByText("×1")).toBeInTheDocument();
    expect(bubble.querySelector('[title="copies of this script differ"]')).not.toBeInTheDocument();
  });
});

describe("StepBubble — footer (proceed-on-failure for processor steps, retries for sources)", () => {
  it("proceed_on_failure === false reads 'stops flow on failure'", () => {
    renderBubble(makeStep({ id: "s1", role: "processor", kind: "destination", proceed_on_failure: false }));
    expect(within(screen.getByTestId("step-bubble-s1")).getByText("stops flow on failure")).toBeInTheDocument();
  });

  it("proceed_on_failure === true reads 'continues on failure', in amber", () => {
    renderBubble(makeStep({ id: "s1", role: "processor", kind: "destination", proceed_on_failure: true }));
    const footer = within(screen.getByTestId("step-bubble-s1")).getByText("continues on failure");
    expect(footer.className).toMatch(/amber/);
  });

  it("proceed_on_failure === null reads 'stops on failure · default'", () => {
    renderBubble(makeStep({ id: "s1", role: "processor", kind: "destination", proceed_on_failure: null }));
    expect(within(screen.getByTestId("step-bubble-s1")).getByText("stops on failure · default")).toBeInTheDocument();
  });

  it("a source with skip_retries=true reads 'retries skipped'", () => {
    renderBubble(makeStep({ id: "s1", role: "generator", kind: "source", skip_retries: true }));
    expect(within(screen.getByTestId("step-bubble-s1")).getByText("retries skipped")).toBeInTheDocument();
  });

  it("a source with skip_retries unset reads 'retries on'", () => {
    renderBubble(makeStep({ id: "s1", role: "generator", kind: "source", skip_retries: null }));
    expect(within(screen.getByTestId("step-bubble-s1")).getByText("retries on")).toBeInTheDocument();
  });
});

describe("StepBubble — error badge, selection, paused", () => {
  it("error_count > 0 shows a 'N open' badge and data-error=\"true\"", () => {
    renderBubble(makeStep({ id: "s1", error_count: 10 }));
    const bubble = screen.getByTestId("step-bubble-s1");
    expect(bubble).toHaveAttribute("data-error", "true");
    expect(within(bubble).getByText("10 open")).toBeInTheDocument();
  });

  it("error_count === 0 carries no data-error and no badge", () => {
    renderBubble(makeStep({ id: "s1", error_count: 0 }));
    const bubble = screen.getByTestId("step-bubble-s1");
    expect(bubble).not.toHaveAttribute("data-error");
    expect(within(bubble).queryByText(/open$/)).not.toBeInTheDocument();
  });

  it("selected renders data-selected=\"true\"", () => {
    renderBubble(makeStep({ id: "s1" }), { selected: true });
    expect(screen.getByTestId("step-bubble-s1")).toHaveAttribute("data-selected", "true");
  });

  it("paused renders the opacity-60 class", () => {
    renderBubble(makeStep({ id: "s1" }), { paused: true });
    expect(screen.getByTestId("step-bubble-s1").className).toMatch(/opacity-60/);
  });

  it("clicking the bubble itself selects the step with no tab", () => {
    const onSelect = vi.fn();
    renderBubble(makeStep({ id: "s1" }), { onSelect });
    fireEvent.click(screen.getByTestId("step-bubble-s1"));
    expect(onSelect).toHaveBeenCalledWith("s1", undefined);
  });

  it("is operable from the keyboard: focusable, announced as a button, Enter and Space select", () => {
    // Final-review finding I8. The bubble was a bare `<div onClick>` -- no
    // role, no tab stop, no key handling -- so the canvas was mouse-only and
    // a screen reader was told nothing was there to activate. The chips
    // inside are real <button>s and were already reachable, which made this
    // easy to miss: you could tab to a chip but never to the bubble itself.
    const onSelect = vi.fn();
    renderBubble(makeStep({ id: "s1" }), { onSelect });
    const bubble = screen.getByTestId("step-bubble-s1");

    expect(bubble).toHaveAttribute("role", "button");
    expect(bubble).toHaveAttribute("tabindex", "0");
    expect(bubble.className).toMatch(/focus-visible:ring-2/);

    fireEvent.keyDown(bubble, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith("s1", undefined);

    onSelect.mockClear();
    fireEvent.keyDown(bubble, { key: " " });
    expect(onSelect).toHaveBeenCalledWith("s1", undefined);
  });

  it("a key press on a chip inside the bubble does not also fire the bubble's own selection", () => {
    const onSelect = vi.fn();
    renderBubble(makeStep({ id: "s1" }), { onSelect });
    const chip = within(screen.getByTestId("step-bubble-s1")).getByText("no hooks");
    fireEvent.keyDown(chip, { key: "Enter" });
    expect(onSelect).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Codex fix wave
// ---------------------------------------------------------------------------

describe("StepBubble — an unsynced adaptor is never called HTTP (item 6)", () => {
  it("titles by kind and says the adaptor is not synced, in both the title and the fact line", () => {
    renderBubble(makeStep({ id: "s1", kind: "destination", role: "processor", adaptor_type: null, connection_celigo_id: null }));
    const bubble = screen.getByTestId("step-bubble-s1");

    expect(within(bubble).getByText("Destination · adaptor not synced")).toHaveAttribute("data-unsynced", "true");
    expect(within(bubble).getByText("adaptor not synced")).toBeInTheDocument();
    expect(bubble.textContent).not.toContain("HTTP");
    expect(bubble.textContent).not.toContain("http");
  });

  it("keeps the connection id, which IS a synced fact, alongside the unsynced adaptor", () => {
    renderBubble(makeStep({ id: "s1", kind: "lookup", role: "processor", adaptor_type: null, connection_celigo_id: "648bd44c1234567" }));
    const bubble = screen.getByTestId("step-bubble-s1");
    expect(within(bubble).getByText("adaptor not synced · conn 648bd44c…")).toBeInTheDocument();
  });
});

describe("StepBubble — the fact line reads the step's KIND, not the adaptor string (item 13)", () => {
  it("an AS2 destination is an import, even though 'AS2' contains no 'Import'", () => {
    renderBubble(makeStep({ id: "s1", kind: "destination", role: "processor", adaptor_type: "AS2", connection_celigo_id: null }));
    const bubble = screen.getByTestId("step-bubble-s1");
    expect(within(bubble).getByText("as2 import")).toBeInTheDocument();
    expect(bubble.textContent).not.toContain("as2 export");
  });

  it("an FTP source is an export", () => {
    renderBubble(makeStep({ id: "s1", kind: "source", role: "generator", adaptor_type: "FTPImport", connection_celigo_id: null }));
    expect(within(screen.getByTestId("step-bubble-s1")).getByText("ftp export")).toBeInTheDocument();
  });

  it("an HTTP lookup says so outright rather than borrowing the source's word", () => {
    renderBubble(makeStep({ id: "s1", kind: "lookup", role: "processor", adaptor_type: "HTTPExport", connection_celigo_id: null }));
    expect(within(screen.getByTestId("step-bubble-s1")).getByText("http export · lookup")).toBeInTheDocument();
  });
});

describe("StepBubble — a long title is clamped, not spilled out of the bubble (item 26)", () => {
  it("clamps to two lines and keeps the full name in the title attribute", () => {
    const name =
      "NS > Solidus — Shipping Confirmations and Fulfilment Status Sync for the Multi-Subsidiary Backfill v2";
    renderBubble(makeStep({ id: "s1", reference_name: name }));
    const title = within(screen.getByTestId("step-bubble-s1")).getByText(name);

    // `line-clamp-2` is Tailwind core and carries `overflow: hidden` with it;
    // a separate `overflow-hidden` beside it is stripped by tailwind-merge as
    // a conflicting rule, so this pins the class that actually survives.
    expect(title.className).toMatch(/line-clamp-2/);
    expect(title).toHaveAttribute("title", name);
  });
});
